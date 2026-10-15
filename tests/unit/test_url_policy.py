"""URL 安全策略（SSRF 防护）单元测试。

完全离线：DNS 解析器由测试注入，不联网、不访问数据库。
"""

import socket
import unittest

from src.monitors.url_policy import (
    UrlPolicyError,
    check_fetch_url,
    check_url_shape,
    is_public_address,
    resolve_public_addresses,
)


def resolver_returning(*addresses):
    """假 DNS：固定返回给定地址。"""

    def resolve(hostname, port=None):
        return [
            (2, 1, 6, "", (value, 0))
            for value in addresses
        ]

    return resolve


def resolver_failing(error):
    def resolve(hostname, port=None):
        raise error

    return resolve


class TestAddressClassification(unittest.TestCase):
    """单个地址是否可安全访问。"""

    def test_internal_addresses_rejected(self):
        for value in (
            "127.0.0.1",
            "10.0.0.1",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.169.254",   # 云元数据服务
            "0.0.0.0",
            "100.64.0.1",        # CGNAT
            "198.18.0.1",
            "::1",
            "fd00::1",
            "fe80::1",
        ):
            with self.subTest(value=value):
                self.assertFalse(is_public_address(value))

    def test_public_addresses_allowed(self):
        for value in ("1.1.1.1", "8.8.8.8", "2606:4700::1111"):
            with self.subTest(value=value):
                self.assertTrue(is_public_address(value))

    def test_ipv4_mapped_internal_rejected(self):
        # ::ffff:127.0.0.1 是绕过 IP 黑名单最常见的写法
        self.assertFalse(
            is_public_address("::ffff:127.0.0.1")
        )
        self.assertFalse(
            is_public_address("::ffff:10.0.0.1")
        )
        self.assertTrue(
            is_public_address("::ffff:8.8.8.8")
        )

    def test_scoped_address_and_garbage(self):
        self.assertFalse(is_public_address("fe80::1%eth0"))
        self.assertFalse(is_public_address("not-an-ip"))
        self.assertFalse(is_public_address(""))
        self.assertFalse(is_public_address(None))


class TestUrlShape(unittest.TestCase):

    def test_public_https_url_allowed(self):
        self.assertEqual(
            check_url_shape(
                "https://blog.python.org/rss.xml"
            ),
            "blog.python.org",
        )

    def test_other_schemes_rejected(self):
        for url in (
            "http://example.com/rss",
            "ftp://example.com/rss",
            "file:///etc/passwd",
            "gopher://example.com/",
        ):
            with self.subTest(url=url):
                with self.assertRaises(UrlPolicyError):
                    check_url_shape(url)

    def test_credentials_in_url_rejected(self):
        with self.assertRaises(UrlPolicyError):
            check_url_shape(
                "https://user:pass@example.com/rss"
            )

    def test_internal_hostnames_rejected(self):
        for url in (
            "https://localhost/rss",
            "https://foo.local/rss",
            "https://svc.internal/rss",
            "https://host.home.arpa/rss",
            "https://a.localhost/rss",
        ):
            with self.subTest(url=url):
                with self.assertRaises(UrlPolicyError):
                    check_url_shape(url)

    def test_literal_internal_ip_rejected(self):
        for url in (
            "https://127.0.0.1/rss",
            "https://169.254.169.254/latest/meta-data",
            "https://[::1]/rss",
        ):
            with self.subTest(url=url):
                with self.assertRaises(UrlPolicyError):
                    check_url_shape(url)

    def test_literal_public_ip_allowed(self):
        self.assertEqual(
            check_url_shape("https://1.1.1.1/rss"),
            "1.1.1.1",
        )

    def test_empty_and_oversized_rejected(self):
        for url in ("", "   ", None, 123):
            with self.subTest(url=url):
                with self.assertRaises(UrlPolicyError):
                    check_url_shape(url)

        with self.assertRaises(UrlPolicyError):
            check_url_shape(
                "https://example.com/" + "a" * 4096
            )

    def test_bad_port_rejected(self):
        with self.assertRaises(UrlPolicyError):
            check_url_shape(
                "https://example.com:99999/rss"
            )

    def test_missing_host_rejected(self):
        with self.assertRaises(UrlPolicyError):
            check_url_shape("https:///rss")


class TestResolution(unittest.TestCase):

    def test_all_public_addresses_returned(self):
        addresses = resolve_public_addresses(
            "example.com",
            resolver=resolver_returning(
                "1.1.1.1", "8.8.8.8"
            ),
        )

        self.assertEqual(addresses, ["1.1.1.1", "8.8.8.8"])

    def test_one_internal_address_rejects_host(self):
        # 多记录里只要有一条内网，整台主机都不能访问。
        with self.assertRaises(UrlPolicyError):
            resolve_public_addresses(
                "example.com",
                resolver=resolver_returning(
                    "1.1.1.1", "10.0.0.5"
                ),
            )

    def test_empty_answer_rejected(self):
        with self.assertRaises(UrlPolicyError):
            resolve_public_addresses(
                "example.com",
                resolver=resolver_returning(),
            )

    def test_lookup_failure_rejected(self):
        with self.assertRaises(UrlPolicyError):
            resolve_public_addresses(
                "example.com",
                resolver=resolver_failing(
                    socket.gaierror("boom")
                ),
            )


class TestFetchUrlCheck(unittest.TestCase):

    def test_domain_is_resolved_and_checked(self):
        called = []

        def resolve(hostname, port=None):
            called.append(hostname)
            return [(2, 1, 6, "", ("10.0.0.1", 0))]

        with self.assertRaises(UrlPolicyError):
            check_fetch_url(
                "https://feed.example.com/rss",
                resolver=resolve,
            )

        self.assertEqual(called, ["feed.example.com"])

    def test_literal_public_ip_skips_resolution(self):
        called = []

        def resolve(hostname, port=None):
            called.append(hostname)
            return []

        host = check_fetch_url(
            "https://1.1.1.1/rss",
            resolver=resolve,
        )

        self.assertEqual(host, "1.1.1.1")
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
