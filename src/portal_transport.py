from __future__ import annotations

import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import urllib.parse
from pathlib import Path


class PortalTransportError(Exception):
    """A transport failure with a safe, allowlisted diagnostic class."""

    def __init__(self, failure_class: str, retryable: bool):
        super().__init__(failure_class)
        self.failure_class = failure_class
        self.retryable = retryable


def _curl_transport_failure(returncode: int) -> PortalTransportError:
    # Classify curl failures without exposing stderr, which may contain request details.
    classes = {
        6: ("DNS_ERROR", True),
        7: ("CONNECT_ERROR", True),
        28: ("TIMEOUT", True),
        35: ("TLS_ERROR", False),
        51: ("TLS_CERTIFICATE_ERROR", False),
        52: ("CONNECTION_CLOSED", True),
        55: ("CONNECTION_SEND_ERROR", True),
        56: ("CONNECTION_RESET", True),
        60: ("TLS_CERTIFICATE_ERROR", False),
        77: ("TLS_CERTIFICATE_ERROR", False),
    }
    failure_class, retryable = classes.get(returncode, ("CURL_TRANSPORT_ERROR", False))
    return PortalTransportError(failure_class, retryable)


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


class PortalSession:
    def __init__(
        self,
        timeout: float = 60,
        *,
        base_url: str | None = None,
        login: str | None = None,
        password: str | None = None,
    ):
        self.timeout = timeout
        self.requests = 0
        self.auth_get_count = 0
        self.auth_post_count = 0
        self.cookie_path: Path | None = None
        self.base_url = (base_url or _required("PORTAL_BASE_URL")).rstrip("/")
        self.last_effective_url: str | None = None
        self.last_retry_after: str | None = None
        self.login_value = login if login is not None else _required("PORTAL_LOGIN")
        self.password_value = password if password is not None else _required("PORTAL_PASSWORD")
        self.curl = shutil.which("curl")
        if not self.curl:
            raise RuntimeError("curl is required on PATH")

    def _base_args(self) -> list[str]:
        if not self.cookie_path:
            raise RuntimeError("Portal session cookie jar is not initialized")
        return [
            self.curl,
            "--http1.1",
            "--silent",
            "--show-error",
            "--fail",
            "--location",
            "--connect-timeout",
            str(self.timeout),
            "--max-time",
            str(self.timeout),
            "--cookie",
            str(self.cookie_path),
            "--cookie-jar",
            str(self.cookie_path),
        ]

    def login(self) -> None:
        handle = tempfile.NamedTemporaryFile(prefix="portal-cookie-", suffix=".txt", delete=False)
        self.cookie_path = Path(handle.name)
        handle.close()
        base = self._base_args()
        subprocess.run(base + ["--output", "/dev/null", self.base_url + "/"], check=True)
        self.auth_get_count += 1
        form = urllib.parse.urlencode(
            {"_login": self.login_value, "_password": self.password_value, "_enter": "1"}
        )
        subprocess.run(
            base
            + [
                "--header",
                "Content-Type: application/x-www-form-urlencoded",
                "--data-binary",
                "@-",
                "--output",
                "/dev/null",
                self.base_url + "/",
            ],
            input=form,
            text=True,
            check=True,
        )
        self.auth_post_count += 1

    def request(
        self,
        path: str,
        method: str = "GET",
        data: dict[str, str] | None = None,
        accept: str = "text/html",
        follow_redirects: bool = True,
    ) -> tuple[int, str, bytes]:
        if not self.cookie_path:
            raise PortalTransportError("SESSION_NOT_AUTHENTICATED", False)
        with tempfile.NamedTemporaryFile() as body, tempfile.NamedTemporaryFile() as headers:
            args = [arg for arg in self._base_args() if arg != "--location"] + [
                "--header",
                f"Accept: {accept}",
                "--output",
                body.name,
                "--dump-header",
                headers.name,
                "--write-out",
                "%{http_code}\n%{url_effective}",
            ]
            if follow_redirects:
                args.append("--location")
            if method == "POST":
                args += [
                    "--request",
                    "POST",
                    "--header",
                    "Content-Type: application/x-www-form-urlencoded",
                    "--data-binary",
                    urllib.parse.urlencode(data or {}, doseq=True),
                ]
            args.append(self.base_url + path)
            self.last_retry_after = None
            self.requests += 1
            try:
                result = subprocess.run(args, capture_output=True, text=True)
            except subprocess.TimeoutExpired as exc:
                raise PortalTransportError("TIMEOUT", True) from exc
            except OSError as exc:
                if isinstance(exc, socket.gaierror):
                    retryable = exc.errno == getattr(socket, "EAI_AGAIN", object())
                    raise PortalTransportError("DNS_ERROR", retryable) from exc
                if isinstance(exc, ssl.SSLError):
                    raise PortalTransportError("TLS_ERROR", False) from exc
                if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
                    raise PortalTransportError("CONNECTION_RESET", True) from exc
                if isinstance(exc, TimeoutError):
                    raise PortalTransportError("TIMEOUT", True) from exc
                if isinstance(exc, ConnectionError):
                    raise PortalTransportError("CONNECT_ERROR", True) from exc
                raise PortalTransportError("LOCAL_TRANSPORT_ERROR", False) from exc
            output_lines = result.stdout.splitlines()
            status_text = output_lines[0].strip() if output_lines else ""
            status = int(status_text[-3:]) if status_text[-3:].isdigit() else 0
            self.last_effective_url = output_lines[1].strip() if len(output_lines) > 1 else None
            header_text = Path(headers.name).read_text(encoding="iso-8859-1", errors="replace")
            content_type = next(
                (
                    line.split(":", 1)[1].strip()
                    for line in header_text.splitlines()
                    if line.lower().startswith("content-type:")
                ),
                "",
            )
            self.last_retry_after = next(
                (line.split(":", 1)[1].strip() for line in header_text.splitlines()
                 if line.lower().startswith("retry-after:")),
                None,
            )
            if result.returncode and status == 0:
                raise _curl_transport_failure(result.returncode)
            return status, content_type, Path(body.name).read_bytes()

    def close(self) -> None:
        if self.cookie_path:
            self.cookie_path.unlink(missing_ok=True)
            self.cookie_path = None
