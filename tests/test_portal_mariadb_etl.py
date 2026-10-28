from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.portal_mariadb_etl import (
    ETLError,
    ETLRunner,
    GuardBlocked,
    RuntimeConfig,
    SecurityGateError,
    SourceValidationError,
    _fingerprints,
    _transform_row,
    load_runtime_config,
    require_mysql_tls_capability,
    resolve_postgres_container_ip,
    source_count_sql,
    source_select_sql,
    validate_dataset,
    validate_explain_plan,
    validate_project_ids,
    validate_readonly_grants,
    verify_known_host_fingerprints,
)


def sample_data():
    return {
        "proj": [
            {"source_id": 7001, "planned_visits": 2, "raw_type": 8, "dt": 1790800000, "dt1": None, "dt2": 0}
        ],
        "visit": [
            {"project_id": 7001, "source_id": 9001, "dt": 1790900001, "dt1": 1790900002, "dt2": None},
            {"project_id": 7001, "source_id": 9002, "dt": 1790900003, "dt1": None, "dt2": 0},
        ],
        "my": [
            {"project_id": 7001, "source_id": 8101, "visit_id": 9001, "raw_state": 45, "dt": 1790900004, "dt_visit": 0}
        ],
        "claim": [
            {"source_id": 81001, "claim_project_id": 7001, "linked_visit_project_id": 7002,
             "visit_id": 9101, "my_id": 8101, "raw_type": 3, "raw_state": 12,
             "dt": 1790900005, "repdt": None},
            {"source_id": 81002, "claim_project_id": 7001, "linked_visit_project_id": None,
             "visit_id": 9999, "my_id": None, "raw_type": None, "raw_state": 45,
             "dt": 1790900006, "repdt": 1790900007},
        ],
    }


class FakeSource:
    def __init__(self, data=None):
        self.data = data or sample_data()
        self.calls = []

    def begin_readonly_snapshot(self):
        self.calls.append("begin")

    def verify_readonly_privileges(self):
        self.calls.append("readonly")

    def validate_schema_and_engines(self):
        self.calls.append("schema")

    def validate_query_plans(self, project_ids):
        self.calls.append("plans")
        assert tuple(project_ids) == (7001,)

    def preflight(self, project_ids):
        assert tuple(project_ids) == (7001,)
        return {entity: len(rows) for entity, rows in self.data.items()}

    def extract(self, project_ids, batch_size):
        assert tuple(project_ids) == (7001,)
        return self.data

    def close_snapshot(self):
        self.calls.append("rollback")


class FakeGuard:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.calls = 0

    def check(self):
        self.calls += 1
        if self.results:
            return self.results.pop(0)
        return True


class MemoryTarget:
    """Synthetic idempotent target; no PostgreSQL connection or writes."""
    def __init__(self):
        self.rows = {entity: {} for entity in ("proj", "visit", "my", "claim")}
        self.events = []
        self.runs = {}

    def verify_target(self):
        self.events.append("verify_target")

    def ensure_tables(self):
        self.events.append("ensure_tables")

    def start_run(self, run_id, project_count):
        self.runs[run_id] = {"status": "RUNNING", "project_count": project_count}
        self.events.append("start_run")

    def upsert_batch(self, run_id, entity, rows):
        written = 0
        skipped = 0
        from src.portal_mariadb_etl import ENTITY_SPECS
        spec = ENTITY_SPECS[entity]
        for row in rows:
            key = tuple(row[col] for col in spec.conflict_columns)
            if self.rows[entity].get(key) == dict(row):
                skipped += 1
            else:
                self.rows[entity][key] = dict(row)
                written += 1
        self.events.append("write:" + entity)
        return written, skipped

    def finish_run(self, run_id, status, summary):
        self.runs[run_id]["status"] = status
        self.events.append("finish_run")


def test_project_ids_are_explicit_bounded_and_not_deduplicated():
    assert validate_project_ids([7001, 7002]) == (7001, 7002)
    with pytest.raises(ETLError):
        validate_project_ids([])
    with pytest.raises(ETLError):
        validate_project_ids([7001, 7001])
    with pytest.raises(ETLError):
        validate_project_ids([0])
    with pytest.raises(ETLError):
        validate_project_ids(["07x"])


