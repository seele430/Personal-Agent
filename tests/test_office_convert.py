"""Office 转换核心与 API 测试（mock 外部进程；真机证据另存）。"""

import io
import json
import os
import tempfile
import unittest
import uuid
import zipfile

from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.artifacts.registry import ArtifactForbidden, ArtifactNotFound
from src.core import office_convert


def make_docx(path: Path, text="hello"):
    content_types = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        "</Relationships>"
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document)


def make_pdf(path: Path):
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)

    with path.open("wb") as handle:
        writer.write(handle)


class FakeRegistry:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.registered = []
        self.fail_register = False

    def get(self, artifact_id, *, owner_id):
        if artifact_id == "art_missing":
            raise ArtifactNotFound(artifact_id)

        if artifact_id == "art_other":
            raise ArtifactForbidden("forbidden")

        return SimpleNamespace(id=artifact_id, owner_id=owner_id, title="x")

    def absolute_path(self, artifact_id, *, owner_id):
        return Path(self.root) / "source.docx"

    def register(self, owner_id, path, *, title=None, media_type=""):
        if self.fail_register:
            raise RuntimeError("db down")

        artifact = SimpleNamespace(
            id=f"art_{uuid.uuid4().hex}", owner_id=owner_id,
            title=title, media_type=media_type,
        )
        self.registered.append(artifact)

        return artifact


