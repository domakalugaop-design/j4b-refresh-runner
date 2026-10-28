"""PORTAL-001.3 hybrid Reference ETL: HTTPS portal reference data -> Analytics.

Channel B of the hybrid architecture. Fetches only the reference records that
the explicitly selected projects actually use (client organization, primary
manager, assigned coordinators) from the authenticated project edit form, and
stores them in protected ``reference`` schema tables of the isolated Analytics
PostgreSQL. Operational SQL data is loaded by ``portal_mariadb_etl``; the two
channels are independent and an HTTPS failure never affects a finished SQL
load.

Privacy contract:
- Labels (FIOs, organization names) are written only to the protected
  reference tables. They never appear in logs, summaries, exceptions, or
  ``repr`` of in-memory objects.
- Only explicit project IDs are accepted; there is no discovery or crawl.
- Missing labels keep an explicit unknown state instead of a fabricated value.

Security contract:
- PostgreSQL is reached only through the fingerprint-verified SSH forward to
  the private Analytics container (reuse of the operational ETL primitives).
- The storage guard is checked before every target write.
- The ``reference`` schema is revoked from PUBLIC (and optionally from the
  analytics reader role); only the dedicated loader identity writes it.
- Without ``--run-live`` the CLI validates only and opens no connections.
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from src.portal_mariadb_etl import (
    ComposeStorageGuard,
    ETLError,
    GuardBlocked,
    MAX_PROJECT_IDS,
    SecurityGateError,
    TARGET_DATABASE,
    TARGET_USER,
    _fingerprints,
    _outside_repository,
    _safe_file,
    connect_postgres,
    exclusive_run_lock,
    read_password,
    open_postgres_tunnel,
    validate_project_ids,
)
from src.parsers import _project_edit_form, select_field_state
from src.portal_transport import PortalSession, PortalTransportError

REFERENCE_SCHEMA = "reference"
OPS_SCHEMA = "ops"
DEFAULT_MAX_AGE_HOURS = 24.0
PER_REQUEST_TIMEOUT = 15.0
RETRY_DELAY_SECONDS = 2.0
_ROLE_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

LABEL_PRESENT = "VALUE_PRESENT"
LABEL_MISSING = "MISSING"


class ReferenceSemanticError(ETLError):
    """The portal response did not contain the expected reference structure."""


@dataclass(frozen=True)
class ReferenceBundle:
    """One project's reference records. Labels are redacted from repr."""

    project_id: int
    client_id: Optional[int]
    client_label: Optional[str]
    client_label_state: str
    manager_id: Optional[int]
    manager_label: Optional[str]
    manager_label_state: str
    coordinators: tuple[tuple[int, Optional[str]], ...]

    def __repr__(self) -> str:  # labels never leak into logs or tracebacks
        return (
            "ReferenceBundle(project_id=%r client_id=%r manager_id=%r "
            "coordinator_ids=%r labels=REDACTED)"
            % (
                self.project_id,
                self.client_id,
                self.manager_id,
                [cid for cid, _label in self.coordinators],
            )
        )

    def user_records(self) -> tuple[tuple[int, Optional[str], str], ...]:
        """Distinct (user_id, label, label_state) records for this bundle."""
        records: dict[int, tuple[Optional[str], str]] = {}
        if self.manager_id is not None:
            records[self.manager_id] = (self.manager_label, self.manager_label_state)
        for coordinator_id, label in self.coordinators:
            state = LABEL_PRESENT if label else LABEL_MISSING
            existing = records.get(coordinator_id)
            if existing is None or (existing[1] == LABEL_MISSING and state == LABEL_PRESENT):
                records[coordinator_id] = (label, state)
        return tuple(
            (user_id, label, state) for user_id, (label, state) in sorted(records.items())
        )

    def content_hash(self) -> str:
        payload = json.dumps(
            {
                "client": [self.client_id, self.client_label],
                "manager": [self.manager_id, self.manager_label],
                "coordinators": sorted(
                    [cid, label] for cid, label in self.coordinators
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _selected_single(
    selected: Sequence[Mapping[str, str]], entity: str
) -> tuple[Optional[int], Optional[str], str]:
    if len(selected) > 1:
        raise ReferenceSemanticError("multiple selected %s options" % entity)
    if not selected:
        return None, None, "ABSENT"
    raw_value = selected[0]["value"].strip()
    label = selected[0]["label"].strip() or None
    if not raw_value.isdigit():
        raise ReferenceSemanticError("%s option has a non-numeric identity" % entity)
    return int(raw_value), label, LABEL_PRESENT if label else LABEL_MISSING


def parse_reference_bundle(project_id: int, html: str) -> ReferenceBundle:
    """Parse only the reference selects of the authenticated project edit form."""
    form = _project_edit_form(html)
    if form is None:
        raise ReferenceSemanticError("authenticated project edit form not found")
    _client_state, client_selected = select_field_state(form, "client")
    _user_state, user_selected = select_field_state(form, "user")
    _coord_state, coord_selected = select_field_state(form, "user2[]")
    client_id, client_label, client_state = _selected_single(client_selected, "client")
    manager_id, manager_label, manager_state = _selected_single(user_selected, "manager")
    coordinators: list[tuple[int, Optional[str]]] = []
    seen: set[int] = set()
    for option in coord_selected:
        raw_value = option["value"].strip()
        if not raw_value.isdigit():
            raise ReferenceSemanticError("coordinator option has a non-numeric identity")
        coordinator_id = int(raw_value)
        if coordinator_id in seen:
            continue
        seen.add(coordinator_id)
        coordinators.append((coordinator_id, option["label"].strip() or None))
    return ReferenceBundle(
        project_id=project_id,
        client_id=client_id,
        client_label=client_label,
        client_label_state=client_state,
        manager_id=manager_id,
        manager_label=manager_label,
        manager_label_state=manager_state,
        coordinators=tuple(coordinators),
    )


class PortalReferenceClient:
    """Bounded HTTPS reader; one edit request per project, one retry allowed."""

    def __init__(
        self,
        session: Any,
        *,
        max_retries: int = 1,
        timeout: float = PER_REQUEST_TIMEOUT,
    ):
        self.session = session
        self.max_retries = max_retries
        self.timeout = timeout
        self.http_requests = 0

    def fetch(self, project_id: int) -> tuple[ReferenceBundle, int]:
        attempts_left = self.max_retries
        while True:
            self.http_requests += 1
            try:
                status, _content_type, body = self.session.request(
                    "/proj/%d/edit" % project_id, accept="text/html"
                )
            except PortalTransportError as exc:
                if not getattr(exc, "retryable", False) or attempts_left <= 0:
                    raise ETLError("reference transport failed") from None
                attempts_left -= 1
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            if status != 200:
                raise ETLError("reference response status was not 200")
            html = body.decode("utf-8", "replace") if body else ""
            return parse_reference_bundle(project_id, html), 1


def keychain_credential(account: str) -> str:
    """Read a Portal credential without exposing it in argv, logs, or errors."""
    result = subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", "j4b-web-login",
         "-a", account, "-w"],
        capture_output=True,
        text=True,
        check=False,
    )
    value = result.stdout.rstrip("\r\n")
    if result.returncode != 0 or not value:
        raise SecurityGateError("portal keychain credential unavailable")
    return value


def live_portal_session(config: Optional[ReferenceConfig] = None) -> PortalSession:
    if config is not None and config.portal_login_file is not None:
        # Server mode: credentials come from owner-only 0600 files on the host.
        session = PortalSession(
            base_url=os.environ.get("PORTAL_BASE_URL", "https://lk.j4b.ru"),
            login=read_password(config.portal_login_file),
            password=read_password(config.portal_password_file),
            timeout=PER_REQUEST_TIMEOUT,
        )
    elif os.environ.get("PORTAL_LOGIN") and os.environ.get("PORTAL_PASSWORD"):
        session = PortalSession(timeout=PER_REQUEST_TIMEOUT)
    else:
        session = PortalSession(
            base_url=os.environ.get("PORTAL_BASE_URL", "https://lk.j4b.ru"),
            login=keychain_credential("login"),
            password=keychain_credential("password"),
            timeout=PER_REQUEST_TIMEOUT,
        )
    session.login()
    return session


@dataclass(frozen=True)
class ReferenceConfig:
    """Target-side runtime configuration (same rules as the operational ETL)."""

    target_host: str
    target_port: int
    target_database: str
    target_user: str
    target_password_file: Path
    target_ssh_host: str
    target_ssh_port: int
    target_ssh_user: str
    target_ssh_jump_host: Optional[str]
    target_ssh_jump_user: Optional[str]
    target_known_hosts_file: Path
    target_ssh_host_fingerprints: tuple[str, ...]
    target_ssh_jump_fingerprints: tuple[str, ...]
    target_ssh_identity_file: Optional[Path] = None
    target_transport: str = "ssh-tunnel"
    guard_transport: str = "ssh"
    portal_login_file: Optional[Path] = None
    portal_password_file: Optional[Path] = None
    compose_project_dir: Path = Path("/home/service/app/j4b-analytics")
    compose_file: Path = Path("/home/service/app/j4b-analytics/j4b-analytics-compose.yaml")


def load_reference_config(path: Path) -> ReferenceConfig:
    config_path = _safe_file(path)
    _outside_repository(config_path)
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            parser.read_file(handle)
        target = parser["target"]
        guard = parser["guard"]
        password_file = _safe_file(Path(target["password_file"]))
        _outside_repository(password_file)
        known_text = target.get("known_hosts_file", "").strip()
        known_hosts = _safe_file(Path(known_text), require_private=False) if known_text else None
        identity_text = target.get("ssh_identity_file", "").strip()
        identity_file = _safe_file(Path(identity_text)) if identity_text else None
        host_fingerprints = _fingerprints(target.get("ssh_host_fingerprints", ""))
        jump_fingerprints = _fingerprints(target.get("ssh_jump_fingerprints", ""))
        project_dir = Path(guard["compose_project_dir"])
        compose_file = Path(guard.get("compose_file", "j4b-analytics-compose.yaml"))
        if not compose_file.is_absolute():
            compose_file = project_dir / compose_file
        if project_dir.as_posix() != "/home/service/app/j4b-analytics" or compose_file.name != "j4b-analytics-compose.yaml":
            raise SecurityGateError("storage guard must target the isolated Analytics Compose project")
        if target["database"] != TARGET_DATABASE or target["user"] != TARGET_USER:
            raise SecurityGateError("target must be the dedicated Analytics loader identity")
        if target["host"] != "postgres" or int(target.get("port", "5432")) != 5432:
            raise SecurityGateError("target must use the private PostgreSQL Compose service endpoint")
        target_transport = target.get("transport", "ssh-tunnel").strip().lower()
        if target_transport not in {"ssh-tunnel", "direct"}:
            raise SecurityGateError("target transport must be the approved SSH tunnel or direct private-network access")
        guard_transport = guard.get("transport", "ssh").strip().lower()
        if guard_transport not in {"ssh", "local"}:
            raise SecurityGateError("guard transport must be ssh or local")
        if target_transport == "ssh-tunnel":
            if not host_fingerprints or not target.get("ssh_host") or not target.get("ssh_user"):
                raise SecurityGateError("target tunnel requires a trusted host key and independently approved fingerprint")
            if target.get("ssh_jump_host", "").strip() and not jump_fingerprints:
                raise SecurityGateError("target jump-host fingerprints must be independently approved")
        portal_login_file: Optional[Path] = None
        portal_password_file: Optional[Path] = None
        if parser.has_section("portal"):
            login_text = parser["portal"].get("login_file", "").strip()
            password_text = parser["portal"].get("password_file", "").strip()
            if bool(login_text) != bool(password_text):
                raise SecurityGateError("portal credential files must be provided together")
            if login_text:
                portal_login_file = _safe_file(Path(login_text))
                portal_password_file = _safe_file(Path(password_text))
                _outside_repository(portal_login_file)
                _outside_repository(portal_password_file)
        return ReferenceConfig(
            target_host=target["host"],
            target_port=int(target.get("port", "5432")),
            target_database=target["database"],
            target_user=target["user"],
            target_password_file=password_file,
            target_ssh_host=target.get("ssh_host", "").strip(),
            target_ssh_port=int(target.get("ssh_port", "22")),
            target_ssh_user=target.get("ssh_user", "").strip(),
            target_ssh_jump_host=target.get("ssh_jump_host", "").strip() or None,
            target_ssh_jump_user=target.get("ssh_jump_user", "").strip() or None,
            target_known_hosts_file=known_hosts,
            target_ssh_host_fingerprints=host_fingerprints,
            target_ssh_jump_fingerprints=jump_fingerprints,
            target_ssh_identity_file=identity_file,
            target_transport=target_transport,
            guard_transport=guard_transport,
            portal_login_file=portal_login_file,
            portal_password_file=portal_password_file,
            compose_project_dir=project_dir,
            compose_file=compose_file,
        )
    except (KeyError, configparser.Error, ValueError, OSError) as exc:
        if isinstance(exc, SecurityGateError):
            raise
        raise SecurityGateError("reference runtime config is incomplete or invalid") from None


_DDL = (
    """CREATE TABLE IF NOT EXISTS reference.portal_clients (
        client_id BIGINT PRIMARY KEY, label TEXT, label_state TEXT NOT NULL,
        first_seen_run UUID NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS reference.portal_users (
        user_id BIGINT PRIMARY KEY, label TEXT, label_state TEXT NOT NULL,
        first_seen_run UUID NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS reference.project_references (
        project_id BIGINT PRIMARY KEY,
        client_id BIGINT REFERENCES reference.portal_clients(client_id),
        primary_manager_id BIGINT REFERENCES reference.portal_users(user_id),
        coordinator_count INTEGER NOT NULL DEFAULT 0,
        first_seen_run UUID NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS reference.project_coordinators (
        project_id BIGINT NOT NULL REFERENCES reference.project_references(project_id) ON DELETE CASCADE,
        coordinator_id BIGINT NOT NULL REFERENCES reference.portal_users(user_id),
        PRIMARY KEY (project_id, coordinator_id))""",
    """CREATE TABLE IF NOT EXISTS ops.reference_sync_state (
        project_id BIGINT PRIMARY KEY, last_synced_at TIMESTAMPTZ NOT NULL,
        content_hash TEXT NOT NULL, http_requests INTEGER NOT NULL, status TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ops.reference_etl_runs (
        run_id UUID PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ, status TEXT NOT NULL, project_count INTEGER NOT NULL,
        fetched INTEGER NOT NULL DEFAULT 0, skipped_fresh INTEGER NOT NULL DEFAULT 0,
        failed INTEGER NOT NULL DEFAULT 0, http_requests INTEGER NOT NULL DEFAULT 0,
        error_count INTEGER NOT NULL DEFAULT 0)""",
)

_HARDENING = (
    "REVOKE ALL ON SCHEMA reference FROM PUBLIC",
    "REVOKE ALL ON SCHEMA ops FROM PUBLIC",
    "REVOKE ALL ON ALL TABLES IN SCHEMA reference FROM PUBLIC",
    "REVOKE ALL ON ALL TABLES IN SCHEMA ops FROM PUBLIC",
)


class ReferenceTarget:
    """Protected reference writer for the dedicated Analytics loader identity."""

    def __init__(self, connection: Any):
        self.connection = connection

    def verify_target(self) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user")
            row = cursor.fetchone()
            cursor.execute(
                "SELECT schema_name FROM information_schema.schemata"
                " WHERE schema_name = ANY(%s) ORDER BY schema_name",
                ([OPS_SCHEMA, REFERENCE_SCHEMA],),
            )
            schemas = [result[0] for result in cursor.fetchall()]
        if row != (TARGET_DATABASE, TARGET_USER) or schemas != sorted((OPS_SCHEMA, REFERENCE_SCHEMA)):
            raise SecurityGateError("target is not the dedicated Analytics loader endpoint")

    def ensure_tables(self, reader_role: Optional[str] = None) -> None:
        with self.connection:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT schema_name FROM information_schema.schemata"
                    " WHERE schema_name = ANY(%s) ORDER BY schema_name",
                    ([OPS_SCHEMA, REFERENCE_SCHEMA],),
                )
                schemas = [row[0] for row in cursor.fetchall()]
                if schemas != sorted((OPS_SCHEMA, REFERENCE_SCHEMA)):
                    raise SecurityGateError(
                        "reference and ops schemas must be pre-provisioned by the administrator"
                    )
                for statement in _DDL:
                    cursor.execute(statement)
                for statement in _HARDENING:
                    cursor.execute(statement)
                if reader_role is not None:
                    if not _ROLE_NAME_RE.match(reader_role):
                        raise SecurityGateError("reader role name has an unexpected format")
                    cursor.execute(
                        "SELECT 1 FROM pg_roles WHERE rolname=%s", (reader_role,)
                    )
                    if cursor.fetchone() is None:
                        raise SecurityGateError("configured reader role does not exist")
                    cursor.execute(
                        'REVOKE ALL ON SCHEMA reference FROM "%s"' % reader_role
                    )
                    cursor.execute(
                        'REVOKE ALL ON ALL TABLES IN SCHEMA reference FROM "%s"' % reader_role
                    )

    def sync_state(self, project_ids: Sequence[int]) -> dict[int, tuple[datetime, str]]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT project_id, last_synced_at, content_hash FROM ops.reference_sync_state "
                "WHERE project_id = ANY(%s)",
                (list(project_ids),),
            )
            rows = cursor.fetchall()
        return {int(row[0]): (row[1], row[2]) for row in rows}

    def start_run(self, run_id: str, project_count: int) -> None:
        with self.connection:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO ops.reference_etl_runs(run_id,status,project_count) "
                    "VALUES (%s,%s,%s)",
                    (run_id, "RUNNING", project_count),
                )

    def finish_run(self, run_id: str, status: str, summary: "ReferenceRunSummary") -> None:
        with self.connection:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE ops.reference_etl_runs SET status=%s, finished_at=%s, "
                    "fetched=%s, skipped_fresh=%s, failed=%s, http_requests=%s, "
                    "error_count=%s WHERE run_id=%s",
                    (
                        status,
                        datetime.now(timezone.utc),
                        summary.fetched,
                        summary.skipped_fresh,
                        summary.failed,
                        summary.http_requests,
                        summary.errors,
                        run_id,
                    ),
                )

    def upsert_bundle(self, run_id: str, bundle: ReferenceBundle) -> dict[str, int]:
        counts = {"clients": 0, "users": 0, "projects": 0, "coordinators": 0}
        with self.connection:
            with self.connection.cursor() as cursor:
                if bundle.client_id is not None:
                    counts["clients"] = self._upsert_reference_row(
                        cursor,
                        "reference.portal_clients",
                        "client_id",
                        bundle.client_id,
                        bundle.client_label,
                        bundle.client_label_state,
                        run_id,
                    )
                user_records = bundle.user_records()
                for user_id, label, label_state in user_records:
                    counts["users"] += self._upsert_reference_row(
                        cursor,
                        "reference.portal_users",
                        "user_id",
                        user_id,
                        label,
                        label_state,
                        run_id,
                    )
                if bundle.client_id is not None or bundle.manager_id is not None:
                    cursor.execute(
                        "INSERT INTO reference.project_references "
                        "(project_id, client_id, primary_manager_id, coordinator_count, first_seen_run) "
                        "VALUES (%s,%s,%s,%s,%s) "
                        "ON CONFLICT (project_id) DO UPDATE SET "
                        "client_id=EXCLUDED.client_id, "
                        "primary_manager_id=EXCLUDED.primary_manager_id, "
                        "coordinator_count=EXCLUDED.coordinator_count, updated_at=now() "
                        "WHERE reference.project_references.client_id IS DISTINCT FROM EXCLUDED.client_id "
                        "OR reference.project_references.primary_manager_id IS DISTINCT FROM EXCLUDED.primary_manager_id "
                        "OR reference.project_references.coordinator_count IS DISTINCT FROM EXCLUDED.coordinator_count "
                        "RETURNING 1",
                        (
                            bundle.project_id,
                            bundle.client_id,
                            bundle.manager_id,
                            len(bundle.coordinators),
                            run_id,
                        ),
                    )
                    counts["projects"] += 1 if cursor.fetchone() else 0
                cursor.execute(
                    "DELETE FROM reference.project_coordinators WHERE project_id=%s",
                    (bundle.project_id,),
                )
                for coordinator_id, _label in bundle.coordinators:
                    cursor.execute(
                        "INSERT INTO reference.project_coordinators "
                        "(project_id, coordinator_id) VALUES (%s,%s) "
                        "ON CONFLICT (project_id, coordinator_id) DO NOTHING",
                        (bundle.project_id, coordinator_id),
                    )
                    counts["coordinators"] += 1
        return counts

    @staticmethod
    def _upsert_reference_row(
        cursor: Any,
        table: str,
        id_column: str,
        record_id: int,
        label: Optional[str],
        label_state: str,
        run_id: str,
    ) -> int:
        cursor.execute(
            "INSERT INTO %s (%s, label, label_state, first_seen_run) VALUES (%%s,%%s,%%s,%%s) "
            "ON CONFLICT (%s) DO UPDATE SET label=EXCLUDED.label, "
            "label_state=EXCLUDED.label_state, updated_at=now() "
            "WHERE %s.label IS DISTINCT FROM EXCLUDED.label "
            "OR %s.label_state IS DISTINCT FROM EXCLUDED.label_state "
            "RETURNING 1" % (table, id_column, id_column, table, table),
            (record_id, label, label_state, run_id),
        )
        return 1 if cursor.fetchone() else 0

    def record_sync(
        self, project_id: int, content_hash: str, http_requests: int, status: str
    ) -> None:
        with self.connection:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO ops.reference_sync_state "
                    "(project_id, last_synced_at, content_hash, http_requests, status) "
                    "VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT (project_id) DO UPDATE SET "
                    "last_synced_at=EXCLUDED.last_synced_at, "
                    "content_hash=EXCLUDED.content_hash, "
                    "http_requests=EXCLUDED.http_requests, status=EXCLUDED.status",
                    (
                        project_id,
                        datetime.now(timezone.utc),
                        content_hash,
                        http_requests,
                        status,
                    ),
                )