def test_readonly_privileges_accept_only_select_show_view_and_usage_in_j4b():
    validate_readonly_grants([
        ("GLOBAL", "USAGE", None, "NO"),
        ("SCHEMA", "SELECT", "j4b", "NO"),
        ("SCHEMA", "SHOW VIEW", "j4b", "NO"),
    ])
    for rows in (
        [("GLOBAL", "INSERT", None, "NO"), ("SCHEMA", "SELECT", "j4b", "NO")],
        [("SCHEMA", "SELECT", "other_db", "NO")],
        [("SCHEMA", "SELECT", "j4b", "YES")],
        [("GLOBAL", "USAGE", None, "NO")],
    ):
        with pytest.raises(SourceValidationError):
            validate_readonly_grants(rows)


def test_tls_gate_rejects_server_without_ssl_before_authentication():
    with pytest.raises(SecurityGateError, match="did not advertise TLS"):
        require_mysql_tls_capability(0)
    require_mysql_tls_capability(0x0800)


def test_tls_connection_mixin_rejects_missing_capability_before_super_auth():
    from src.portal_mariadb_etl import FailClosedTLSConnection
    with pytest.raises(SecurityGateError, match="did not advertise TLS"):
        FailClosedTLSConnection._request_authentication(SimpleNamespace(server_capabilities=0))


def test_sql_uses_allowlisted_columns_and_parameter_placeholders():
    sql = source_select_sql("proj", 2).lower()
    assert "where i in (%s,%s)" in sql
    assert "select *" not in sql
    assert not any(column in sql for column in ("client", "staff", "user", "name", "log.info"))
    assert source_count_sql("claim", 2).count("%s") == 4
    with pytest.raises(ETLError):
        source_select_sql("user", 1)


def test_query_plan_gate_allows_bounded_index_access_and_rejects_full_scans():
    validate_explain_plan(["id", "type", "rows"], [(1, "range", 8206)])
    for access, estimate in (("ALL", 190), ("index", 1000), ("mystery", 2), ("range", 50001)):
        with pytest.raises(SourceValidationError):
            validate_explain_plan(["id", "type", "rows"], [(1, access, estimate)])


def test_raw_dates_and_states_preserved_without_kpi_interpretation():
    my_row = _transform_row("my", {
        "project_id": 7001, "i": 8101, "visit": 9001, "state": 45,
        "dt": 1790900004, "dt_visit": 0,
    })
    assert my_row["raw_state"] == 45
    assert my_row["dt"] == 1790900004
    assert my_row["dt_visit"] == 0
    assert "successful" not in my_row
    assert "completed" not in my_row


def test_allowlisted_source_fields_exclude_personal_and_free_text_columns():
    from src.portal_mariadb_etl import ENTITY_SPECS
    denied = {"name", "client", "user", "staff", "usr", "info", "description", "phone", "email"}
    all_columns = {column.lower() for spec in ENTITY_SPECS.values() for column in spec.source_columns}
    assert all_columns.isdisjoint(denied)


def test_relations_and_claim_mismatch_are_reported_not_silently_repaired():
    qa = validate_dataset([7001], sample_data())
    assert qa["my_visit_broken"] == 0
    assert qa["claim_visit_missing"] == 1
    assert qa["claim_project_visit_mismatch"] == 1
    assert qa["claim_my_missing_in_selection"] == 0
    assert sample_data()["claim"][0]["claim_project_id"] == 7001
    assert sample_data()["claim"][0]["linked_visit_project_id"] == 7002


def test_broken_my_link_and_duplicate_source_keys_fail_before_target_writes():
    data = sample_data()
    data["my"][0]["visit_id"] = 5555
    with pytest.raises(SourceValidationError, match="my-to-visit"):
        validate_dataset([7001], data)
    data = sample_data()
    data["visit"].append(dict(data["visit"][0]))
    with pytest.raises(SourceValidationError, match="duplicate source visit"):
        validate_dataset([7001], data)


