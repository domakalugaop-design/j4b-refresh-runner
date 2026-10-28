"""Synthetic tests for the bounded, guarded Portal reference ETL.

No network, no real credentials, no real PostgreSQL: the target connection is
an in-memory emulation of the exact SQL semantics the ReferenceTarget issues
(ON CONFLICT ... WHERE ... IS DISTINCT FROM upserts, roster replacement,
sync-state journal). The portal side is a scripted fake session.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.portal_mariadb_etl import ETLError, GuardBlocked, SecurityGateError
from src.portal_reference_etl import (
    LABEL_MISSING,
    LABEL_PRESENT,
    PortalReferenceClient,
    ReferenceBundle,
    ReferenceETLRunner,
    ReferenceTarget,
    load_reference_config,
    parse_reference_bundle,
)
from src.portal_transport import PortalTransportError

PROJECT_A = 8181
PROJECT_B = 8187


def edit_form_html(
    client=(224, "Org Label A"),
    manager=(95137, "Manager FIO A"),
    coordinators=((830, "Controller FIO One"), (12483, "Controller FIO Two"), (8285, None)),
) -> str:
    def select(name: str, selected_values: set[int], choices) -> str:
        rows = ['<option value="">--</option>']
        for value, label in choices:
            is_selected = value in selected_values
            rows.append(
                '<option value="%d"%s>%s</option>' % (value, " selected" if is_selected else "", label or "")
            )
        multiple = " multiple" if name == "user2[]" else ""
        return '<select name="%s"%s>\n%s\n</select>' % (name, multiple, "\n".join(rows))

    return (
        "<!doctype html><html><body>"
        '<form method="post" action="/proj/8181/edit">'
        '<input type="text" name="name" value="Synthetic Project">'
        '<input type="text" name="dt1" value="01.10.2026">'
        '<input type="text" name="dt2" value="31.10.2026">'
        '<input type="text" name="visits" value="10">'
        + select("client", {client[0]} if client else set(), [client] if client else [])
        + select("user", {manager[0]} if manager else set(), [manager] if manager else [])
        + select("user2[]", {cid for cid, _label in coordinators}, coordinators)
        + "</form></body></html>"
    )


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.connection.executed.append((normalized, params))
        self._rows = self.connection._dispatch(normalized, params or ())

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    """In-memory emulation of the loader-side SQL semantics."""

    def __init__(
        self,
        roles=("analytics_reader",),
        identity=("j4b_analytics", "j4b_analytics_loader"),
        schemas=("ops", "reference"),
    ):
        self.clients: dict[int, tuple[str | None, str]] = {}
        self.users: dict[int, tuple[str | None, str]] = {}
        self.projects: dict[int, tuple[int | None, int | None, int]] = {}
        self.coordinators: set[tuple[int, int]] = set()
        self.sync_state: dict[int, tuple[datetime, str]] = {}
        self.runs: dict[str, dict] = {}
        self.roles = set(roles)
        self.identity = identity
        self.schemas = tuple(sorted(schemas))
        self.executed: list[tuple[str, object]] = []

    def cursor(self):
        return FakeCursor(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass

    def _dispatch(self, sql, params):
        if "current_database()" in sql:
            return [self.identity]
        if "information_schema.schemata" in sql:
            return [(name,) for name in self.schemas]
        if "FROM pg_roles" in sql:
            return [(1,)] if params[0] in self.roles else []
        if "FROM ops.reference_sync_state" in sql:
            wanted = set(params[0])
            return [
                (pid, row[0], row[1])
                for pid, row in sorted(self.sync_state.items())
                if pid in wanted
            ]
        if "INSERT INTO ops.reference_etl_runs" in sql:
            self.runs[params[0]] = {"status": params[1], "project_count": params[2]}
            return []
        if "UPDATE ops.reference_etl_runs" in sql:
            self.runs[params[-1]].update(
                status=params[0],
                fetched=params[2],
                skipped_fresh=params[3],
                failed=params[4],
                http_requests=params[5],
                error_count=params[6],
            )
            return []
        if "INSERT INTO reference.portal_clients" in sql:
            client_id, label, state = params[0], params[1], params[2]
            if self.clients.get(client_id) == (label, state):
                return []
            self.clients[client_id] = (label, state)
            return [(1,)]
        if "INSERT INTO reference.portal_users" in sql:
            user_id, label, state = params[0], params[1], params[2]
            if self.users.get(user_id) == (label, state):
                return []
            self.users[user_id] = (label, state)
            return [(1,)]
        if "INSERT INTO reference.project_references" in sql:
            project_id, client_id, manager_id, count = params[0], params[1], params[2], params[3]
            if self.projects.get(project_id) == (client_id, manager_id, count):
                return []
            self.projects[project_id] = (client_id, manager_id, count)
            return [(1,)]
        if "DELETE FROM reference.project_coordinators" in sql:
            self.coordinators = {key for key in self.coordinators if key[0] != params[0]}
            return []
        if "INSERT INTO reference.project_coordinators" in sql:
            self.coordinators.add((params[0], params[1]))
            return []
        if "INSERT INTO ops.reference_sync_state" in sql:
            self.sync_state[params[0]] = (params[1], params[2])
            return []
        if sql.startswith("CREATE") or sql.startswith("REVOKE"):
            return []
        raise AssertionError("unexpected SQL in test: %s" % sql[:120])


class AllowGuard:
    def __init__(self):
        self.checks = 0

    def check(self):
        self.checks += 1
        return True


class DenyGuard:
    def check(self):
        return False


class FakePortalSession:
    """Scripted responses: str -> HTML page; list -> consumed in order."""

    def __init__(self, pages: dict[int, object]):
        self.pages = pages
        self.requests = 0

    def request(self, path, accept="text/html"):
        self.requests += 1
        project_id = int(path.split("/")[2])
        outcome = self.pages[project_id]
        if isinstance(outcome, list):
            outcome = outcome.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, tuple):
            return outcome
        return 200, "text/html", outcome.encode("utf-8")


def make_runner(pages, connection=None, guard=None, max_age_hours=24.0):
    connection = connection or FakeConnection()
    client = PortalReferenceClient(FakePortalSession(pages))
    target = ReferenceTarget(connection)
    return (
        ReferenceETLRunner(client, target, guard or AllowGuard(), max_age_hours=max_age_hours),
        client,
        connection,
    )


# --- parsing ----------------------------------------------------------------


def test_parse_extracts_selected_reference_records():
    bundle = parse_reference_bundle(PROJECT_A, edit_form_html())
    assert bundle.project_id == PROJECT_A
    assert bundle.client_id == 224
    assert bundle.client_label_state == LABEL_PRESENT
    assert bundle.manager_id == 95137
    assert [cid for cid, _label in bundle.coordinators] == [830, 12483, 8285]
    assert bundle.user_records() == (
        (830, "Controller FIO One", LABEL_PRESENT),
        (8285, None, LABEL_MISSING),
        (12483, "Controller FIO Two", LABEL_PRESENT),
        (95137, "Manager FIO A", LABEL_PRESENT),
    )


def test_parse_keeps_explicit_unknown_state_for_missing_labels():
    bundle = parse_reference_bundle(
        PROJECT_A, edit_form_html(client=(224, ""), manager=(95137, ""))
    )
    assert bundle.client_label is None
    assert bundle.client_label_state == LABEL_MISSING
    assert bundle.manager_label_state == LABEL_MISSING


def test_parse_rejects_non_numeric_identity_and_ambiguous_selection():
    with pytest.raises(ETLError):
        parse_reference_bundle(
            PROJECT_A, edit_form_html(client=(224, "Org")).replace('value="224"', 'value="org-224"')
        )
    ambiguous = edit_form_html().replace(
        '<option value="224" selected>', '<option value="224" selected></option><option value="185" selected>'
    )
    with pytest.raises(ETLError):
        parse_reference_bundle(PROJECT_A, ambiguous)


def test_bundle_repr_and_content_hash_redact_labels():
    bundle = parse_reference_bundle(PROJECT_A, edit_form_html())
    rendered = repr(bundle)
    assert "Org Label A" not in rendered
    assert "Manager FIO A" not in rendered
    assert "Controller FIO One" not in rendered
    assert bundle.client_id == 224  # identifiers stay visible
    changed = parse_reference_bundle(PROJECT_A, edit_form_html(client=(224, "Org Label B")))
    assert bundle.content_hash() != changed.content_hash()
    same = parse_reference_bundle(PROJECT_A, edit_form_html())
    assert bundle.content_hash() == same.content_hash()


# --- HTTPS client -----------------------------------------------------------


def test_client_fetch_parses_bundle_and_counts_requests():
    client = PortalReferenceClient(FakePortalSession({PROJECT_A: edit_form_html()}))
    bundle, used = client.fetch(PROJECT_A)
    assert used == 1
    assert client.http_requests == 1
    assert bundle.client_id == 224


def test_client_retries_retryable_transport_error_once():
    pages = {
        PROJECT_A: [
            PortalTransportError("TIMEOUT", True),
            edit_form_html(),
        ]
    }
    client = PortalReferenceClient(FakePortalSession(pages))
    bundle, used = client.fetch(PROJECT_A)
    assert bundle.manager_id == 95137
    assert client.http_requests == 2


def test_client_fails_closed_on_non_retryable_error():
    client = PortalReferenceClient(
        FakePortalSession({PROJECT_A: PortalTransportError("TLS_ERROR", False)})
    )
    with pytest.raises(ETLError):
        client.fetch(PROJECT_A)


def test_client_rejects_non_200_status():
    client = PortalReferenceClient(FakePortalSession({PROJECT_A: (503, "text/html", b"")}))
    with pytest.raises(ETLError):
        client.fetch(PROJECT_A)


# --- runner: happy path, idempotency, isolation ------------------------------


def test_runner_happy_path_writes_reference_records():
    runner, client, connection = make_runner(
        {PROJECT_A: edit_form_html(), PROJECT_B: edit_form_html()}
    )
    summary = runner.run([PROJECT_A, PROJECT_B])
    assert summary.status == "SUCCEEDED"
    assert summary.fetched == 2
    assert summary.failed == 0
    assert summary.http_requests == 2
    assert summary.written == {"clients": 1, "users": 4, "projects": 2, "coordinators": 6}
    assert connection.clients[224] == ("Org Label A", LABEL_PRESENT)
    assert connection.users[95137] == ("Manager FIO A", LABEL_PRESENT)
    assert connection.users[8285] == (None, LABEL_MISSING)
    assert (PROJECT_A, 8285) in connection.coordinators
    assert connection.sync_state[PROJECT_A][1]  # content hash recorded
    assert connection.runs[summary.run_id]["status"] == "SUCCEEDED"


def test_rerun_within_freshness_window_skips_https_entirely():
    runner, client, _connection = make_runner(
        {PROJECT_A: edit_form_html()}, max_age_hours=24.0
    )
    runner.run([PROJECT_A])
    requests_after_first = client.http_requests
    summary = runner.run([PROJECT_A])
    assert summary.status == "SUCCEEDED"
    assert summary.skipped_fresh == 1
    assert summary.fetched == 0
    assert client.http_requests == requests_after_first  # no new HTTPS traffic


def test_rerun_after_expiry_writes_no_duplicates():
    runner, client, connection = make_runner(
        {PROJECT_A: edit_form_html()}, max_age_hours=0.0
    )
    first = runner.run([PROJECT_A])
    assert first.written["users"] == 4
    users_after_first = dict(connection.users)
    second = runner.run([PROJECT_A])
    assert second.fetched == 1
    assert second.written["users"] == 0  # identical content -> zero writes
    assert second.written["clients"] == 0
    assert second.written["projects"] == 0
    assert connection.users == users_after_first
    assert len(connection.coordinators) == 3  # roster replaced, not duplicated


def test_label_change_updates_reference_record_on_next_run():
    runner, _client, connection = make_runner(
        {PROJECT_A: edit_form_html()}, max_age_hours=0.0
    )
    runner.run([PROJECT_A])
    runner.client.session.pages[PROJECT_A] = edit_form_html(manager=(95137, "Manager FIO Renamed"))
    second = runner.run([PROJECT_A])
    assert second.written["users"] == 1
    assert connection.users[95137] == ("Manager FIO Renamed", LABEL_PRESENT)


def test_unknown_coordinator_gets_own_user_record_without_cross_links():
    runner, _client, connection = make_runner(
        {PROJECT_A: edit_form_html(coordinators=((777001, "New Controller FIO"),))}
    )
    summary = runner.run([PROJECT_A])
    assert summary.status == "SUCCEEDED"
    assert connection.users[777001] == ("New Controller FIO", LABEL_PRESENT)
    assert (PROJECT_A, 777001) in connection.coordinators
    assert all(key[0] == PROJECT_A for key in connection.coordinators)


def test_https_failure_isolated_and_never_touches_operational_schema():
    pages = {
        PROJECT_A: PortalTransportError("TIMEOUT", True),  # both attempts fail
        PROJECT_B: edit_form_html(),
    }
    runner, _client, connection = make_runner(pages)
    summary = runner.run([PROJECT_A, PROJECT_B])
    assert summary.status == "SUCCEEDED_WITH_ERRORS"
    assert summary.failed == 1
    assert summary.project_statuses[PROJECT_A] == "FAILED"
    assert summary.project_statuses[PROJECT_B] == "SUCCEEDED"
    assert PROJECT_A not in connection.sync_state  # failed project not marked synced
    assert not any("raw." in sql for sql, _params in connection.executed)


def test_all_projects_failing_marks_run_failed():
    runner, _client, connection = make_runner(
        {PROJECT_A: PortalTransportError("CONNECT_ERROR", True)}
    )
    summary = runner.run([PROJECT_A])
    assert summary.status == "FAILED"
    assert summary.failed == 1
    assert connection.runs[summary.run_id]["status"] == "FAILED"


def test_summary_never_contains_labels():
    runner, _client, _connection = make_runner({PROJECT_A: edit_form_html()})
    summary = runner.run([PROJECT_A])
    rendered = repr(summary.redacted())
    for label in ("Org Label A", "Manager FIO A", "Controller FIO One"):
        assert label not in rendered


# --- guard and target validation --------------------------------------------


def test_guard_block_stops_before_any_write():
    runner, client, connection = make_runner(
        {PROJECT_A: edit_form_html()}, guard=DenyGuard()
    )
    with pytest.raises(GuardBlocked):
        runner.run([PROJECT_A])
    assert not any(
        sql.startswith("INSERT") or sql.startswith("UPDATE") or sql.startswith("DELETE")
        for sql, _params in connection.executed
    )
    assert client.http_requests == 0


def test_verify_target_rejects_wrong_identity():
    connection = FakeConnection(identity=("other_db", "other_user"))
    with pytest.raises(SecurityGateError):
        ReferenceTarget(connection).verify_target()


def test_ensure_tables_revokes_public_and_validates_reader_role():
    connection = FakeConnection(roles=("analytics_reader",))
    ReferenceTarget(connection).ensure_tables(reader_role="analytics_reader")
    executed = [sql for sql, _params in connection.executed]
    assert any(sql == "REVOKE ALL ON SCHEMA reference FROM PUBLIC" for sql in executed)
    assert any(
        sql == 'REVOKE ALL ON SCHEMA reference FROM "analytics_reader"' for sql in executed
    )
    with pytest.raises(SecurityGateError):
        ReferenceTarget(connection).ensure_tables(reader_role="missing_role")
    with pytest.raises(SecurityGateError):
        ReferenceTarget(connection).ensure_tables(reader_role="Bad Role; DROP")


# --- configuration -----------------------------------------------------------


def write_config(tmp_path: Path, **overrides: str) -> Path:
    values = {
        "host": "postgres",
        "port": "5432",
        "database": "j4b_analytics",
        "user": "j4b_analytics_loader",
        "transport": "ssh-tunnel",
        "ssh_host": "200.169.190.247",
        "ssh_port": "22",
        "ssh_user": "root",
        "ssh_host_fingerprints": "SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "compose_project_dir": "/home/service/app/j4b-analytics",
        "compose_file": "j4b-analytics-compose.yaml",
    }
    values.update(overrides)
    password_file = tmp_path / "pgpass"
    password_file.write_text("secret", encoding="utf-8")
    os.chmod(password_file, 0o600)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("", encoding="utf-8")
    os.chmod(known_hosts, 0o600)
    ini = tmp_path / "reference.ini"
    jump_lines = ""
    if values.get("ssh_jump_host"):
        jump_lines = (
            "ssh_jump_host = %s\nssh_jump_user = %s\n"
            % (values["ssh_jump_host"], values.get("ssh_jump_user", ""))
        )
    ini.write_text(
        "[target]\n"
        "transport = %(transport)s\n"
        "host = %(host)s\n"
        "port = %(port)s\n"
        "database = %(database)s\n"
        "user = %(user)s\n"
        "password_file = %(password_file)s\n"
        "ssh_host = %(ssh_host)s\n"
        "ssh_port = %(ssh_port)s\n"
        "ssh_user = %(ssh_user)s\n"
        "known_hosts_file = %(known_hosts)s\n"
        "ssh_host_fingerprints = %(ssh_host_fingerprints)s\n"
        "%(jump_lines)s"
        "\n[guard]\n"
        "compose_project_dir = %(compose_project_dir)s\n"
        "compose_file = %(compose_file)s\n"
        % {
            **values,
            "password_file": str(password_file),
            "known_hosts": str(known_hosts),
            "jump_lines": jump_lines,
        },
        encoding="utf-8",
    )
    os.chmod(ini, 0o600)
    return ini


def test_load_reference_config_accepts_approved_shape(tmp_path):
    config = load_reference_config(write_config(tmp_path))
    assert config.target_database == "j4b_analytics"
    assert config.target_user == "j4b_analytics_loader"
    assert config.target_ssh_host == "200.169.190.247"
    assert config.compose_file.name == "j4b-analytics-compose.yaml"


@pytest.mark.parametrize(
    "overrides",
    [
        {"database": "postgres"},
        {"user": "postgres"},
        {"host": "localhost"},
        {"transport": "direct"},
        {"ssh_host_fingerprints": ""},
        {"compose_project_dir": "/home/service/app/other"},
        {"compose_file": "docker-compose.yaml"},
    ],
)
def test_load_reference_config_rejects_unapproved_shapes(tmp_path, overrides):
    with pytest.raises(SecurityGateError):
        load_reference_config(write_config(tmp_path, **overrides))


def test_load_reference_config_requires_jump_fingerprints_when_jump_set(tmp_path):
    with pytest.raises(SecurityGateError):
        load_reference_config(
            write_config(tmp_path, ssh_jump_host="jump.example", ssh_jump_user="root")
        )


def test_runner_rejects_negative_max_age():
    with pytest.raises(ETLError):
        ReferenceETLRunner(
            PortalReferenceClient(FakePortalSession({})),
            ReferenceTarget(FakeConnection()),
            AllowGuard(),
            max_age_hours=-1.0,
        )


def test_verify_target_rejects_missing_reference_schemas():
    connection = FakeConnection(schemas=("ops",))
    with pytest.raises(SecurityGateError):
        ReferenceTarget(connection).verify_target()


def test_ensure_tables_requires_preprovisioned_schemas():
    connection = FakeConnection(schemas=("reference",))
    with pytest.raises(SecurityGateError):
        ReferenceTarget(connection).ensure_tables()
    assert not any(sql.startswith("CREATE TABLE") for sql, _ in connection.executed)
