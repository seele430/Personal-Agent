"""监听地址解析：回环 / 非回环判定。"""

from __future__ import annotations

import unittest

from src.platform.netcheck import exposed, is_loopback, parse_netstat

SAMPLE = """
  TCP    0.0.0.0:8000           0.0.0.0:0              LISTENING       1111
  TCP    127.0.0.1:8000         0.0.0.0:0              LISTENING       2222
  TCP    [::]:8000              [::]:0                 LISTENING       3333
  TCP    [::1]:8000             [::]:0                 LISTENING       4444
  TCP    192.168.1.5:5432       0.0.0.0:0              LISTENING       5555
  UDP    0.0.0.0:500            *:*                                    6666
"""


class NetcheckTest(unittest.TestCase):
    def test_is_loopback(self):
        self.assertTrue(is_loopback("127.0.0.1"))
        self.assertTrue(is_loopback("127.1.2.3"))
        self.assertTrue(is_loopback("[::1]"))
        self.assertFalse(is_loopback("0.0.0.0"))
        self.assertFalse(is_loopback("[::]"))
        self.assertFalse(is_loopback("192.168.1.5"))

    def test_parse_only_listening(self):
        listeners = parse_netstat(SAMPLE)

        self.assertEqual(len(listeners), 5)
        self.assertTrue(all(item.state == "LISTENING" for item in listeners))

    def test_exposed_filters_loopback(self):
        listeners = parse_netstat(SAMPLE)
        risky = exposed(listeners, port=8000)

        addresses = sorted(item.address for item in risky)

        self.assertEqual(addresses, ["0.0.0.0", "[::]"])

    def test_exposed_without_port(self):
        listeners = parse_netstat(SAMPLE)

        self.assertEqual(len(exposed(listeners)), 3)


if __name__ == "__main__":
    unittest.main()
