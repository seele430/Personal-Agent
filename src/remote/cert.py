"""自签名证书：给局域网手机访问用。

私钥权限会被收紧（Windows 用 icacls，其它平台 chmod 600）。
**不做**自动信任分发——手机端信任自签名证书是用户手动步骤，文档里写清。
"""

from __future__ import annotations

import ipaddress
import os
import subprocess
import sys

from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_DAYS = 397
KEY_FILENAME = "remote-key.pem"
CERT_FILENAME = "remote-cert.pem"


class CertError(RuntimeError):
    """证书生成失败。"""


def _cryptography():
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

    except ImportError as error:
        raise CertError(
            "未安装 cryptography，无法生成证书："
            "pip install cryptography -i https://mirrors.aliyun.com/pypi/simple/"
        ) from error

    return x509, hashes, serialization, rsa, NameOID


def restrict_permissions(path: Path) -> None:
    """尽力收紧私钥权限；失败不致命，但要如实记录。"""

    try:
        if sys.platform == "win32":
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{os.environ.get('USERNAME','*')}:F"],
                capture_output=True,
                timeout=30,
            )

        else:
            path.chmod(0o600)

    except Exception:  # noqa: BLE001 - 权限收紧失败不应让证书生成失败
        pass


def generate_self_signed(
    *,
    ip: str,
    out_dir: str | Path,
    hostnames: tuple[str, ...] = ("localhost",),
    days: int = DEFAULT_DAYS,
) -> tuple[Path, Path]:
    """生成 cert/key 两个 PEM；返回 (cert, key)。"""

    if not 1 <= int(days) <= 825:
        raise CertError("days 必须在 1..825（浏览器上限）")

    try:
        address = ipaddress.ip_address(ip)

    except ValueError as error:
        raise CertError(f"IP 不合法：{ip}") from error

    x509, hashes, serialization, rsa, NameOID = _cryptography()

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "Personal Agent Remote"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Personal Agent Local"),
        ]
    )

    san = [x509.DNSName(name) for name in hostnames] + [
        x509.IPAddress(address)
    ]

    now = datetime.now(timezone.utc)

    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    target_dir = Path(out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    cert_path = target_dir / CERT_FILENAME
    key_path = target_dir / KEY_FILENAME

    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    restrict_permissions(key_path)

    return cert_path, key_path


def describe(cert_path: str | Path) -> dict:
    """读回证书的基本信息（自检用）。"""

    x509, _hashes, _serialization, _rsa, _NameOID = _cryptography()

    certificate = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())

    try:
        san = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value

    except x509.ExtensionNotFound:
        san = None

    return {
        "subject": certificate.subject.rfc4514_string(),
        "not_before": certificate.not_valid_before_utc.isoformat(),
        "not_after": certificate.not_valid_after_utc.isoformat(),
        "dns": list(san.get_values_for_type(x509.DNSName)) if san else [],
        "ips": [
            str(value) for value in san.get_values_for_type(x509.IPAddress)
        ]
        if san
        else [],
    }