class FakeExecutor:
    def __init__(self, root: Path, behavior=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.behavior = behavior or (lambda workspace, out: make_pdf(out / "input.pdf"))
        self.calls = []
        self.destroyed = []

    def open_sandbox(self):
        sandbox = "sbx_" + uuid.uuid4().hex
        (self.root / sandbox / "workspace").mkdir(parents=True)

        return sandbox

    def execute_command(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        workspace = Path(self.root) / kwargs["sandbox_id"] / "workspace"
        out = workspace / "out"
        self.behavior(workspace, out)

        return SimpleNamespace(status="succeeded", error_code=None, duration_ms=10)

    def destroy(self, sandbox_id):
        self.destroyed.append(sandbox_id)
        import shutil
        shutil.rmtree(self.root / sandbox_id, ignore_errors=True)


class OfficeCoreCase(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.registry = FakeRegistry(self.root / "artifacts")
        self.source = self.registry.root / "source.docx"
        make_docx(self.source)
        self.soffice = self.root / "soffice.com"
        self.soffice.write_text("stub")

    def convert(self, executor=None, soffice=None):
        return office_convert.convert_artifact(
            source_artifact_id="art_test", owner_id="owner-1",
            registry=self.registry,
            executor=executor or FakeExecutor(self.root / "sandboxes"),
            soffice=soffice or self.soffice,
        )

    def test_unsupported_extension(self):
        (self.registry.root / "source.txt").write_text("x")

        with self.assertRaises(office_convert.OfficeError) as caught:
            office_convert.preflight(self.registry.root / "source.txt")

        self.assertEqual(caught.exception.status, 415)

    def test_empty_source(self):
        source = self.registry.root / "empty.docx"
        source.write_bytes(b"")

        with self.assertRaises(office_convert.OfficeError) as caught:
            office_convert.preflight(source)

        self.assertEqual(caught.exception.status, 413)

    def test_invalid_zip(self):
        source = self.registry.root / "bad.docx"
        source.write_bytes(b"not a zip")

        with self.assertRaises(office_convert.OfficeError) as caught:
            office_convert.preflight(source)

        self.assertEqual(caught.exception.code, "invalid_ooxml")

    def test_content_type_mismatch(self):
        source = self.registry.root / "wrong.docx"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("_rels/.rels", "<Relationships/>")

        with self.assertRaises(office_convert.OfficeError) as caught:
            office_convert.preflight(source)

        self.assertIn(caught.exception.code, ("content_type_mismatch", "invalid_ooxml"))

    def test_macro_rejected(self):
        source = self.registry.root / "macro.docx"
        make_docx(source)

        with zipfile.ZipFile(source, "a") as archive:
            archive.writestr("word/vbaProject.bin", b"macro")

        with self.assertRaises(office_convert.OfficeError) as caught:
            office_convert.preflight(source)

        self.assertEqual(caught.exception.code, "unsupported_active_content")

    def test_external_relationship_rejected(self):
        source = self.registry.root / "external.docx"
        make_docx(source)

        with zipfile.ZipFile(source, "a") as archive:
            archive.writestr(
                "word/_rels/document.xml.rels",
                '<Relationships><Relationship Id="r" Target="http://x" TargetMode="External"/></Relationships>',
            )

        with self.assertRaises(office_convert.OfficeError) as caught:
            office_convert.preflight(source)

        self.assertEqual(caught.exception.code, "external_relationship")

    def test_symlink_source_rejected(self):
        link = self.registry.root / "link.docx"

        try:
            os.symlink(self.source, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlink 不可用")

        with self.assertRaises(office_convert.OfficeError):
            office_convert.preflight(link)

    def test_convert_success(self):
        result = self.convert()

        self.assertTrue(result.artifact_id.startswith("art_"))
        self.assertEqual(result.media_type, "application/pdf")
        self.assertGreaterEqual(result.page_count, 1)

    def test_missing_pdf(self):
        with self.assertRaises(office_convert.OfficeError) as caught:
            self.convert(FakeExecutor(self.root / "s1", behavior=lambda w, o: None))

        self.assertEqual(caught.exception.code, "unexpected_output")

    def test_multiple_pdf(self):
        def behavior(workspace, out):
            make_pdf(out / "input.pdf")
            make_pdf(out / "extra.pdf")

        with self.assertRaises(office_convert.OfficeError):
            self.convert(FakeExecutor(self.root / "s2", behavior=behavior))

    def test_empty_pdf(self):
        def behavior(workspace, out):
            (out / "input.pdf").write_bytes(b"")

        with self.assertRaises(office_convert.OfficeError) as caught:
            self.convert(FakeExecutor(self.root / "s3", behavior=behavior))

        self.assertEqual(caught.exception.code, "pdf_empty")

    def test_bad_pdf_header(self):
        def behavior(workspace, out):
            (out / "input.pdf").write_bytes(b"not a pdf")

        with self.assertRaises(office_convert.OfficeError) as caught:
            self.convert(FakeExecutor(self.root / "s4", behavior=behavior))

        self.assertEqual(caught.exception.code, "pdf_bad_header")

    def test_timeout_mapping(self):
        class TimeoutExecutor(FakeExecutor):
            def execute_command(self, argv, **kwargs):
                return SimpleNamespace(status="timed_out", error_code="EXEC_TIMED_OUT", duration_ms=120000)

        with self.assertRaises(office_convert.OfficeError) as caught:
            self.convert(TimeoutExecutor(self.root / "s5"))

        self.assertEqual(caught.exception.status, 504)

    def test_cleanup_failure_mapping(self):
        class CleanupExecutor(FakeExecutor):
            def execute_command(self, argv, **kwargs):
                return SimpleNamespace(status="failed", error_code="RUNNER_CLEANUP_FAILED", duration_ms=1)

        with self.assertRaises(office_convert.OfficeError) as caught:
            self.convert(CleanupExecutor(self.root / "s6"))

        self.assertEqual(caught.exception.status, 500)

    def test_source_modified(self):
        def behavior(workspace, out):
            self.source.write_text("changed")
            make_pdf(out / "input.pdf")

        with self.assertRaises(office_convert.OfficeError) as caught:
            self.convert(FakeExecutor(self.root / "s7", behavior=behavior))

        self.assertEqual(caught.exception.code, "source_modified")

    def test_registry_failure(self):
        self.registry.fail_register = True

        with self.assertRaises(office_convert.OfficeError) as caught:
            self.convert()

        self.assertEqual(caught.exception.status, 500)

    def test_artifact_not_found(self):
        with self.assertRaises(office_convert.OfficeError) as caught:
            office_convert.convert_artifact(
                source_artifact_id="art_missing", owner_id="owner-1",
                registry=self.registry, executor=FakeExecutor(self.root / "s8"),
                soffice=self.soffice,
            )

        self.assertEqual(caught.exception.status, 404)

    def test_artifact_forbidden(self):
        with self.assertRaises(office_convert.OfficeError) as caught:
            office_convert.convert_artifact(
                source_artifact_id="art_other", owner_id="owner-1",
                registry=self.registry, executor=FakeExecutor(self.root / "s9"),
                soffice=self.soffice,
            )

        self.assertEqual(caught.exception.status, 403)

    def test_office_unavailable(self):
        with self.assertRaises(office_convert.OfficeError) as caught:
            office_convert.convert_artifact(
                source_artifact_id="art_test", owner_id="owner-1",
                registry=self.registry, executor=FakeExecutor(self.root / "s10"),
                soffice=Path("does-not-exist.exe"),
            )

        self.assertEqual(caught.exception.status, 503)


    def _rels_variant(self, rels_xml):
        source = self.registry.root / "variant.docx"
        make_docx(source)

        with zipfile.ZipFile(source, "a") as archive:
            archive.writestr("word/_rels/document.xml.rels", rels_xml)

        return source

    def test_external_single_quotes_rejected(self):
        source = self._rels_variant(
            "<Relationships><Relationship TargetMode='External' Target='http://x'/></Relationships>"
        )

        with self.assertRaises(office_convert.OfficeError):
            office_convert.preflight(source)

    def test_external_spacing_and_newlines_rejected(self):
        source = self._rels_variant(
            "<Relationships><Relationship TargetMode \t=\n 'External' Target='http://x'/></Relationships>"
        )

        with self.assertRaises(office_convert.OfficeError):
            office_convert.preflight(source)

    def test_external_uppercase_rejected(self):
        source = self._rels_variant(
            '<Relationships><Relationship TargetMode="EXTERNAL" Target="http://x"/></Relationships>'
        )

        with self.assertRaises(office_convert.OfficeError):
            office_convert.preflight(source)

    def test_external_utf16_rejected(self):
        xml = (
            '<?xml version="1.0" encoding="utf-16"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship TargetMode="External" Target="http://x"/></Relationships>'
        ).encode("utf-16")
        source = self.registry.root / "utf16.docx"
        make_docx(source)

        with zipfile.ZipFile(source, "a") as archive:
            archive.writestr("word/_rels/document.xml.rels", xml)

        with self.assertRaises(office_convert.OfficeError):
            office_convert.preflight(source)

    def test_malformed_relationship_xml(self):
        source = self._rels_variant("<Relationships><Relationship")

        with self.assertRaises(office_convert.OfficeError) as caught:
            office_convert.preflight(source)

        self.assertEqual(caught.exception.code, "invalid_relationship_xml")

    def _changed_on_failure(self, executor):
        with self.assertRaises(office_convert.OfficeError) as caught:
            self.convert(executor)

        self.assertEqual(caught.exception.code, "source_modified")

    def test_timeout_source_modified(self):
        class TimeoutExecutor(FakeExecutor):
            def execute_command(self, argv, **kwargs):
                self.write_source()
                return SimpleNamespace(status="timed_out", error_code="EXEC_TIMED_OUT", duration_ms=1)

            def write_source(self):
                self.source_owner.write_text("changed during conversion")

        executor = TimeoutExecutor(self.root / "f1")
        executor.source_owner = self.source
        self._changed_on_failure(executor)

    def test_missing_pdf_source_modified(self):
        def behavior(workspace, out):
            self.source.write_text("changed during conversion")

        self._changed_on_failure(FakeExecutor(self.root / "f2", behavior=behavior))

    def test_invalid_pdf_source_modified(self):
        def behavior(workspace, out):
            self.source.write_text("changed during conversion")
            (out / "input.pdf").write_bytes(b"bad")

        self._changed_on_failure(FakeExecutor(self.root / "f3", behavior=behavior))

    def test_extra_non_pdf_output_rejected(self):
        def behavior(workspace, out):
            make_pdf(out / "input.pdf")
            (out / "unexpected.txt").write_text("extra")

        with self.assertRaises(office_convert.OfficeError) as caught:
            self.convert(FakeExecutor(self.root / "f4", behavior=behavior))

        self.assertEqual(caught.exception.code, "unexpected_output")

    def test_destroy_failure(self):
        class BrokenDestroy(FakeExecutor):
            def destroy(self, sandbox_id):
                raise RuntimeError("cannot remove")

        with self.assertRaises(office_convert.OfficeError) as caught:
            self.convert(BrokenDestroy(self.root / "f5"))

        self.assertEqual(caught.exception.code, "sandbox_cleanup_failed")
        self.assertEqual(self.registry.registered, [])

    def test_destroy_leaves_directory(self):
        class LeftoverDestroy(FakeExecutor):
            def destroy(self, sandbox_id):
                self.destroyed.append(sandbox_id)

        with self.assertRaises(office_convert.OfficeError) as caught:
            self.convert(LeftoverDestroy(self.root / "f6"))

        self.assertEqual(caught.exception.code, "sandbox_cleanup_failed")

    def test_directory_source_rejected(self):
        directory = self.registry.root / "dir.docx"
        directory.mkdir()

        with self.assertRaises(office_convert.OfficeError):
            office_convert.preflight(directory)

    def test_junction_ancestor_rejected(self):
        import subprocess as sp

        real = self.registry.root / "real-dir"
        real.mkdir(exist_ok=True)
        make_docx(real / "inner.docx")
        link = self.registry.root / "link-dir"
        result = sp.run(["cmd", "/c", "mklink", "/J", str(link), str(real)], capture_output=True)

        if result.returncode != 0:
            self.skipTest("junction 不可用")

        with self.assertRaises(office_convert.OfficeError) as caught:
            office_convert.preflight(link / "inner.docx")

        self.assertEqual(caught.exception.code, "path_link_rejected")

    def test_argv_profile_and_no_shell(self):
        executor = FakeExecutor(self.root / "f7")
        self.convert(executor)
        argv, kwargs = executor.calls[0]

        self.assertTrue(argv[0].endswith("soffice.com"))
        self.assertIn("--headless", argv)
        self.assertTrue(any(item.startswith("-env:UserInstallation=file:") for item in argv))
        self.assertNotIn("shell", kwargs)

    def test_encrypted_pdf_rejected(self):
        def behavior(workspace, out):
            from pypdf import PdfWriter

            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            writer.encrypt("pw")

            with (out / "input.pdf").open("wb") as handle:
                writer.write(handle)

        with self.assertRaises(office_convert.OfficeError):
            self.convert(FakeExecutor(self.root / "f8", behavior=behavior))

    def test_eof_outside_window_rejected(self):
        def behavior(workspace, out):
            make_pdf(out / "input.pdf")
            with (out / "input.pdf").open("ab") as handle:
                handle.write(b"\n" * 3000)

        with self.assertRaises(office_convert.OfficeError) as caught:
            self.convert(FakeExecutor(self.root / "f9", behavior=behavior))

        self.assertEqual(caught.exception.code, "pdf_missing_eof")

    def test_register_failure_cleans_output(self):
        self.registry.fail_register = True
        before = set(self.registry.root.iterdir())

        with self.assertRaises(office_convert.OfficeError):
            self.convert()

        office = self.registry.root / "office"

        if office.exists():
            self.assertEqual(list(office.iterdir()), [])

    def _patched(self, name, value):
        return patch.object(office_convert, name, value)

    def test_input_size_budget(self):
        with self._patched("MAX_INPUT_BYTES", 5):
            with self.assertRaises(office_convert.OfficeError) as caught:
                office_convert.preflight(self.source)

        self.assertEqual(caught.exception.code, "source_too_large")

    def test_zip_entry_budget(self):
        with self._patched("MAX_ZIP_ENTRIES", 1):
            with self.assertRaises(office_convert.OfficeError) as caught:
                office_convert.preflight(self.source)

        self.assertEqual(caught.exception.code, "zip_too_many_entries")

    def test_uncompressed_budget(self):
        with self._patched("MAX_UNCOMPRESSED", 1):
            with self.assertRaises(office_convert.OfficeError):
                office_convert.preflight(self.source)

    def test_single_entry_budget(self):
        with self._patched("MAX_ENTRY_UNCOMPRESSED", 1):
            with self.assertRaises(office_convert.OfficeError):
                office_convert.preflight(self.source)

    def test_encrypted_zip_rejected(self):
        source = self.registry.root / "encrypted.docx"
        make_docx(source)

        import struct

        data = bytearray(source.read_bytes())
        offset = 0

        while True:
            offset = data.find(b"PK\x03\x04", offset)

            if offset < 0:
                break

            struct.pack_into("<H", data, offset + 6, 1)
            offset += 4

        offset = 0

        while True:
            offset = data.find(b"PK\x01\x02", offset)

            if offset < 0:
                break

            struct.pack_into("<H", data, offset + 8, 1)
            offset += 4

        source.write_bytes(bytes(data))

        with self.assertRaises(office_convert.OfficeError) as caught:
            office_convert.preflight(source)

        self.assertEqual(caught.exception.code, "encrypted_document")

    def test_zip_traversal_rejected(self):
        source = self.registry.root / "traversal.docx"
        make_docx(source)

        with zipfile.ZipFile(source, "a") as archive:
            archive.writestr("../evil.bin", b"x")

        with self.assertRaises(office_convert.OfficeError) as caught:
            office_convert.preflight(source)

        self.assertEqual(caught.exception.code, "zip_path_traversal")

    def test_ole_object_rejected(self):
        source = self.registry.root / "ole.docx"
        make_docx(source)

        with zipfile.ZipFile(source, "a") as archive:
            archive.writestr("word/oleObject1.bin", b"x")

        with self.assertRaises(office_convert.OfficeError) as caught:
            office_convert.preflight(source)

        self.assertEqual(caught.exception.code, "unsupported_active_content")

    def test_pdf_size_budget(self):
        with self._patched("MAX_PDF_BYTES", 10):
            with self.assertRaises(office_convert.OfficeError) as caught:
                self.convert()

        self.assertEqual(caught.exception.code, "pdf_too_large")

    def test_nonzero_exit_mapping(self):
        class FailedExecutor(FakeExecutor):
            def execute_command(self, argv, **kwargs):
                return SimpleNamespace(status="failed", error_code="RUNNER_FAILED", duration_ms=1)

        with self.assertRaises(office_convert.OfficeError) as caught:
            self.convert(FailedExecutor(self.root / "n1"))

        self.assertEqual(caught.exception.code, "convert_failed")
        self.assertEqual(self.registry.registered, [])

    def test_timeout_leftover_cleanup_fails_closed(self):
        class Leftover(FakeExecutor):
            def execute_command(self, argv, **kwargs):
                return SimpleNamespace(status="timed_out", error_code="EXEC_TIMED_OUT", duration_ms=1)

            def destroy(self, sandbox_id):
                self.destroyed.append(sandbox_id)

        with self.assertRaises(office_convert.OfficeError) as caught:
            self.convert(Leftover(self.root / "n2"))

        self.assertEqual(caught.exception.code, "sandbox_cleanup_failed")

    def test_convert_failed_destroy_raises(self):
        class Broken(FakeExecutor):
            def execute_command(self, argv, **kwargs):
                return SimpleNamespace(status="failed", error_code="RUNNER_FAILED", duration_ms=1)

            def destroy(self, sandbox_id):
                raise RuntimeError("cannot remove")

        with self.assertRaises(office_convert.OfficeError) as caught:
            self.convert(Broken(self.root / "n3"))

        self.assertEqual(caught.exception.code, "sandbox_cleanup_failed")
        self.assertEqual(self.registry.registered, [])

    def test_error_messages_have_no_paths_or_secrets(self):
        with self.assertRaises(office_convert.OfficeError) as caught:
            office_convert.preflight(self.registry.root / "missing.docx")

        message = str(caught.exception)

        self.assertNotIn("C:\\", message)
        self.assertNotIn("SECRET", message)
        self.assertEqual(message, "path_missing")

    def test_remote_readonly_blocks_post(self):
        import asyncio
        import tempfile

        from src.remote.access_log import AccessLog
        from src.remote.config import RemoteConfig
        from src.remote.guard import RemoteGuard
        from tests.unit.test_remote_guard import TOKEN, call, status_of

        class App:
            async def __call__(self, scope, receive, send):
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await send({"type": "http.response.body", "body": b"ok"})

        with tempfile.TemporaryDirectory() as temp:
            log_path = Path(temp) / "access.jsonl"
            config = RemoteConfig(enabled=True, token=TOKEN, log_path=log_path)
            guard = RemoteGuard(App(), config, log=AccessLog(log_path))
            headers = [(b"authorization", f"Bearer {TOKEN}".encode())]

            blocked = asyncio.run(call(
                guard, "POST", "/api/office/convert", headers=headers
            ))
            allowed = asyncio.run(call(
                guard, "GET", "/api/artifacts/art_1/content", headers=headers
            ))

        self.assertEqual(status_of(blocked), 403)
        self.assertEqual(status_of(allowed), 200)

    def test_no_logs_on_failure(self):
        with self.assertNoLogs("src.core.office_convert", level="DEBUG"):
            with self.assertRaises(office_convert.OfficeError):
                office_convert.preflight(self.registry.root / "missing.docx")

class OfficeApiCase(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        from src.main import app
        from src.auth_api import get_current_user

        self.app = app
        self.client = TestClient(app)
        self._override = app.dependency_overrides.get(get_current_user)
        app.dependency_overrides[get_current_user] = lambda: {"id": "owner-1", "role": "owner"}
        self.addCleanup(self._restore)

    def _restore(self):
        from src.auth_api import get_current_user

        if self._override is None:
            self.app.dependency_overrides.pop(get_current_user, None)
        else:
            self.app.dependency_overrides[get_current_user] = self._override

    def test_bad_body(self):
        response = self.client.post("/api/office/convert", json={})
        self.assertEqual(response.status_code, 400)

    def test_bad_id(self):
        response = self.client.post("/api/office/convert", json={"source_artifact_id": "x"})
        self.assertEqual(response.status_code, 400)

    def test_office_unavailable(self):
        with patch("src.office_api.find_soffice", return_value=None):
            response = self.client.post(
                "/api/office/convert", json={"source_artifact_id": "art_abc"}
            )

        self.assertEqual(response.status_code, 503)

    def test_success(self):
        payload = {
            "artifact_id": "art_123", "title": "a.pdf", "kind": "pdf",
            "media_type": "application/pdf", "size_bytes": 10,
            "sha256": "0" * 64, "page_count": 1, "duration_ms": 5,
            "preview_url": "/api/artifacts/art_123/content?inline=1",
            "download_url": "/api/artifacts/art_123/content",
        }

        with patch("src.office_api.find_soffice", return_value=Path("soffice.com")), patch(
            "src.office_api.convert_artifact",
            return_value=SimpleNamespace(to_dict=lambda: payload),
        ):
            response = self.client.post(
                "/api/office/convert", json={"source_artifact_id": "art_123"}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["artifact_id"], "art_123")

    def test_busy_returns_429(self):
        from src.office_api import _global_lock

        acquired = _global_lock.acquire(blocking=False)
        self.addCleanup(lambda: _global_lock.release() if acquired else None)

        with patch("src.office_api.find_soffice", return_value=Path("soffice.com")):
            response = self.client.post(
                "/api/office/convert", json={"source_artifact_id": "art_123"}
            )

        self.assertEqual(response.status_code, 429)




    def test_unauthenticated_401(self):
        from src.auth_api import get_current_user

        self.app.dependency_overrides.pop(get_current_user, None)

        try:
            response = self.client.post(
                "/api/office/convert", json={"source_artifact_id": "art_123"}
            )
        finally:
            self.app.dependency_overrides[get_current_user] = lambda: {"id": "owner-1", "role": "owner"}

        self.assertEqual(response.status_code, 401)

    def test_per_owner_busy_429(self):
        from src import office_api

        lock = office_api._acquire("owner-1")
        self.addCleanup(lambda: office_api._release("owner-1", lock))

        with patch("src.office_api.find_soffice", return_value=Path("soffice.com")):
            response = self.client.post(
                "/api/office/convert", json={"source_artifact_id": "art_123"}
            )

        self.assertEqual(response.status_code, 429)

    def test_lock_released_after_office_error(self):
        from src.core.office_convert import OfficeError

        with patch("src.office_api.find_soffice", return_value=Path("soffice.com")), patch(
            "src.office_api.convert_artifact", side_effect=OfficeError("bad", 422)
        ):
            first = self.client.post(
                "/api/office/convert", json={"source_artifact_id": "art_123"}
            )

        self.assertEqual(first.status_code, 422)

        payload = {"artifact_id": "art_ok", "title": "a.pdf", "kind": "pdf",
                   "media_type": "application/pdf", "size_bytes": 1, "sha256": "0" * 64,
                   "page_count": 1, "duration_ms": 1,
                   "preview_url": "/x", "download_url": "/y"}

        with patch("src.office_api.find_soffice", return_value=Path("soffice.com")), patch(
            "src.office_api.convert_artifact", return_value=SimpleNamespace(to_dict=lambda: payload)
        ):
            second = self.client.post(
                "/api/office/convert", json={"source_artifact_id": "art_ok"}
            )

        self.assertEqual(second.status_code, 200)

    def test_lock_released_after_unhandled_error(self):
        from fastapi.testclient import TestClient

        from src.main import app

        client = TestClient(app, raise_server_exceptions=False)

        with patch("src.office_api.find_soffice", return_value=Path("soffice.com")), patch(
            "src.office_api.convert_artifact", side_effect=RuntimeError("boom")
        ):
            first = client.post("/api/office/convert", json={"source_artifact_id": "art_123"})

        self.assertEqual(first.status_code, 500)

        payload = {"artifact_id": "art_ok", "title": "a.pdf", "kind": "pdf",
                   "media_type": "application/pdf", "size_bytes": 1, "sha256": "0" * 64,
                   "page_count": 1, "duration_ms": 1,
                   "preview_url": "/x", "download_url": "/y"}

        with patch("src.office_api.find_soffice", return_value=Path("soffice.com")), patch(
            "src.office_api.convert_artifact", return_value=SimpleNamespace(to_dict=lambda: payload)
        ):
            second = client.post("/api/office/convert", json={"source_artifact_id": "art_ok"})

        self.assertEqual(second.status_code, 200)




if __name__ == "__main__":
    unittest.main()
