"""Personal Agent Python test runtime safety guard.

Allows:
- In-memory SQLite
- SQLite files inside the system temporary directory
- Python's internal socketpair communication

Blocks:
- SQLite files inside the project
- SQLite files outside the temporary directory
- Ordinary TCP/UDP connections and DNS lookups

This is not an operating-system sandbox.
"""

import os
import socket
import sqlite3
import tempfile

from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit


class TestSafetyViolation(RuntimeError):
    """A prohibited operation was attempted during a test."""


# Keep real functions from module import time.
# This also helps when a guarded test opens a nested guard.
_REAL_SQLITE_CONNECT = sqlite3.connect
_REAL_SOCKET_CONNECT = socket.socket.connect
_REAL_SOCKETPAIR = socket.socketpair


# Permission only exists during the standard-library
# socketpair() call in the current execution context.
_inside_socketpair = ContextVar(
    "personal_agent_test_socketpair",
    default=False,
)


def _is_inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _sqlite_file_path(database):
    """Return the resolved SQLite file path, or None for memory."""

    if not isinstance(
        database,
        (str, bytes, os.PathLike),
    ):
        raise TestSafetyViolation(
            "Unsupported SQLite database path type"
        )

    name = os.fsdecode(os.fspath(database))

    if name in ("", ":memory:"):
        return None

    if name.startswith("file:"):
        parsed = urlsplit(name)

        if parsed.netloc not in ("", "localhost"):
            raise TestSafetyViolation(
                "Remote SQLite URI is prohibited"
            )

        options = parse_qs(parsed.query)

        if options.get("mode") == ["memory"]:
            return None

        filename = unquote(parsed.path)

        if filename == ":memory:":
            return None

        # Handle Windows file:///C:/path.db format.
        if (
            os.name == "nt"
            and len(filename) >= 3
            and filename[0] == "/"
            and filename[2] == ":"
        ):
            filename = filename[1:]
    else:
        filename = name

    return Path(filename).resolve()


def _is_internal_loopback(address) -> bool:
    """Accept only loopback addresses during socketpair setup."""

    if not isinstance(address, tuple) or len(address) < 2:
        return False

    return address[0] in ("127.0.0.1", "::1")


@contextmanager
def guarded_test_runtime(
    project_root: str | Path | None = None,
):
    """Temporarily protect SQLite and Python socket operations."""

    if project_root is None:
        project_root = Path(__file__).resolve().parents[1]

    project_root = Path(project_root).resolve()
    temporary_root = Path(tempfile.gettempdir()).resolve()

    def checked_sqlite_connect(database, *args, **kwargs):
        path = _sqlite_file_path(database)

        if path is not None:
            if _is_inside(path, project_root):
                raise TestSafetyViolation(
                    f"Test cannot access project database: {path}"
                )

            if not _is_inside(path, temporary_root):
                raise TestSafetyViolation(
                    f"Test SQLite path must be temporary: {path}"
                )

        return _REAL_SQLITE_CONNECT(
            database,
            *args,
            **kwargs,
        )

    def blocked_network(*args, **kwargs):
        raise TestSafetyViolation(
            "Real network access is blocked during tests"
        )

    def checked_connect(sock, address):
        # Windows may implement socket.socketpair() using
        # a short-lived loopback TCP connection.
        #
        # Only that specific internal construction is allowed.
        # An ordinary localhost connection stays blocked.
        if (
            _inside_socketpair.get()
            and _is_internal_loopback(address)
        ):
            return _REAL_SOCKET_CONNECT(sock, address)

        raise TestSafetyViolation(
            "Real TCP connection is blocked during tests"
        )

    def safe_socketpair(*args, **kwargs):
        token = _inside_socketpair.set(True)

        try:
            return _REAL_SOCKETPAIR(*args, **kwargs)
        finally:
            _inside_socketpair.reset(token)

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                sqlite3,
                "connect",
                checked_sqlite_connect,
            )
        )

        stack.enter_context(
            patch.object(
                sqlite3.dbapi2,
                "connect",
                checked_sqlite_connect,
            )
        )

        stack.enter_context(
            patch.object(
                socket.socket,
                "connect",
                checked_connect,
            )
        )

        stack.enter_context(
            patch.object(
                socket.socket,
                "connect_ex",
                blocked_network,
            )
        )

        stack.enter_context(
            patch.object(
                socket.socket,
                "sendto",
                blocked_network,
            )
        )

        stack.enter_context(
            patch.object(
                socket,
                "getaddrinfo",
                blocked_network,
            )
        )

        # Fix for Windows asyncio ProactorEventLoop.
        stack.enter_context(
            patch.object(
                socket,
                "socketpair",
                safe_socketpair,
            )
        )

        yield