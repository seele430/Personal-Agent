"""访问日志与证书测试：脱敏、JSONL、SAN、私钥权限。"""

import tempfile
import unittest

from pathlib import Path

from src.remote.access_log import (
    AccessLog,
    AccessRecord,
    build_record,
    redact,
    redact_headers,
    strip_query,
)
from src.remote.cert import (
    CERT_FILENAME,
    KEY_FILENAME,
    CertError,
    describe,
    generate_self_signed,
)


class TestRedaction(unittest.TestCase):
    def test_strip_query(self):
        self.assertEqual(strip_query("/api/artifacts?token=x"), "/api/artifacts")

    def test_strip_query_empty(self):
        self.assertEqual(strip_query(""), "/")

    def test_redact_token_query(self):
        self.assertEqual(
            redact("/a?token=abc&x=1"), "/a?token=***&x=1"
        )

    def test_redact_keeps_normal_query(self):
        self.assertEqual(redact("/a?x=1"), "/a?x=1")

    def test_redact_headers_masks_authorization(self):
        headers = redact_headers(
            [(b"Authorization", b"Bearer secret"), (b"user-agent", b"pytest")]
        )

        self.assertEqual(headers["authorization"], "***")
        self.assertEqual(headers["user-agent"], "pytest")


class TestBuildRecord(unittest.TestCase):
    def test_record_fields(self):
        record = build_record(
            method="get",
            path="/api/artifacts?token=abc",
            status=200,
            duration_ms=12.345,
            client="10.0.0.1",
        )

        self.assertEqual(record.method, "GET")
        self.assertEqual(record.path, "/api/artifacts")
        self.assertEqual(record.status, 200)
        self.assertEqual(record.duration_ms, 12.35)

    def test_long_user_agent_truncated(self):
        record = build_record(
            method="GET",
            path="/health",
            status=200,
            duration_ms=1.0,
            client="1.1.1.1",
            user_agent="u" * 500,
        )

        self.assertLessEqual(len(record.user_agent), 120)

    def test_serializes(self):
        payload = build_record(
            method="GET", path="/health", status=200, duration_ms=1.0, client="c"
        ).to_dict()

        self.assertIn("ts", payload)
        self.assertEqual(payload["method"], "GET")


class TestAccessLog(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.path = Path(self._temporary.name) / "nested" / "access.jsonl"

    def test_append_creates_dirs_and_reads_back(self):
        log = AccessLog(self.path)
        log.append(
            build_record(method="GET", path="/health", status=200, duration_ms=1.0, client="c")
        )

        records = log.read()

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["path"], "/health")

    def test_multiple_lines(self):
        log = AccessLog(self.path)

        for index in range(3):
            log.append(
                build_record(
                    method="GET", path=f"/health?i={index}", status=200,
                    duration_ms=1.0, client="c",
                )
            )

        self.assertEqual(len(log.read()), 3)
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 3)

    def test_read_missing_file(self):
        self.assertEqual(AccessLog(self.path).read(), [])

    def test_record_is_immutable(self):
        record = AccessRecord(
            ts="t", method="GET", path="/", status=200, duration_ms=1.0, client="c"
        )

        with self.assertRaises(Exception):
            record.status = 500


class TestCert(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.out = Path(self._temporary.name) / "tls"

    def test_generates_pair(self):
        cert, key = generate_self_signed(ip="10.224.76.60", out_dir=self.out)

        self.assertTrue(cert.is_file())
        self.assertTrue(key.is_file())
        self.assertEqual(cert.name, CERT_FILENAME)
        self.assertEqual(key.name, KEY_FILENAME)

    def test_san_contains_ip_and_hostname(self):
        cert, _ = generate_self_signed(
            ip="10.224.76.60", out_dir=self.out, hostnames=("localhost",)
        )

        info = describe(cert)

        self.assertIn("10.224.76.60", info["ips"])
        self.assertIn("localhost", info["dns"])

    def test_key_contains_private_material(self):
        _, key = generate_self_signed(ip="127.0.0.1", out_dir=self.out)

        self.assertIn("PRIVATE KEY", key.read_text(encoding="utf-8"))

    def test_invalid_ip_rejected(self):
        with self.assertRaises(CertError):
            generate_self_signed(ip="not-an-ip", out_dir=self.out)

    def test_invalid_days_rejected(self):
        with self.assertRaises(CertError):
            generate_self_signed(ip="127.0.0.1", out_dir=self.out, days=0)

    def test_describe_subject(self):
        cert, _ = generate_self_signed(ip="127.0.0.1", out_dir=self.out)

        self.assertIn("Personal Agent Remote", describe(cert)["subject"])


class TestRotation(unittest.TestCase):
    """日志轮转：超限生成 .1、保留份数、未超限不动、尽力而为。"""

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.path = Path(self._temporary.name) / "logs" / "access.jsonl"

    def record(self, index=0):
        return build_record(
            method="GET",
            path=f"/health?i={index}",
            status=200,
            duration_ms=1.0,
            client="c",
        )

    def test_no_rotation_when_small(self):
        log = AccessLog(self.path, max_bytes=10_000)
        log.append(self.record())

        self.assertTrue(self.path.is_file())
        self.assertFalse((self.path.parent / "access.jsonl.1").exists())

    def test_rotation_creates_backup(self):
        log = AccessLog(self.path, max_bytes=120, backups=2)

        for index in range(6):
            log.append(self.record(index))

        backups = sorted(p.name for p in self.path.parent.glob("access.jsonl.*"))

        self.assertIn("access.jsonl.1", backups)
        self.assertTrue(self.path.is_file())

    def test_keeps_only_n_backups(self):
        log = AccessLog(self.path, max_bytes=120, backups=2)

        for index in range(20):
            log.append(self.record(index))

        backups = sorted(p.name for p in self.path.parent.glob("access.jsonl.*"))

        self.assertLessEqual(len(backups), 2)
        self.assertNotIn("access.jsonl.3", backups)

    def test_writes_continue_after_rotation(self):
        log = AccessLog(self.path, max_bytes=120, backups=1)

        for index in range(5):
            log.append(self.record(index))

        self.assertGreaterEqual(len(log.read()), 1)

    def test_invalid_parameters(self):
        with self.assertRaises(Exception):
            AccessLog(self.path, max_bytes=0)

        with self.assertRaises(Exception):
            AccessLog(self.path, backups=0)

    def test_defaults_are_large(self):
        log = AccessLog(self.path)

        self.assertEqual(log.max_bytes, 1_048_576)
        self.assertEqual(log.backups, 3)

    def test_rotation_preserves_previous_content(self):
        log = AccessLog(self.path, max_bytes=150, backups=1)

        for index in range(8):
            log.append(self.record(index))

        backup = self.path.parent / "access.jsonl.1"

        if backup.is_file():
            self.assertGreater(len(backup.read_text(encoding="utf-8")), 0)

        self.assertGreater(len(log.read()), 0)

    def test_unicode_safe(self):
        log = AccessLog(self.path, max_bytes=10_000)
        log.append(self.record(1))

        text = self.path.read_text(encoding="utf-8")

        self.assertIn("/health", text)
if __name__ == "__main__":
    unittest.main()
