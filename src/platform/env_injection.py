"""命令 exec 的受控环境变量注入。

只允许白名单里的键（DSH 在沙箱里需要的凭据与 profile 路径）。
值只随内存队列项和 runner 的 stdin 载荷传递：**不落库、不进 argv**。

宿主（``exec_service`` / ``ops``）与 runner（``tier1_runner``）各自
独立校验同一份白名单 —— runner 不信任宿主，防止构造载荷绕过宿主。
"""

from __future__ import annotations

ENV_INJECTION_ALLOWLIST = (
    "DEEPSEEK_API_KEY",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "NO_BROWSER",
    "NO_PROXY",
)


def validate_env_injection(env: dict | None) -> dict[str, str]:
    """校验并清洗待注入的环境变量；非法键/值一律 ValueError。"""

    if env is None:
        return {}

    if not isinstance(env, dict):
        raise ValueError("env 必须是 dict[str, str]")

    cleaned: dict[str, str] = {}

    for key, value in env.items():
        if key not in ENV_INJECTION_ALLOWLIST:
            raise ValueError(f"环境变量 {key!r} 不在注入白名单")

        if not isinstance(value, str) or not value:
            raise ValueError(f"环境变量 {key} 的值必须是非空字符串")

        if "\x00" in value:
            raise ValueError(f"环境变量 {key} 的值含 NUL")

        cleaned[key] = value

    return cleaned