def test_visit_id_shared_across_projects_is_rejected_as_ambiguous():
    data = sample_data()
    data["proj"].append({
        "source_id": 7002, "planned_visits": 1, "raw_type": 8,
        "dt": None, "dt1": None, "dt2": None,
    })
    data["visit"].append({
        "project_id": 7002, "source_id": 9001, "dt": None, "dt1": None, "dt2": None,
    })
    with pytest.raises(SourceValidationError, match="maps to multiple selected projects"):
        validate_dataset([7001, 7002], data)


def test_second_import_is_idempotent_and_counts_unchanged_rows_as_skipped():
    source = FakeSource()
    target = MemoryTarget()
    guard = FakeGuard()
    runner = ETLRunner(source, target, guard, batch_size=1)
    first = runner.run([7001])
    counts_after_first = {entity: len(rows) for entity, rows in target.rows.items()}
    second = runner.run([7001])
    assert first.status == second.status == "SUCCEEDED"
    assert counts_after_first == {entity: len(rows) for entity, rows in target.rows.items()}
    assert all(value == 0 for value in second.written.values())
    assert second.skipped == second.read
    assert guard.calls >= 2 * (1 + 1 + sum(len(rows) for rows in sample_data().values()) + 1)


def test_guard_is_called_before_schema_run_log_each_batch_and_finalization():
    source = FakeSource()
    target = MemoryTarget()
    guard = FakeGuard()
    summary = ETLRunner(source, target, guard, batch_size=1).run([7001])
    assert summary.status == "SUCCEEDED"
    assert guard.calls == 1 + 1 + sum(len(rows) for rows in sample_data().values()) + 1
    assert target.events[0] == "verify_target"
    assert target.events[1:3] == ["ensure_tables", "start_run"]
    assert target.events[-1] == "finish_run"


def test_failed_guard_prevents_any_target_write():
    source = FakeSource()
    target = MemoryTarget()
    guard = FakeGuard([False])
    with pytest.raises(GuardBlocked):
        ETLRunner(source, target, guard, batch_size=1).run([7001])
    assert target.events == ["verify_target"]
    assert all(not rows for rows in target.rows.values())
    assert source.calls[-1] == "rollback"


def test_guard_stop_between_batches_prevents_the_next_batch_write():
    source = FakeSource()
    target = MemoryTarget()
    # Guard order: before DDL, before run row, before proj batch, then stop before visit batch.
    guard = FakeGuard([True, True, True, False])
    with pytest.raises(GuardBlocked):
        ETLRunner(source, target, guard, batch_size=1).run([7001])
    assert len(target.rows["proj"]) == 1
    assert len(target.rows["visit"]) == 0
    assert target.events.count("write:proj") == 1


def test_source_validation_failure_happens_before_any_target_write():
    class InvalidSource(FakeSource):
        def verify_readonly_privileges(self):
            raise SourceValidationError("fake read-only gate failure")

    target = MemoryTarget()
    with pytest.raises(ETLError):
        ETLRunner(InvalidSource(), target, FakeGuard()).run([7001])
    assert target.events == []


def test_invalid_batch_size_and_non_numeric_source_values_fail_closed():
    with pytest.raises(ETLError):
        ETLRunner(FakeSource(), MemoryTarget(), FakeGuard(), batch_size=5001)
    with pytest.raises(SourceValidationError):
        _transform_row("proj", {"i": "oops", "visits": 1, "typ": 2, "dt": None, "dt1": None, "dt2": None})


def test_ssh_fingerprint_check_requires_exact_preapproved_known_hosts_key(tmp_path):
    key_blob = b"synthetic public host key only"
    key_b64 = base64.b64encode(key_blob).decode("ascii")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key_blob).digest()).decode("ascii").rstrip("=")
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("test-host ssh-ed25519 " + key_b64 + "\n", encoding="utf-8")
    known_hosts.chmod(0o600)
    result = SimpleNamespace(returncode=0, stdout="test-host ssh-ed25519 " + key_b64 + "\n")
    with patch("src.portal_mariadb_etl.subprocess.run", return_value=result):
        verify_known_host_fingerprints("test-host", known_hosts, [fingerprint])
        with pytest.raises(SecurityGateError, match="does not match"):
            verify_known_host_fingerprints("test-host", known_hosts, ["SHA256:unconfirmed"])