@dataclass
class ReferenceRunSummary:
    run_id: str
    status: str
    project_count: int
    fetched: int = 0
    skipped_fresh: int = 0
    failed: int = 0
    http_requests: int = 0
    errors: int = 0
    written: dict[str, int] = field(default_factory=dict)
    project_statuses: dict[int, str] = field(default_factory=dict)

    def redacted(self) -> dict[str, Any]:
        """Log-safe view: identifiers and counters only, never labels."""
        return {
            "run_id": self.run_id,
            "status": self.status,
            "project_count": self.project_count,
            "fetched": self.fetched,
            "skipped_fresh": self.skipped_fresh,
            "failed": self.failed,
            "http_requests": self.http_requests,
            "errors": self.errors,
            "written": dict(self.written),
            "project_statuses": {
                str(pid): status for pid, status in sorted(self.project_statuses.items())
            },
        }


class ReferenceETLRunner:
    """Independent HTTPS reference channel; per-project failure isolation."""

    def __init__(
        self,
        client: Any,
        target: ReferenceTarget,
        guard: Any,
        *,
        max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
        reader_role: Optional[str] = None,
    ):
        if max_age_hours < 0:
            raise ETLError("max age must not be negative")
        self.client = client
        self.target = target
        self.guard = guard
        self.max_age_hours = max_age_hours
        self.reader_role = reader_role

    def _guard(self) -> None:
        if self.guard.check() is not True:
            raise GuardBlocked("storage guard did not return ALLOW; no next write was performed")

    def run(self, selected_project_ids: Sequence[int]) -> ReferenceRunSummary:
        project_ids = validate_project_ids(selected_project_ids)
        run_id = str(uuid.uuid4())
        summary = ReferenceRunSummary(
            run_id=run_id, status="PREPARING", project_count=len(project_ids)
        )
        run_started = False
        try:
            self.target.verify_target()
            self._guard()
            self.target.ensure_tables(reader_role=self.reader_role)
            self._guard()
            self.target.start_run(run_id, len(project_ids))
            run_started = True
            summary.status = "RUNNING"
            state = self.target.sync_state(project_ids)
            now = datetime.now(timezone.utc)
            fresh_cutoff = timedelta(hours=self.max_age_hours)
            totals = {"clients": 0, "users": 0, "projects": 0, "coordinators": 0}
            for project_id in project_ids:
                last_synced, _hash = state.get(project_id, (None, None))
                if last_synced is not None and now - last_synced < fresh_cutoff:
                    summary.skipped_fresh += 1
                    summary.project_statuses[project_id] = "SKIPPED_FRESH"
                    continue
                try:
                    bundle, used = self.client.fetch(project_id)
                except Exception:
                    # Error text is suppressed: it may contain response details.
                    summary.failed += 1
                    summary.errors += 1
                    summary.project_statuses[project_id] = "FAILED"
                    continue
                summary.http_requests += used
                self._guard()
                written = self.target.upsert_bundle(run_id, bundle)
                for key, value in written.items():
                    totals[key] += value
                self._guard()
                self.target.record_sync(
                    project_id, bundle.content_hash(), used, "SUCCEEDED"
                )
                summary.fetched += 1
                summary.project_statuses[project_id] = "SUCCEEDED"
            summary.written = totals
            if summary.failed and summary.fetched:
                summary.status = "SUCCEEDED_WITH_ERRORS"
            elif summary.failed:
                summary.status = "FAILED"
            else:
                summary.status = "SUCCEEDED"
            self._guard()
            self.target.finish_run(run_id, summary.status, summary)
            return summary
        except GuardBlocked:
            summary.status = "STOPPED_BY_GUARD"
            summary.errors += 1
            raise
        except Exception:
            summary.status = "FAILED"
            summary.errors += 1
            if run_started:
                try:
                    self._guard()
                    self.target.finish_run(run_id, "FAILED", summary)
                except Exception:
                    pass
            raise ETLError("reference ETL stopped; inspect only redacted error class") from None


