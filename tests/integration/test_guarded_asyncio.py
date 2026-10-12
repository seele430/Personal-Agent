"""Compatibility tests for the guarded runtime and asyncio."""

import asyncio
import socket
import unittest

from contextlib import closing
from pathlib import Path

from scripts.test_safety import guarded_test_runtime


ROOT = Path(__file__).resolve().parents[2]


class TestGuardedAsyncio(unittest.TestCase):

    def test_socketpair_works_inside_guard(self):
        with guarded_test_runtime(ROOT):
            left, right = socket.socketpair()

            with closing(left), closing(right):
                left.sendall(b"ok")
                self.assertEqual(right.recv(2), b"ok")

    def test_asyncio_event_loop_works_inside_guard(self):
        with guarded_test_runtime(ROOT):
            loop = asyncio.new_event_loop()

            try:
                result = loop.run_until_complete(
                    asyncio.sleep(0, result=123)
                )
                self.assertEqual(result, 123)
            finally:
                loop.close()


if __name__ == "__main__":
    unittest.main()