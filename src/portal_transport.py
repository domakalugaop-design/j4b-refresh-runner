from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import urllib.parse
from pathlib import Path


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


class PortalSession:
    def __init__(self, timeout: float = 60):
        self.timeout = timeout
        self.requests = 0
        self.cookie_path: Path | None = None
        self.base_url = _required("PORTAL_BASE_URL").rstrip("/")
        self.login_value = _required("PORTAL_LOGIN")
        self.password_value = _required("PORTAL_PASSWORD")
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

    def request(
        self,
        path: str,
        method: str = "GET",
        data: dict[str, str] | None = None,
        accept: str = "text/html",
    ) -> tuple[int, str, bytes]:
        if not self.cookie_path:
            raise RuntimeError("Portal session is not authenticated")
        with tempfile.NamedTemporaryFile() as body, tempfile.NamedTemporaryFile() as headers:
            args = self._base_args() + [
                "--header",
                f"Accept: {accept}",
                "--output",
                body.name,
                "--dump-header",
                headers.name,
                "--write-out",
                "%{http_code}",
            ]
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
            result = subprocess.run(args, capture_output=True, text=True)
            self.requests += 1
            status_text = result.stdout.strip()
            status = int(status_text[-3:]) if status_text[-3:].isdigit() else 0
            header_text = Path(headers.name).read_text(encoding="iso-8859-1", errors="replace")
            content_type = next(
                (
                    line.split(":", 1)[1].strip()
                    for line in header_text.splitlines()
                    if line.lower().startswith("content-type:")
                ),
                "",
            )
            if result.returncode and status == 0:
                raise RuntimeError(f"Portal request failed via curl ({result.returncode})")
            return status, content_type, Path(body.name).read_bytes()

    def close(self) -> None:
        if self.cookie_path:
            self.cookie_path.unlink(missing_ok=True)
            self.cookie_path = None
