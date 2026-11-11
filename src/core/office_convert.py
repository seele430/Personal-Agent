"""Office → PDF 转换：OOXML 预检 + Tier1 执行 + PDF 验证 + Artifact 登记。

边界（v1.1）：
- 只接受 ArtifactRegistry 中当前 owner 的 docx/xlsx/pptx；
- 拒绝加密、宏、外链、OLE/ActiveX；
- 转换在 Tier1 sandbox 内执行，120 s 超时，进程树由平台回收；
- PDF 必须可被 pypdf 解析且 ≥1 页；
- 输出登记到 ArtifactRegistry，不返回宿主绝对路径。
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import subprocess
import uuid
import xml.etree.ElementTree as ET
import zipfile

from dataclasses import dataclass
from pathlib import Path

from src.artifacts.registry import ArtifactForbidden, ArtifactNotFound


SOFFICE_CANDIDATES = (
    r"C:\Program Files\LibreOffice\program\soffice.com",
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.com",
)
ALLOWED_EXTENSIONS = {".docx": "word", ".xlsx": "spreadsheet", ".pptx": "presentation"}
CONTENT_TYPE_MARKERS = {
    "word": ("wordprocessingml.document.main+xml",),
    "spreadsheet": ("spreadsheetml.sheet.main+xml",),
    "presentation": ("presentationml.presentation.main+xml",),
}
MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_ZIP_ENTRIES = 2000
MAX_UNCOMPRESSED = 100 * 1024 * 1024
MAX_ENTRY_UNCOMPRESSED = 50 * 1024 * 1024
MAX_PDF_BYTES = 50 * 1024 * 1024
TIMEOUT_MS = 120_000
REPARSE_FLAG = 0x400


class OfficeError(ValueError):
    def __init__(self, code: str, status: int, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class OfficeResult:
    artifact_id: str
    title: str
    media_type: str
    size_bytes: int
    sha256: str
    page_count: int
    duration_ms: int

    def to_dict(self) -> dict:
        return {
            "artifact_id": self.artifact_id,
            "title": self.title,
            "kind": "pdf",
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "page_count": self.page_count,
            "duration_ms": self.duration_ms,
            "preview_url": f"/api/artifacts/{self.artifact_id}/content?inline=1",
            "download_url": f"/api/artifacts/{self.artifact_id}/content",
        }


def find_soffice() -> Path | None:
    for candidate in SOFFICE_CANDIDATES:
        path = Path(candidate)

        if path.is_file():
            return path

    found = shutil.which("soffice") or shutil.which("soffice.com")

    return Path(found) if found else None


def soffice_version(soffice: Path, *, timeout: float = 10.0) -> str | None:
    try:
        completed = subprocess.run(
            [str(soffice), "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    text = (completed.stdout or completed.stderr or "").strip()

    return text.splitlines()[0] if text else None


def _reject_links(path: Path) -> None:
    for current in [path, *path.parents]:
        try:
            stats = os.lstat(current)
        except OSError:
            raise OfficeError("path_missing", 422) from None

        attributes = getattr(stats, "st_file_attributes", 0)

        if stat.S_ISLNK(stats.st_mode) or attributes & REPARSE_FLAG:
            raise OfficeError("path_link_rejected", 422)


def _has_external_relationship(archive: zipfile.ZipFile, names: set[str]) -> bool:
    for name in names:
        if not name.endswith(".rels"):
            continue

        try:
            root = ET.fromstring(archive.read(name))
        except ET.ParseError:
            raise OfficeError("invalid_relationship_xml", 422) from None

        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1] != "Relationship":
                continue

            if (node.get("TargetMode") or "").strip().lower() == "external":
                return True

    return False


def _preflight_zip(source: Path, ext: str) -> None:
    kind = ALLOWED_EXTENSIONS[ext]

    try:
        with zipfile.ZipFile(source) as archive:
            items = archive.infolist()

            if len(items) > MAX_ZIP_ENTRIES:
                raise OfficeError("zip_too_many_entries", 413)

            total = 0
            names = set()

            for item in items:
                name = item.filename.replace("\\", "/")

                if name in names:
                    raise OfficeError("zip_duplicate_entry", 422)

                names.add(name)

                if name.startswith("/") or ".." in Path(name).parts:
                    raise OfficeError("zip_path_traversal", 422)

                if item.flag_bits & 0x1:
                    raise OfficeError("encrypted_document", 415)

                if item.file_size > MAX_ENTRY_UNCOMPRESSED:
                    raise OfficeError("zip_entry_too_large", 413)

                total += item.file_size

                if total > MAX_UNCOMPRESSED:
                    raise OfficeError("zip_uncompressed_too_large", 413)

                lowered = name.lower()

                if "vbaproject.bin" in lowered or "oleobject" in lowered or "activex" in lowered:
                    raise OfficeError("unsupported_active_content", 415)

            content_types = ""

            if "[Content_Types].xml" in names:
                content_types = archive.read("[Content_Types].xml").decode("utf-8", "ignore").lower()

            if "macroenabled" in content_types or "vba" in content_types:
                raise OfficeError("macro_not_supported", 415)

            if not any(marker in content_types for marker in CONTENT_TYPE_MARKERS[kind]):
                raise OfficeError("content_type_mismatch", 415)

            if _has_external_relationship(archive, names):
                raise OfficeError("external_relationship", 415)

    except zipfile.BadZipFile:
        raise OfficeError("invalid_ooxml", 422) from None


def preflight(source: Path) -> str:
    ext = source.suffix.lower()

    if ext not in ALLOWED_EXTENSIONS:
        raise OfficeError("unsupported_extension", 415)

    _reject_links(source)

    if not source.is_file():
        raise OfficeError("source_not_file", 422)

    size = source.stat().st_size

    if size <= 0:
        raise OfficeError("empty_source", 413)

    if size > MAX_INPUT_BYTES:
        raise OfficeError("source_too_large", 413)

    _preflight_zip(source, ext)

    return ext


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def validate_pdf(path: Path) -> tuple[int, str, int]:
    from pypdf import PdfReader

    _reject_links(path)

    if not path.is_file():
        raise OfficeError("pdf_missing", 422)

    size = path.stat().st_size

    if size <= 0:
        raise OfficeError("pdf_empty", 422)

    if size > MAX_PDF_BYTES:
        raise OfficeError("pdf_too_large", 413)

    data = path.read_bytes()

    if not data.startswith(b"%PDF-"):
        raise OfficeError("pdf_bad_header", 422)

    if b"%%EOF" not in data[-2048:]:
        raise OfficeError("pdf_missing_eof", 422)

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = len(reader.pages)
    except Exception:  # noqa: BLE001 - 第三方解析异常统一失败
        raise OfficeError("pdf_unreadable", 422) from None

    if pages < 1:
        raise OfficeError("pdf_no_pages", 422)

    return size, hashlib.sha256(data).hexdigest(), pages


def _ensure_source_unchanged(source: Path, expected: str) -> None:
    try:
        current = _sha256(source)
    except OSError:
        raise OfficeError("source_modified", 500) from None

    if current != expected:
        raise OfficeError("source_modified", 500)


def _cleanup_sandbox(executor, sandbox_id: str) -> None:
    try:
        executor.destroy(sandbox_id)
    except Exception:
        raise OfficeError("sandbox_cleanup_failed", 500) from None

    if (Path(executor.root) / sandbox_id).exists():
        raise OfficeError("sandbox_cleanup_failed", 500)


def convert_artifact(
    *,
    source_artifact_id: str,
    owner_id: str,
    registry,
    executor,
    soffice: Path | None = None,
) -> OfficeResult:
    soffice = soffice or find_soffice()

    if soffice is None or not soffice.is_file():
        raise OfficeError("office_unavailable", 503)

    try:
        registry.get(source_artifact_id, owner_id=owner_id)
        source = registry.absolute_path(source_artifact_id, owner_id=owner_id)
    except ArtifactNotFound:
        raise OfficeError("artifact_not_found", 404) from None
    except ArtifactForbidden:
        raise OfficeError("artifact_forbidden", 403) from None

    ext = preflight(source)
    source_hash = _sha256(source)
    sandbox_id = executor.open_sandbox()
    cleaned = False
    registered = False
    office_root = None

    try:
        workspace = Path(executor.root) / sandbox_id / "workspace"
        profile = workspace / "profile"
        out_dir = workspace / "out"
        profile.mkdir(exist_ok=True)
        out_dir.mkdir(exist_ok=True)
        input_path = workspace / f"input{ext}"
        shutil.copyfile(source, input_path)

        if _sha256(input_path) != source_hash:
            raise OfficeError("sandbox_copy_mismatch", 500)

        argv = [
            str(soffice),
            "--headless", "--nologo", "--nodefault",
            "--nofirststartwizard", "--norestore",
            f"-env:UserInstallation={profile.resolve().as_uri()}",
            "--convert-to", "pdf",
            "--outdir", str(out_dir),
            str(input_path),
        ]
        outcome = executor.execute_command(
            argv,
            timeout_ms=TIMEOUT_MS,
            max_output_bytes=65536,
            sandbox_id=sandbox_id,
        )
        status = getattr(outcome, "status", "")

        if getattr(outcome, "error_code", None) == "RUNNER_CLEANUP_FAILED":
            raise OfficeError("cleanup_failed", 500)

        if status == "timed_out":
            raise OfficeError("timeout", 504)

        if status != "succeeded":
            raise OfficeError("convert_failed", 422)

        entries = list(out_dir.iterdir())

        if {item.name for item in entries} != {"input.pdf"}:
            raise OfficeError("unexpected_output", 422)

        pdf = out_dir / "input.pdf"
        size, digest, pages = validate_pdf(pdf)
        _ensure_source_unchanged(source, source_hash)

        office_root = registry.root / "office" / uuid.uuid4().hex
        office_root.mkdir(parents=True, exist_ok=False)
        target = office_root / "converted.pdf"
        temporary = office_root / "converted.pdf.tmp"
        shutil.copyfile(pdf, temporary)

        if _sha256(temporary) != digest:
            raise OfficeError("pdf_copy_mismatch", 500)

        _cleanup_sandbox(executor, sandbox_id)
        cleaned = True
        temporary.replace(target)

        try:
            artifact = registry.register(
                owner_id,
                target,
                title=f"{Path(source.name).stem}.pdf",
                media_type="application/pdf",
            )
        except Exception:
            raise OfficeError("artifact_register_failed", 500) from None

        registered = True

        return OfficeResult(
            artifact_id=artifact.id,
            title=artifact.title,
            media_type=artifact.media_type,
            size_bytes=size,
            sha256=digest,
            page_count=pages,
            duration_ms=int(getattr(outcome, "duration_ms", 0) or 0),
        )
    except Exception:
        try:
            _ensure_source_unchanged(source, source_hash)
        except OfficeError as source_error:
            raise source_error from None

        raise
    finally:
        if not cleaned:
            try:
                executor.destroy(sandbox_id)
            except Exception:
                pass

        if office_root is not None and not registered:
            shutil.rmtree(office_root, ignore_errors=True)