def run_live(
    config: ReferenceConfig,
    project_ids: Sequence[int],
    *,
    max_age_hours: float,
    reader_role: Optional[str],
) -> ReferenceRunSummary:
    with exclusive_run_lock("portal-reference-etl"):
        with open_postgres_tunnel(config) as endpoint:
            connection = connect_postgres(config, endpoint)
            try:
                runner = ReferenceETLRunner(
                    PortalReferenceClient(live_portal_session(config)),
                    ReferenceTarget(connection),
                    ComposeStorageGuard(config),
                    max_age_hours=max_age_hours,
                    reader_role=reader_role,
                )
                return runner.run(project_ids)
            finally:
                connection.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded, guarded Portal reference ETL")
    parser.add_argument("--config", required=True, type=Path, help="external owner-only INI; never store in Git")
    parser.add_argument("--project-id", action="append", required=True, type=int, help="explicit selected project ID; repeatable")
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    parser.add_argument("--reader-role", type=str, default=None, help="analytics reader role to explicitly restrict")
    parser.add_argument("--run-live", action="store_true", help="explicitly allow portal reads and Analytics writes")
    args = parser.parse_args(argv)
    try:
        project_ids = validate_project_ids(args.project_id)
        config = load_reference_config(args.config)
        if not args.run_live:
            print(
                "PREPARATION_ONLY=YES project_id_count=%d live_connections=NO" % len(project_ids)
            )
            return 0
        summary = run_live(
            config, project_ids, max_age_hours=args.max_age_hours, reader_role=args.reader_role
        )
        print(
            "REFERENCE_ETL_STATUS=%s run_id=%s fetched=%d skipped_fresh=%d failed=%d "
            "http_requests=%d written=%s"
            % (
                summary.status,
                summary.run_id,
                summary.fetched,
                summary.skipped_fresh,
                summary.failed,
                summary.http_requests,
                json.dumps(summary.written, sort_keys=True),
            )
        )
        return 0
    except GuardBlocked:
        print("REFERENCE_ETL_STATUS=STOPPED_BY_GUARD writes_after_stop=0", file=sys.stderr)
        return 3
    except ETLError:
        print("REFERENCE_ETL_STATUS=BLOCKED error_class=suppressed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