def test_ssh_forward_uses_strict_host_checks_and_places_options_before_destination(tmp_path, monkeypatch):
    import src.portal_mariadb_etl as etl
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("synthetic\n", encoding="utf-8")
    known_hosts.chmod(0o600)
    monkeypatch.setattr(etl, "verify_known_host_fingerprints", lambda *a, **k: None)
    monkeypatch.setattr(etl.socket, "create_connection", lambda *a, **k: SimpleNamespace(close=lambda: None))
    captured = {}

    class Process:
        def poll(self):
            return None

        def terminate(self):
            captured["terminated"] = True

        def wait(self, timeout=None):
            return 0

    def fake_popen(args, **kwargs):
        captured["args"] = args
        return Process()

    monkeypatch.setattr(etl.subprocess, "Popen", fake_popen)
    with etl.open_ssh_forward(
        "dbhost", 22, "dbuser", known_hosts, ["SHA256:independently-approved"],
        "127.0.0.1", 3306,
    ) as endpoint:
        assert endpoint[0] == "127.0.0.1"
    args = captured["args"]
    destination = args[-1]
    assert destination == "dbuser@dbhost"
    assert args.index("-N") < args.index("-L") < len(args) - 1
    assert "StrictHostKeyChecking=yes" in args
    assert "BatchMode=yes" in args
    assert "StrictHostKeyChecking=no" not in args
    assert captured["terminated"] is True


def test_ssh_unknown_host_key_fails_before_opening_tunnel(tmp_path, monkeypatch):
    import src.portal_mariadb_etl as etl
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("", encoding="utf-8")
    known_hosts.chmod(0o600)
    monkeypatch.setattr(
        etl, "verify_known_host_fingerprints",
        lambda *a, **k: (_ for _ in ()).throw(SecurityGateError("not trusted")),
    )
    monkeypatch.setattr(etl.subprocess, "Popen", lambda *a, **k: pytest.fail("SSH must not start"))
    with pytest.raises(SecurityGateError):
        with etl.open_ssh_forward("unknown-host", 22, "user", known_hosts, (), "127.0.0.1", 3306):
            pass


