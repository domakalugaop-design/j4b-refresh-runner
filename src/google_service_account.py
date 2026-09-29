from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2 import service_account

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"


def _service_account_info() -> dict[str, Any]:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        credential_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON_FILE", "").strip()
        if credential_path:
            path = Path(credential_path).expanduser()
            try:
                mode = stat.S_IMODE(path.stat().st_mode)
                if mode & 0o077:
                    raise RuntimeError(
                        "Google service-account file permissions must not allow group/other access"
                    )
                raw = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise RuntimeError(
                    "Google service-account file could not be read"
                ) from exc
    if not raw:
        raise RuntimeError(
            "set GOOGLE_SERVICE_ACCOUNT_JSON or GOOGLE_SERVICE_ACCOUNT_JSON_FILE"
        )

    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON"
        ) from exc

    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not a service-account credential"
        )

    return info


def google_token() -> str:
    credentials = service_account.Credentials.from_service_account_info(
        _service_account_info(),
        scopes=[SHEETS_SCOPE],
    )
    credentials.refresh(Request())

    if not credentials.token:
        raise RuntimeError(
            "Google service account did not return an access token"
        )

    return credentials.token
