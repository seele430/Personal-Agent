"""生成局域网自签名证书（v1.2 手机只读预览用）。

    .\\.venv\\Scripts\\python.exe scripts/make_lan_cert.py --ip 10.224.76.60 --out pa-v12-evidence-20261107/tls

只生成证书与私钥；**手机端信任证书是手动步骤**，脚本不代做。
"""

from __future__ import annotations

import argparse
import json
import sys

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.remote.cert import CertError, describe, generate_self_signed  # noqa: E402


DEFAULT_OUT = (
    Path(r"C:\Users\123\Documents\deepseek-harness\default-workspace")
    / "pa-v12-evidence-20261107"
    / "tls"
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成远程访问自签名证书")
    parser.add_argument("--ip", required=True, help="局域网 IP（写进 SAN）")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--hostname", action="append", default=[])
    parser.add_argument("--days", type=int, default=397)
    args = parser.parse_args(argv)

    hostnames = tuple(args.hostname or ["localhost"])

    try:
        cert, key = generate_self_signed(
            ip=args.ip, out_dir=args.out, hostnames=hostnames, days=args.days
        )

    except CertError as error:
        print(f"失败：{error}")

        return 1

    info = describe(cert)
    info["cert"] = str(cert)
    info["key"] = str(key)

    print(json.dumps(info, ensure_ascii=False, indent=2))
    print()
    print("用这个证书起服务：")
    print(f"  PERSONAL_AGENT_REMOTE=1 PERSONAL_AGENT_REMOTE_TOKEN=<32+字符> \\")
    print(f"  PERSONAL_AGENT_REMOTE_CERT={cert} PERSONAL_AGENT_REMOTE_KEY={key} \\")
    print("  uvicorn src.main:app --host 0.0.0.0 --port 8443 --ssl-keyfile <key> --ssl-certfile <cert>")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
