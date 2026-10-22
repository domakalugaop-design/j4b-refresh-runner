"""One-page, aggregate-only probe of a project page on the DFB host."""

from __future__ import annotations

import importlib.util
import json
import os
import sys

REPO = "/home/maxis/j4b-refresh-deployment/repo"
CREDENTIALS = "/home/maxis/j4b-refresh-deployment/credentials/portal-auth.json"
BASE_URL = "https://lk.j4b.ru"
PROJECT_ID = "7986"  # selected from a local checkpoint: ACQUIRED with completed-state visits
sys.path.insert(1, REPO)

from src.portal_transport import PortalSession

_MODULE_PATH = os.path.join(os.path.dirname(__file__), "project_report_listing.py")
_SPEC = importlib.util.spec_from_file_location("j4b_project_report_listing", _MODULE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("probe unavailable")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)


def quiet_login(session: PortalSession) -> None:
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
        with open(CREDENTIALS, encoding="utf-8") as stream:
            config = json.load(stream)
        session = PortalSession(timeout=45, base_url=BASE_URL,
                                login=config["login"], password=config["password"])
        quiet_login(session)
        status, content_type, body = session.request(
            f"/proj/{PROJECT_ID}/report", "GET", accept="text/html", follow_redirects=True
        )
        if status != 200 or "html" not in content_type.lower():
            print(json.dumps({
                "http_status": status,
                "content_type_class": "HTML" if "html" in content_type.lower() else "NON_HTML_OR_ERROR",
                "response_bytes": len(body),
                "failure_category": "HTTP_OR_CONTENT_TYPE",
            }, separators=(",", ":")), flush=True)
            return 0
        result = _MODULE.inspect_project_page(body.decode("utf-8"), base_url=BASE_URL)
        result.update({
            "http_status": status,
            "response_bytes": len(body),
            "final_path_class": "PROJECT" if session.last_effective_url and "/proj/" in session.last_effective_url else "OTHER",
            "report_list_found": bool(
                result["data_rows"] and (result["action_ids_count"] or result["report_ids_count"])
            ),
        })
        rendered = json.dumps(result, ensure_ascii=True, separators=(",", ":"))
        if len(rendered) > 1800:
            print('{"failure_category":"SUMMARY_LIMIT"}', flush=True)
        else:
            print(rendered, flush=True)
        return 0
    except Exception:
        print('{"failure_category":"SAFE_PROBE_ERROR"}', flush=True)
        return 0
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    raise SystemExit(main())