def test_runtime_config_requires_owner_only_external_credentials_and_explicit_transports(tmp_path):
    source_password = tmp_path / "source.pass"
    target_password = tmp_path / "target.pass"
    known_hosts = tmp_path / "known_hosts"
    source_password.write_text("synthetic-only\n", encoding="utf-8")
    target_password.write_text("synthetic-only\n", encoding="utf-8")
    known_hosts.write_text("", encoding="utf-8")
    source_password.chmod(0o600)
    target_password.chmod(0o600)
    known_hosts.chmod(0o600)
    config_path = tmp_path / "runtime.ini"
    config_path.write_text(
        "[source]\ntransport=tls\nhost=lk.j4b.ru\nport=3306\ndatabase=j4b\n"
        "user=synthetic_readonly_user\npassword_file=%s\nverify_identity=true\n\n"
        "[target]\ntransport=ssh-tunnel\nhost=postgres\nport=5432\ndatabase=j4b_analytics\n"
        "user=j4b_analytics_loader\npassword_file=%s\nssh_host=example-vps\nssh_user=etl\n"
        "known_hosts_file=%s\nssh_host_fingerprints=SHA256:approved-placeholder\n\n"
        "[guard]\ncompose_project_dir=/home/service/app/j4b-analytics\n"
        "compose_file=j4b-analytics-compose.yaml\n"
        % (source_password, target_password, known_hosts),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    loaded = load_runtime_config(config_path)
    assert loaded.source_transport == "tls"
    assert loaded.target_host == "postgres"
    assert loaded.target_database == "j4b_analytics"
    assert loaded.ssh_host_fingerprints == ()
    assert _fingerprints("SHA256:one,SHA256:two") == ("SHA256:one", "SHA256:two")


def _target_config():
    return RuntimeConfig(
        source_transport="tls", source_host="lk.j4b.ru", source_port=3306,
        source_database="j4b", source_user="synthetic_readonly_user", source_password_file=Path("/tmp/source"),
        source_ca_file=None, ssh_host=None, ssh_port=22, ssh_user=None,
        ssh_jump_host=None, ssh_jump_user=None, known_hosts_file=None,
        ssh_host_fingerprints=(), ssh_jump_fingerprints=(), target_host="postgres",
        target_port=5432, target_database="j4b_analytics", target_user="j4b_analytics_loader",
        target_password_file=Path("/tmp/target"), target_ssh_host="example-vps",
        target_ssh_port=22, target_ssh_user="etl", target_ssh_jump_host=None,
        target_ssh_jump_user=None, target_known_hosts_file=Path("/tmp/known_hosts"),
        target_ssh_host_fingerprints=("SHA256:approved",), target_ssh_jump_fingerprints=(),
        compose_project_dir=Path("/home/service/app/j4b-analytics"),
        compose_file=Path("/home/service/app/j4b-analytics/j4b-analytics-compose.yaml"),
    )


def test_target_resolution_requires_internal_healthy_unpublished_postgres(monkeypatch):
    outputs = iter((
        "true|172.22.0.0/16;",
        "172.22.0.2",
        "healthy",
        "",
    ))
    monkeypatch.setattr("src.portal_mariadb_etl.run_approved_ssh_command", lambda *a, **k: next(outputs))
    assert resolve_postgres_container_ip(_target_config()) == "172.22.0.2"

    for bad_outputs in (
        ("false|172.22.0.0/16;", "172.22.0.2", "healthy", ""),
        ("true|172.22.0.0/16;", "172.22.0.2", "healthy", "0.0.0.0:5432"),
        ("true|172.22.0.0/16;", "172.23.0.2", "healthy", ""),
        ("true|172.22.0.0/16;", "172.22.0.2", "unhealthy", ""),
    ):
        output_iter = iter(bad_outputs)
        monkeypatch.setattr("src.portal_mariadb_etl.run_approved_ssh_command", lambda *a, _it=output_iter, **k: next(_it))
        with pytest.raises(SecurityGateError):
            resolve_postgres_container_ip(_target_config())


def test_storage_guard_requires_remote_allow_token(monkeypatch):
    from src.portal_mariadb_etl import ComposeStorageGuard
    config = _target_config()
    with patch("src.portal_mariadb_etl.run_approved_ssh_command", return_value="STORAGE_GUARD=ALLOW database_bytes=1"):
        assert ComposeStorageGuard(config).check() is True
    with patch("src.portal_mariadb_etl.run_approved_ssh_command", return_value="STORAGE_GUARD=STOP database_bytes=1"):
        assert ComposeStorageGuard(config).check() is False
    with patch("src.portal_mariadb_etl.run_approved_ssh_command", side_effect=SecurityGateError("x")):
        assert ComposeStorageGuard(config).check() is False


# --- deployment hardening: legacy opt-in, identity files, run lock ----------


def _legacy_config(tmp_path, *, opt_in=True, user="watch", identity_file=None):
    source_password = tmp_path / "source.pass"
    target_password = tmp_path / "target.pass"
    known_hosts = tmp_path / "known_hosts"
    source_password.write_text("synthetic-only\n", encoding="utf-8")
    target_password.write_text("synthetic-only\n", encoding="utf-8")
    known_hosts.write_text("", encoding="utf-8")
    source_password.chmod(0o600)
    target_password.chmod(0o600)
    known_hosts.chmod(0o600)
    identity_line = ""
    if identity_file is not None:
        identity_line = "ssh_identity_file=%s\n" % identity_file
    config_path = tmp_path / "legacy.ini"
    config_path.write_text(
        "[source]\ntransport=legacy-plain\nlegacy_plain_opt_in=%s\nhost=lk.j4b.ru\nport=3306\n"
        "database=j4b\nuser=%s\npassword_file=%s\n%s\n"
        "[target]\ntransport=ssh-tunnel\nhost=postgres\nport=5432\ndatabase=j4b_analytics\n"
        "user=j4b_analytics_loader\npassword_file=%s\nssh_host=example-vps\nssh_user=etl\n"
        "known_hosts_file=%s\nssh_host_fingerprints=SHA256:approved-placeholder\n\n"
        "[guard]\ncompose_project_dir=/home/service/app/j4b-analytics\n"
        "compose_file=j4b-analytics-compose.yaml\n"
        % (
            "true" if opt_in else "false", user, source_password,
            identity_line, target_password, known_hosts,
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    return config_path


def test_legacy_plain_requires_explicit_config_opt_in(tmp_path):
    from src.portal_mariadb_etl import load_runtime_config
    with pytest.raises(SecurityGateError):
        load_runtime_config(_legacy_config(tmp_path, opt_in=False))
    loaded = load_runtime_config(_legacy_config(tmp_path, opt_in=True))
    assert loaded.source_transport == "legacy-plain"


def test_legacy_plain_is_restricted_to_approved_readonly_account(tmp_path):
    from src.portal_mariadb_etl import load_runtime_config
    with pytest.raises(SecurityGateError):
        load_runtime_config(_legacy_config(tmp_path, opt_in=True, user="root"))


def test_legacy_plain_cli_gate_blocks_without_second_opt_in(tmp_path, capsys):
    from src.portal_mariadb_etl import main
    config_path = _legacy_config(tmp_path, opt_in=True)
    assert main(["--config", str(config_path), "--project-id", "8181"]) == 2
    assert "error_class=SecurityGateError" in capsys.readouterr().err
    assert main(["--config", str(config_path), "--project-id", "8181", "--allow-legacy-plain"]) == 0
    assert "PREPARATION_ONLY=YES" in capsys.readouterr().out


def test_cli_rejects_project_list_and_cohort_together_or_neither(tmp_path, capsys):
    from src.portal_mariadb_etl import main
    config_path = _legacy_config(tmp_path, opt_in=True)
    assert main(["--config", str(config_path), "--project-id", "8181", "--cohort-dynamic"]) == 2
    assert "error_class=UsageError" in capsys.readouterr().err
    assert main(["--config", str(config_path)]) == 2
    assert "error_class=UsageError" in capsys.readouterr().err
    assert main(["--config", str(_server_config(tmp_path)), "--cohort-dynamic", "--allow-legacy-plain"]) == 0
    assert "PREPARATION_ONLY=YES" in capsys.readouterr().out


def test_legacy_plain_connection_rejects_non_challenge_response_auth():
    from src.portal_mariadb_etl import LegacyPlainConnection

    class _FakeAuthBase:
        plugin = ""

        def _request_authentication(self):
            self._auth_plugin_name = self.plugin

    class _NativeBase(_FakeAuthBase):
        plugin = "mysql_native_password"

    class _ClearTextBase(_FakeAuthBase):
        plugin = "mysql_clear_password"

    native_cls = type("_NativeConn", (LegacyPlainConnection, _NativeBase), {})
    clear_cls = type("_ClearConn", (LegacyPlainConnection, _ClearTextBase), {})
    assert native_cls().set_character_set("utf8mb4") is None  # no implicit SET NAMES
    native_cls()._request_authentication()  # challenge-response: accepted
    with pytest.raises(SecurityGateError):
        clear_cls()._request_authentication()


def test_exclusive_run_lock_blocks_parallel_and_allows_sequential():
    from src.portal_mariadb_etl import exclusive_run_lock
    with exclusive_run_lock("test-channel-lock"):
        with pytest.raises(ETLError):
            with exclusive_run_lock("test-channel-lock"):
                pass
    with exclusive_run_lock("test-channel-lock"):
        pass
    with pytest.raises(ETLError):
        with exclusive_run_lock("../escape"):
            pass


def test_ssh_identity_file_is_validated_and_injected(tmp_path):
    import src.portal_mariadb_etl as etl
    key_file = tmp_path / "deploy_key"
    key_file.write_text("synthetic\n", encoding="utf-8")
    key_file.chmod(0o600)
    config = etl.load_runtime_config(_legacy_config(tmp_path, identity_file=key_file))
    assert config.ssh_identity_file == key_file.resolve()
    assert config.target_ssh_identity_file is None
    missing = tmp_path / "missing_key"
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub2").mkdir()
    with pytest.raises(SecurityGateError):
        etl.load_runtime_config(_legacy_config(tmp_path / "sub", identity_file=missing))
    loose = tmp_path / "loose_key"
    loose.write_text("synthetic\n", encoding="utf-8")
    loose.chmod(0o644)
    with pytest.raises(SecurityGateError):
        etl.load_runtime_config(_legacy_config(tmp_path / "sub2", identity_file=loose))
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("synthetic\n", encoding="utf-8")
    known_hosts.chmod(0o600)
    with patch.object(etl, "verify_known_host_fingerprints", lambda *a, **k: None):
        args = etl._approved_ssh_args(
            "dbhost", 22, "dbuser", known_hosts, ["SHA256:approved"],
            identity_file=key_file,
        )
    assert args[args.index("-i") + 1] == str(key_file)
    assert "IdentitiesOnly=yes" in args


def test_bounded_full_scan_allowed_only_within_explicit_bound():
    columns = ("type", "rows")
    assert validate_explain_plan(columns, [("ALL", 14_843)], allow_bounded_full_scan=True) is None
    with pytest.raises(SourceValidationError):
        validate_explain_plan(columns, [("ALL", 14_843)])
    with pytest.raises(SourceValidationError):
        validate_explain_plan(
            columns, [("ALL", 50_001)], allow_bounded_full_scan=True
        )
    with pytest.raises(SourceValidationError):
        validate_explain_plan(columns, [("ALL", -1)], allow_bounded_full_scan=True)


def _server_config(tmp_path, *, target_transport="direct", guard_transport="local"):
    source_password = tmp_path / "source.pass"
    target_password = tmp_path / "target.pass"
    source_password.write_text("synthetic-only\n", encoding="utf-8")
    target_password.write_text("synthetic-only\n", encoding="utf-8")
    source_password.chmod(0o600)
    target_password.chmod(0o600)
    config_path = tmp_path / "server.ini"
    config_path.write_text(
        "[source]\ntransport=legacy-plain\nlegacy_plain_opt_in=true\nhost=lk.j4b.ru\nport=3306\n"
        "database=j4b\nuser=watch\npassword_file=%s\n\n"
        "[target]\ntransport=%s\nhost=postgres\nport=5432\ndatabase=j4b_analytics\n"
        "user=j4b_analytics_loader\npassword_file=%s\n\n"
        "[guard]\ntransport=%s\ncompose_project_dir=/home/service/app/j4b-analytics\n"
        "compose_file=j4b-analytics-compose.yaml\n"
        % (source_password, target_transport, target_password, guard_transport),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    return config_path


def test_direct_target_and_local_guard_accepted_without_ssh_fields(tmp_path):
    from src.portal_mariadb_etl import load_runtime_config
    loaded = load_runtime_config(_server_config(tmp_path))
    assert loaded.target_transport == "direct"
    assert loaded.guard_transport == "local"


def test_direct_target_rejects_unknown_transport(tmp_path):
    from src.portal_mariadb_etl import load_runtime_config
    with pytest.raises(SecurityGateError):
        load_runtime_config(_server_config(tmp_path, target_transport="public-internet"))


def test_local_guard_runs_compose_without_ssh(tmp_path):
    from src.portal_mariadb_etl import ComposeStorageGuard, load_runtime_config
    loaded = load_runtime_config(_server_config(tmp_path))
    calls = []

    def fake_run(args, **kwargs):
        calls.append((tuple(args), kwargs.get("cwd")))
        return SimpleNamespace(returncode=0, stdout="STORAGE_GUARD=ALLOW free=1\n", stderr="")

    with patch("src.portal_mariadb_etl.subprocess.run", side_effect=fake_run):
        assert ComposeStorageGuard(loaded).check() is True
    args, cwd = calls[0]
    assert args[:2] == ("docker", "compose")
    assert "--profile" in args and "storage-guard" in args
    assert cwd == str(loaded.compose_project_dir)
