"""隔离验证 Caddy 反向代理：18443 TLS → 18000 后端。

不安装、不改系统信任、不碰生产 8000/8443；所有临时资源在 finally 清理。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time

from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROXY_PORT = 18443
BACKEND_PORT = 18000
OWNER_ID = "owner-caddy-test"


def port_free(port: int) -> bool:
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
            return True
    except OSError:
        return False


def wait_until(predicate, timeout=30.0, interval=0.3):
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass

        time.sleep(interval)

    return False


def kill_tree(process) -> None:
    if process is None or process.poll() is not None:
        return

    try:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass


def fingerprint(pem: str) -> str:
    der = ssl.PEM_cert_to_DER_cert(pem)

    return hashlib.sha256(der).hexdigest()


def main() -> int:
    caddy = shutil.which("caddy")

    if not caddy:
        print(json.dumps({"error": "caddy_unavailable"}, ensure_ascii=False))
        return 2

    for port in (PROXY_PORT, BACKEND_PORT):
        if not port_free(port):
            print(json.dumps({"error": f"port_busy:{port}"}, ensure_ascii=False))
            return 2

    token = secrets.token_urlsafe(32)
    work = Path(tempfile.mkdtemp(prefix="pa-caddy-verify-"))
    data_dir = work / "data"
    storage = work / "caddy-storage"
    caddyfile = work / "Caddyfile"
    backend_log = work / "backend.log"
    caddy_log = work / "caddy.log"
    access_log = work / "access.jsonl"
    checks: dict[str, bool] = {}
    report: dict = {}
    report_path = work / "report.json"

    os.environ.update({
        "PERSONAL_AGENT_DATA_DIR": str(data_dir),
        "PERSONAL_AGENT_REMOTE": "1",
        "PERSONAL_AGENT_REMOTE_TOKEN": token,
        "PERSONAL_AGENT_REMOTE_OWNER": OWNER_ID,
        "PERSONAL_AGENT_REMOTE_LOG": str(access_log),
        "NO_PROXY": "*",
        "PYTHONUTF8": "1",
    })

    from pypdf import PdfWriter

    from src.artifacts.registry import ArtifactRegistry

    registry = ArtifactRegistry()
    pdf_path = registry.root / "caddy-test.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)

    with pdf_path.open("wb") as handle:
        writer.write(handle)

    artifact = registry.register(OWNER_ID, pdf_path, title="caddy-test.pdf", media_type="application/pdf")
    pdf_bytes = pdf_path.read_bytes()
    pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()

    caddyfile.write_text(
        f"""{{ 
    admin off
    auto_https disable_redirects
    local_certs
    skip_install_trust
    storage file_system {storage.as_posix()}
}}

