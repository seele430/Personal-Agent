"""监控订阅源的 URL 安全策略（SSRF 防护）。

分两层，因为"建任务"和"真正抓取"的风险不一样：

1. 结构校验 `check_url_shape()`：协议、主机名形态、用户信息、字面量 IP。
   建任务时做一次；每次抓取前再做一次。
2. 解析校验 `resolve_public_addresses()`：把域名解析成**全部**地址，
   只要有一个不是公网地址就拒绝。只在抓取前做 —— 建任务时解析没有意义，
   DNS 完全可以在那之后才变化（DNS rebinding 的常见形态）。

诚实边界：解析与真正连接之间仍存在 TOCTOU 窗口，彻底的做法是把已验证的
IP 直接用于连接、用 Host 头承载域名。当前实现选择"解析校验 + 禁止自动
重定向 + 每一跳复检 + 体积上限"，残余风险在此明确标注。

解析函数可注入（`resolver=`），所以单元测试完全不需要联网。
"""

import ipaddress
import os
import socket

from urllib.parse import urlsplit


# 只允许 HTTPS：明文 HTTP 既泄露内容，也更容易被中间人改写。
ALLOWED_SCHEMES = frozenset({"https"})

MAX_URL_BYTES = 2048

# 这些主机名通常指向本机或内网，直接拒绝。
BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
})

BLOCKED_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".home.arpa",
)


class UrlPolicyError(ValueError):
    """URL 不符合抓取策略。

    继承 ValueError：调用方既有的 `except ValueError` 仍然有效。
    """


def is_public_address(value: str) -> bool:
    """判断单个 IP 是否属于可安全访问的公网地址。"""

    if not isinstance(value, str) or not value.strip():
        return False

    # 去掉 IPv6 的 scope id，例如 fe80::1%eth0
    text = value.strip().split("%", 1)[0]

    try:
        address = ipaddress.ip_address(text)

    except ValueError:
        return False

    if not address.is_global:
        return False

    # is_global 之外的兜底：这些段一律不放行。
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def check_url_shape(url: str) -> str:
    """结构性校验，返回小写主机名。

    不联网、不做 DNS 解析，因此可以安全地用在建任务路径上。
    """

    if not isinstance(url, str) or not url.strip():
        raise UrlPolicyError("订阅地址不能为空")

    url = url.strip()

    if len(url.encode("utf-8")) > MAX_URL_BYTES:
        raise UrlPolicyError("订阅地址过长")

    parts = urlsplit(url)

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UrlPolicyError("订阅地址必须使用 HTTPS")

    # https://user:pass@host/ 既可能泄露凭据，
    # 也会让"主机名"在不同解析器下含义不一致。
    if parts.username or parts.password:
        raise UrlPolicyError("订阅地址不能包含用户名或密码")

    host = parts.hostname

    if not host:
        raise UrlPolicyError("订阅地址缺少主机名")

    host = host.lower().rstrip(".")

    if host in BLOCKED_HOSTNAMES:
        raise UrlPolicyError("不允许访问本机或内网主机")

    if host.endswith(BLOCKED_SUFFIXES):
        raise UrlPolicyError("不允许访问本机或内网主机")

    try:
        port = parts.port

    except ValueError:
        raise UrlPolicyError("订阅地址端口不合法") from None

    if port is not None and not 1 <= port <= 65535:
        raise UrlPolicyError("订阅地址端口不合法")

    # 字面量 IP：不需要 DNS，直接判定公网与否。
    try:
        literal = ipaddress.ip_address(host)

    except ValueError:
        literal = None

    if literal is not None and not is_public_address(host):
        raise UrlPolicyError(
            "不允许访问非公网地址（内网 / 环回 / 链路本地）"
        )

    return host


def resolve_public_addresses(
    hostname: str,
    *,
    resolver=None,
) -> list[str]:
    """解析域名并确认**所有**地址都是公网地址。

    resolver 可注入：测试用假解析器，绝不联网。
    默认用 socket.getaddrinfo，拿到的是解析后的真实地址。
    """

    lookup = socket.getaddrinfo if resolver is None else resolver

    try:
        infos = lookup(hostname, None)

    except OSError as error:
        raise UrlPolicyError(
            f"域名解析失败：{hostname}"
        ) from error

    addresses = set()

    for info in infos or []:
        try:
            socket_address = info[4]

        except (IndexError, TypeError):
            continue

        if not socket_address:
            continue

        value = socket_address[0]

        if isinstance(value, str) and value:
            addresses.add(value)

    if not addresses:
        raise UrlPolicyError(
            f"域名没有解析到任何地址：{hostname}"
        )

    for value in sorted(addresses):
        if not is_public_address(value):
            raise UrlPolicyError(
                f"域名解析到非公网地址：{value}"
            )

    return sorted(addresses)


def check_fetch_url(url: str, *, resolver=None) -> str:
    """抓取前的完整校验：结构 + 解析。返回主机名。"""

    host = check_url_shape(url)

    try:
        ipaddress.ip_address(host)

    except ValueError:
        # 不是字面量 IP，必须解析并逐个确认。
        resolve_public_addresses(host, resolver=resolver)

    return host


# 可选的域名白名单环境变量：
# 不设置 = 除 URL 策略外不做额外限制；
# 设置 = 只允许列表内的主机（逗号分隔）。
ALLOWED_HOSTS_ENV = "RSS_ALLOWED_HOSTS"


def configured_allowed_hosts() -> frozenset[str]:
    """读取可选域名白名单。默认为空集（不做额外限制）。"""

    raw = os.getenv(ALLOWED_HOSTS_ENV, "")

    return frozenset(
        item.strip().lower().rstrip(".")
        for item in raw.split(",")
        if item.strip()
    )


def check_host_allowed(
    url: str,
    *,
    allowed: frozenset[str] | None = None,
) -> str:
    """结构校验 + 可选域名白名单（不做 DNS）。

    调度器用它决定"这条任务的数据源是否获准执行"，
    与接口层共用同一份策略，避免两处判断漂移。
    """

    host = check_url_shape(url)

    allow = (
        configured_allowed_hosts()
        if allowed is None
        else allowed
    )

    if allow and host not in allow:
        raise UrlPolicyError(
            f"该域名未在允许列表内：{host}"
        )

    return host


def resolve_for_fetch(
    url: str,
    *,
    resolver=None,
) -> tuple[str, list[str]]:
    """抓取前校验，并返回 (主机名, 已验证地址列表)。

    返回值直接用于"连接固定到已验证 IP"，
    从而消除"校验完再解析一次"的 TOCTOU 窗口。
    """

    host = check_url_shape(url)

    try:
        literal = ipaddress.ip_address(host)

    except ValueError:
        addresses = resolve_public_addresses(
            host, resolver=resolver
        )

        return host, addresses

    # 字面量 IP 已经在 check_url_shape 里判过公网。
    if not is_public_address(str(literal)):
        raise UrlPolicyError(
            "不允许访问非公网地址（内网 / 环回 / 链路本地）"
        )

    return host, [str(literal)]
