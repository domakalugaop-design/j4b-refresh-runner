"""One-shot, aggregate-only /action diagnostic; run only on the DFB host."""

from __future__ import annotations

import importlib.util
import json
import os
import sys

REPO = "/home/maxis/j4b-refresh-deployment/repo"
CREDENTIALS = "/home/maxis/j4b-refresh-deployment/credentials/portal-auth.json"
BASE_URL = "https://lk.j4b.ru"
STATES = (0, 5, 10, 15, 20, 25, 30, 35, 37, 38, 39, 40, 45, 50)
sys.path.insert(1, REPO)

from src.portal_transport import PortalSession, PortalTransportError

_DIAGNOSTIC_PATH = os.path.join(os.path.dirname(__file__), "action_search_diagnostic.py")
_SPEC = importlib.util.spec_from_file_location("j4b_action_search_diagnostic", _DIAGNOSTIC_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("diagnostic module unavailable")
_DIAGNOSTIC = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _DIAGNOSTIC
_SPEC.loader.exec_module(_DIAGNOSTIC)
render_comparison = _DIAGNOSTIC.render_comparison
summarize_action_response = _DIAGNOSTIC.summarize_action_response


def _quiet_login(session: PortalSession) -> None:
    """Suppress curl diagnostics from auth subprocess without altering its flow."""
    saved_out, saved_err = os.dup(1), os.dup(2)
    null_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null_fd, 1)
        os.dup2(null_fd, 2)
        session.login()
    finally:
        os.dup2(saved_out, 1)
        os.dup2(saved_err, 2)
        os.close(saved_out)
        os.close(saved_err)
        os.close(null_fd)


def main() -> int:
    session = None
    try:
        with open(CREDENTIALS, "r", encoding="utf-8") as stream:
            config = json.load(stream)
        session = PortalSession(
            timeout=45,
            base_url=BASE_URL,
            login=config["login"],
            password=config["password"],
        )
        _quiet_login(session)
        base_payload = {
            "user": "",
            "place": "",
            "limit": "10000",
            "send": "send",
        }
        base_payload.update({f"state[{code}]": "on" for code in STATES})
        summaries = []
        for day in ("2026-10-01", "2026-10-02"):
            payload = {**base_payload, "dt1": day, "dt2": day}
            status, content_type, body = session.request(
                "/action", "POST", payload, accept="text/html", follow_redirects=True
            )
            summary = summarize_action_response(
                http_status=status, content_type=content_type, body=body,
                final_url=session.last_effective_url, expected_host="lk.j4b.ru", limit=10000,
            )
            summaries.append(summary)
            body = b""
            if summary.failure_category != "NONE" or not summary.html_parsed:
                break
        if len(summaries) == 2:
            print(render_comparison(summaries[0], summaries[1]), flush=True)
        else:
            print(render_comparison(summaries[0], summaries[0]), flush=True)
        return 0
    except PortalTransportError as exc:
        # PortalTransportError.failure_class is an allowlisted internal code.
        print(json.dumps({"failure_category": exc.failure_class}, separators=(",", ":")), flush=True)
        return 0
    except Exception:
        print('{"failure_category":"DIAGNOSTIC_INTERNAL_ERROR"}', flush=True)
        return 0
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    raise SystemExit(main())