https://127.0.0.1:{PROXY_PORT} {{
    tls internal
    header Cache-Control "no-store"
    reverse_proxy http://127.0.0.1:{BACKEND_PORT}
}}
""",
        encoding="utf-8",
    )

    validation = subprocess.run(
        [caddy, "validate", "--config", str(caddyfile), "--adapter", "caddyfile"],
        capture_output=True,
        text=True,
    )
    checks["caddy_validate"] = validation.returncode == 0

    backend = None
    caddy_process = None

    try:
        backend = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "src.main:app",
                "--host", "127.0.0.1", "--port", str(BACKEND_PORT),
                "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1",
            "--no-access-log",
            ],
            cwd=str(ROOT),
            env=os.environ.copy(),
            stdout=backend_log.open("wb"),
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        backend_ready = wait_until(
            lambda: requests.get(
                f"http://127.0.0.1:{BACKEND_PORT}/health", timeout=2
            ).status_code == 401
        )
        checks["backend_ready"] = backend_ready

        caddy_process = subprocess.Popen(
            [caddy, "run", "--config", str(caddyfile), "--adapter", "caddyfile"],
            cwd=str(ROOT),
            stdout=caddy_log.open("wb"),
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        proxy_base = f"https://127.0.0.1:{PROXY_PORT}"
        ca_path = storage / "pki" / "authorities" / "local" / "root.crt"
        proxy_ready = wait_until(
            lambda: ca_path.is_file()
            and requests.get(proxy_base + "/health", verify=False, timeout=2).status_code == 401
        )
        checks["proxy_ready"] = proxy_ready

        if not proxy_ready:
            report = {"checks": checks, "error": "proxy_not_ready"}
            return 1

        session = requests.Session()
        session.verify = str(ca_path)
        bearer = {"Authorization": f"Bearer {token}"}

        checks["health_no_token_401"] = session.get(proxy_base + "/health").status_code == 401
        checks["health_wrong_token_401"] = session.get(
            proxy_base + "/health", headers={"Authorization": "Bearer wrong"}
        ).status_code == 401
        health = session.get(proxy_base + "/health", headers=bearer)
        checks["health_bearer_200"] = health.status_code == 200
        checks["artifacts_no_token_401"] = session.get(proxy_base + "/api/artifacts").status_code == 401
        checks["artifacts_bearer_200"] = session.get(
            proxy_base + "/api/artifacts", headers=bearer
        ).status_code == 200
        checks["post_register_403"] = session.post(
            proxy_base + "/api/artifacts/register", json={}, headers=bearer
        ).status_code == 403
        checks["office_get_403"] = session.get(
            proxy_base + "/api/office/convert", headers=bearer
        ).status_code == 403
        checks["artifactsx_403"] = session.get(
            proxy_base + "/api/artifactsx", headers=bearer
        ).status_code == 403

        login = session.get(
            proxy_base + "/api/artifacts", params={"token": token}, allow_redirects=False
        )
        cookie = login.headers.get("Set-Cookie", "")
        checks["token_303"] = login.status_code == 303
        checks["location_no_token"] = login.headers.get("Location", "").find("token") < 0
        checks["cookie_secure"] = "Secure" in cookie
        checks["cookie_httponly"] = "HttpOnly" in cookie
        checks["cookie_samesite"] = "SameSite=Strict" in cookie
        checks["no_referrer"] = login.headers.get("Referrer-Policy", "").lower() == "no-referrer"
        checks["no_store"] = "no-store" in login.headers.get("Cache-Control", "")
        checks["cookie_get_200"] = session.get(
            proxy_base + "/api/artifacts", headers={"Cookie": f"pa_remote={token}"}
        ).status_code == 200

        direct = requests.get(
            f"http://127.0.0.1:{BACKEND_PORT}/api/artifacts", headers=bearer, timeout=10
        )
        proxied = session.get(proxy_base + "/api/artifacts", headers=bearer)
        checks["json_semantics"] = direct.json() == proxied.json()

        direct_pdf = requests.get(
            f"http://127.0.0.1:{BACKEND_PORT}/api/artifacts/{artifact.id}/content?inline=1",
            headers=bearer, timeout=10,
        )
        proxied_pdf = session.get(
            proxy_base + f"/api/artifacts/{artifact.id}/content?inline=1", headers=bearer
        )
        checks["pdf_bytes_equal"] = direct_pdf.content == proxied_pdf.content
        checks["pdf_sha_equal"] = (
            hashlib.sha256(proxied_pdf.content).hexdigest() == pdf_sha
        )
        checks["pdf_content_type"] = proxied_pdf.headers.get("Content-Type", "").startswith("application/pdf")
        checks["pdf_content_disposition"] = "inline" in proxied_pdf.headers.get("Content-Disposition", "").lower()

        preview = session.get(proxy_base + f"/api/artifacts/{artifact.id}/preview", headers=bearer)
        checks["preview_pdf_inline"] = (
            preview.status_code == 200
            and preview.json().get("kind") == "pdf"
            and preview.json().get("mode") == "inline"
        )

        leaf_pem = ssl.get_server_certificate(("127.0.0.1", PROXY_PORT))
        report["tls"] = {
            "root_sha256": fingerprint(ca_path.read_text(encoding="utf-8")),
            "leaf_sha256": fingerprint(leaf_pem),
            "cacert_verify": True,
        }

        secret_values = [token]

        for log_path in (caddy_log, backend_log, access_log):
            text = log_path.read_text(encoding="utf-8", errors="ignore") if log_path.exists() else ""
            checks[f"no_secret_in_{log_path.name}"] = all(
                value not in text for value in secret_values
            )

        report["checks"] = checks
        report["artifact_id"] = artifact.id
        report["caddy_version"] = subprocess.run(
            [caddy, "version"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
    finally:
        kill_tree(caddy_process)
        kill_tree(backend)

        for _ in range(30):
            if port_free(PROXY_PORT) and port_free(BACKEND_PORT):
                break
            time.sleep(0.2)

        report["ports_freed"] = port_free(PROXY_PORT) and port_free(BACKEND_PORT)
        report.setdefault("checks", checks)["ports_freed"] = report["ports_freed"]
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    failed = [name for name, ok in report["checks"].items() if not ok]
    report["failed"] = failed
    report["passed"] = not failed
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))

    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
