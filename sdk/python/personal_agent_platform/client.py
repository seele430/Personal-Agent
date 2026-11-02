"""Synchronous Python client for the platform v1 HTTP contract."""

import json
import socket
import time
import uuid

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .errors import (
    ApiError,
    ConflictError,
    GoneError,
    NotFoundError,
    PlatformError,
    ProtocolError,
    RateLimitError,
    RequestTimeoutError,
    TransportError,
    ValidationError,
    WaitTimeoutError,
)
from .models import ExecResult, Sandbox


DEFAULT_TIMEOUT_MS = 30000
DEFAULT_MAX_OUTPUT_BYTES = 65536


@dataclass(frozen=True)
class TransportResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> TransportResponse:
        ...


class UrllibTransport:
    """Dependency-free HTTP transport; HTTP errors remain response data."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> TransportResponse:
        request = Request(
            url,
            data=body,
            headers=dict(headers),
            method=method,
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                return TransportResponse(
                    status_code=response.status,
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except HTTPError as exc:
            return TransportResponse(
                status_code=exc.code,
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=exc.read(),
            )
        except URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise RequestTimeoutError(str(exc.reason)) from exc
            raise TransportError(str(exc.reason)) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise RequestTimeoutError(str(exc)) from exc
        except OSError as exc:
            raise TransportError(str(exc)) from exc


class PlatformClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: Transport | None = None,
        request_timeout_seconds: float = 10.0,
        poll_interval_seconds: float = 0.2,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds cannot be negative")
        self.base_url = base_url.rstrip("/")
        self.transport = transport or UrllibTransport()
        self.request_timeout_seconds = request_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._monotonic = monotonic
        self._sleep = sleeper

    def create(
        self,
        *,
        tier: str,
        workspace_id: str,
        ttl_seconds: int,
        idempotency_key: str | None = None,
    ) -> Sandbox:
        key = (
            uuid.uuid4().hex
            if idempotency_key is None
            else idempotency_key
        )
        payload = self._request(
            "POST",
            "/v1/sandboxes",
            json_body={
                "tier": tier,
                "workspace_id": workspace_id,
                "ttl_seconds": ttl_seconds,
            },
            headers={"Idempotency-Key": key},
        )
        return self._parse(payload, Sandbox.from_dict)

    def exec(
        self,
        sandbox_id: str,
        command: list[str],
        *,
        run_id: str,
        step_id: str,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        wait: bool = True,
        wait_timeout_seconds: float | None = None,
        cancel_on_wait_timeout: bool = True,
    ) -> ExecResult:
        if not command or not all(
            isinstance(part, str) for part in command
        ):
            raise ValueError("command must be a non-empty list of strings")
        self._validate_limits(timeout_ms, max_output_bytes)
        return self._submit_exec(
            f"/v1/sandboxes/{self._id(sandbox_id)}/exec",
            {
                "run_id": run_id,
                "step_id": step_id,
                "timeout_ms": timeout_ms,
                "max_output_bytes": max_output_bytes,
                "command": command,
            },
            wait=wait,
            wait_timeout_seconds=wait_timeout_seconds,
            cancel_on_wait_timeout=cancel_on_wait_timeout,
        )

    def fn(
        self,
        sandbox_id: str,
        name: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
        *,
        run_id: str,
        step_id: str,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        wait: bool = True,
        wait_timeout_seconds: float | None = None,
        cancel_on_wait_timeout: bool = True,
    ) -> ExecResult:
        if not name:
            raise ValueError("name cannot be empty")
        self._validate_limits(timeout_ms, max_output_bytes)
        return self._submit_exec(
            f"/v1/sandboxes/{self._id(sandbox_id)}/fn",
            {
                "name": name,
                "args": args or [],
                "kwargs": kwargs or {},
                "run_id": run_id,
                "step_id": step_id,
                "timeout_ms": timeout_ms,
                "max_output_bytes": max_output_bytes,
            },
            wait=wait,
            wait_timeout_seconds=wait_timeout_seconds,
            cancel_on_wait_timeout=cancel_on_wait_timeout,
        )

    def get_exec(self, exec_id: str) -> ExecResult:
        payload = self._request(
            "GET",
            f"/v1/exec/{self._id(exec_id)}",
        )
        return self._parse(payload, ExecResult.from_dict)

    def cancel(self, exec_id: str) -> ExecResult:
        payload = self._request(
            "POST",
            f"/v1/exec/{self._id(exec_id)}/cancel",
            json_body={},
        )
        return self._parse(payload, ExecResult.from_dict)

    def destroy(self, sandbox_id: str) -> Sandbox:
        payload = self._request(
            "POST",
            f"/v1/sandboxes/{self._id(sandbox_id)}/destroy",
            json_body={},
        )
        return self._parse(payload, Sandbox.from_dict)

    def metrics(self) -> str:
        response = self._raw_request(
            "GET",
            "/metrics",
            headers={"Accept": "text/plain"},
        )
        try:
            return response.body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("Metrics response is not UTF-8") from exc

    def _submit_exec(
        self,
        path: str,
        json_body: dict[str, Any],
        *,
        wait: bool,
        wait_timeout_seconds: float | None,
        cancel_on_wait_timeout: bool,
    ) -> ExecResult:
        if wait_timeout_seconds is not None and wait_timeout_seconds < 0:
            raise ValueError("wait_timeout_seconds cannot be negative")
        accepted = self._parse(
            self._request("POST", path, json_body=json_body),
            ExecResult.from_dict,
        )
        if not wait or accepted.terminal:
            return accepted
        return self._wait_for_terminal(
            accepted,
            wait_timeout_seconds=wait_timeout_seconds,
            cancel_on_wait_timeout=cancel_on_wait_timeout,
        )

    def _wait_for_terminal(
        self,
        accepted: ExecResult,
        *,
        wait_timeout_seconds: float | None,
        cancel_on_wait_timeout: bool,
    ) -> ExecResult:
        deadline = (
            None
            if wait_timeout_seconds is None
            else self._monotonic() + wait_timeout_seconds
        )
        result = accepted
        while not result.terminal:
            if deadline is not None and self._monotonic() >= deadline:
                cancel_result = None
                cancel_error = None
                if cancel_on_wait_timeout:
                    try:
                        cancel_result = self.cancel(result.exec_id)
                    except PlatformError as exc:
                        cancel_error = exc
                raise WaitTimeoutError(
                    result.exec_id,
                    cancel_result=cancel_result,
                    cancel_error=cancel_error,
                )
            self._sleep(self.poll_interval_seconds)
            result = self.get_exec(result.exec_id)
        return result

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        response = self._raw_request(
            method,
            path,
            json_body=json_body,
            headers=headers,
        )
        try:
            return response.json()
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProtocolError("Expected a UTF-8 JSON response") from exc

    def _raw_request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> TransportResponse:
        request_headers = {"Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        body = None
        if json_body is not None:
            request_headers["Content-Type"] = "application/json"
            body = json.dumps(
                json_body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        response = self.transport.request(
            method,
            f"{self.base_url}{path}",
            headers=request_headers,
            body=body,
            timeout_seconds=self.request_timeout_seconds,
        )
        if not 200 <= response.status_code < 300:
            self._raise_api_error(response)
        return response

    @staticmethod
    def _raise_api_error(response: TransportResponse) -> None:
        try:
            payload = response.json()
            error = payload["error"]
            code = error["code"]
            if not isinstance(code, str):
                raise ValueError("error code must be a string")
        except (KeyError, TypeError, UnicodeDecodeError, ValueError):
            code = "INVALID_ERROR_RESPONSE"
            error = {}
        error_type = {
            400: ValidationError,
            404: NotFoundError,
            409: ConflictError,
            410: GoneError,
            429: RateLimitError,
        }.get(response.status_code, ApiError)
        raise error_type(
            code,
            str(error.get("message", "Platform request failed")),
            status_code=response.status_code,
            request_id=error.get("request_id"),
            retryable=error.get("retryable") is True,
        )

    @staticmethod
    def _parse(payload: Any, parser: Callable[[Any], Any]) -> Any:
        try:
            return parser(payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProtocolError("Response is missing required v1 fields") from exc

    @staticmethod
    def _id(value: str) -> str:
        if not value:
            raise ValueError("resource ID cannot be empty")
        return quote(value, safe="")

    @staticmethod
    def _validate_limits(timeout_ms: int, max_output_bytes: int) -> None:
        if (
            isinstance(timeout_ms, bool)
            or not isinstance(timeout_ms, int)
            or not 1 <= timeout_ms <= 300000
        ):
            raise ValueError("timeout_ms must be in 1...300000")
        if (
            isinstance(max_output_bytes, bool)
            or not isinstance(max_output_bytes, int)
            or not 1 <= max_output_bytes <= 1048576
        ):
            raise ValueError("max_output_bytes must be in 1...1048576")
