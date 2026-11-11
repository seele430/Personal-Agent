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


if __name__ == "__main__":
    unittest.main()
