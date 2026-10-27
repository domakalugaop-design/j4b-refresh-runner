"""Guarded, bounded MariaDB-to-Analytics ETL preparation.

Nothing connects unless the caller supplies an external protected config and
explicitly passes --run-live. Source columns are allowlisted; values remain raw.
"""
from __future__ import annotations

import argparse
import base64
import configparser
import hashlib
import ipaddress
import os
import shlex
import socket
import ssl
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple


SOURCE_DATABASE = "j4b"
TARGET_DATABASE = "j4b_analytics"
TARGET_SCHEMA = "raw"
TARGET_USER = "j4b_analytics_loader"
MAX_PROJECT_IDS = 250
MAX_ROWS_PER_ENTITY = 50_000
DEFAULT_BATCH_SIZE = 500
MYSQL_CLIENT_SSL = 0x0800
MAX_ESTIMATED_ROWS_PER_QUERY = 50_000
ALLOWED_ACCESS_TYPES = {
    "SYSTEM", "CONST", "EQ_REF", "REF", "RANGE", "INDEX_MERGE",
    "REF_OR_NULL", "UNIQUE_SUBQUERY", "INDEX_SUBQUERY", "FULLTEXT",
}


@dataclass(frozen=True)
class EntitySpec:
    source_columns: Tuple[str, ...]
    target_table: str
    target_columns: Tuple[str, ...]
    conflict_columns: Tuple[str, ...]


ENTITY_SPECS: Mapping[str, EntitySpec] = {
    "proj": EntitySpec(
        source_columns=("i", "visits", "typ", "dt", "dt1", "dt2"),
        target_table="portal_mariadb_proj",
        target_columns=("source_id", "planned_visits", "raw_type", "dt", "dt1", "dt2"),
        conflict_columns=("source_id",),
    ),
    "visit": EntitySpec(
        source_columns=("i", "proj", "dt", "dt1", "dt2"),
        target_table="portal_mariadb_visit",
        target_columns=("project_id", "source_id", "dt", "dt1", "dt2"),
        conflict_columns=("project_id", "source_id"),
    ),
    "my": EntitySpec(
        source_columns=("i", "visit", "state", "dt", "dt_visit"),
        target_table="portal_mariadb_my",
        target_columns=("project_id", "source_id", "visit_id", "raw_state", "dt", "dt_visit"),
        conflict_columns=("project_id", "source_id"),
    ),
    "claim": EntitySpec(
        source_columns=("i", "proj", "visit", "my", "typ", "state", "dt", "repdt"),
        target_table="portal_mariadb_claim",
        target_columns=(
            "source_id", "claim_project_id", "linked_visit_project_id", "visit_id",
            "my_id", "raw_type", "raw_state", "dt", "repdt",
        ),
        conflict_columns=("source_id",),
    ),
}

COUNTER_COLUMNS: Mapping[str, Tuple[str, str, str]] = {
    "proj": ("proj_read", "proj_written", "proj_skipped"),
    "visit": ("visit_read", "visit_written", "visit_skipped"),
    "my": ("my_read", "my_written", "my_skipped"),
    "claim": ("claim_read", "claim_written", "claim_skipped"),
}

ALLOWED_SOURCE_PRIVILEGES = {"SELECT", "SHOW VIEW", "USAGE"}


class ETLError(RuntimeError):
    """Safe-to-report ETL error; messages must not contain config or row values."""


class SecurityGateError(ETLError):
    pass


class GuardBlocked(SecurityGateError):
    pass


class SourceValidationError(ETLError):
    pass


def validate_project_ids(project_ids: Sequence[int]) -> Tuple[int, ...]:
    """Require an explicit, unique, bounded numeric cohort; never infer one."""
    if not project_ids:
        raise ETLError("explicit project IDs are required; no cohort default exists")
    if len(project_ids) > MAX_PROJECT_IDS:
        raise ETLError("project ID count exceeds the bounded run limit")
    normalized: List[int] = []
    for value in project_ids:
        if isinstance(value, bool):
            raise ETLError("project IDs must be positive integers")
        try:
            project_id = int(value)
        except (TypeError, ValueError):
            raise ETLError("project IDs must be positive integers")
        if project_id <= 0 or project_id > (2**63 - 1) or str(project_id) != str(value).strip():
            raise ETLError("project IDs must be positive canonical integers")
        normalized.append(project_id)
    if len(set(normalized)) != len(normalized):
        raise ETLError("duplicate project IDs are rejected, not silently deduplicated")
    return tuple(normalized)


def validate_readonly_grants(
    rows: Iterable[Sequence[Any]], database: str = SOURCE_DATABASE
) -> None:
    """Validate SELECT-only database scope from INFORMATION_SCHEMA results.

    Each row is (scope, privilege_type, table_schema, is_grantable). No grant
    text or authentication material is retained or printed.
    """
    seen_select = False
    seen_any = False
    for row in rows:
        if len(row) < 4:
            raise SourceValidationError("source privilege metadata is incomplete")
        scope, privilege, schema_name, grantable = row[:4]
        scope = str(scope).upper()
        privilege = str(privilege).upper()
        schema_name = None if schema_name is None else str(schema_name)
        grantable = str(grantable).upper()
        seen_any = True
        if grantable not in {"NO", "0", "FALSE", "NONE"}:
            raise SourceValidationError("source grants include grant option")
        if scope == "GLOBAL":
            if privilege != "USAGE":
                raise SourceValidationError("source grants exceed read-only database scope")
            continue
        if scope not in {"SCHEMA", "TABLE", "COLUMN"}:
            raise SourceValidationError("source privilege scope is not allowlisted")
        if schema_name != database or privilege not in ALLOWED_SOURCE_PRIVILEGES:
            raise SourceValidationError("source grants exceed read-only database scope")
        if privilege == "SELECT":
            seen_select = True
    if not seen_any or not seen_select:
        raise SourceValidationError("no SELECT grant was confirmed for the source database")


def require_mysql_tls_capability(server_capabilities: int) -> None:
    """Fail before sending any authentication packet if server lacks TLS."""
    if not server_capabilities & MYSQL_CLIENT_SSL:
        raise SecurityGateError("MariaDB server did not advertise TLS; authentication aborted")


def validate_explain_plan(
    column_names: Sequence[str], rows: Iterable[Sequence[Any]],
    max_estimated_rows: int = MAX_ESTIMATED_ROWS_PER_QUERY,
) -> None:
    """Reject full scans, unknown plan shapes, or excessive estimated work."""
    indexes = {name.lower(): index for index, name in enumerate(column_names)}
    if "type" not in indexes or "rows" not in indexes:
        raise SourceValidationError("MariaDB EXPLAIN result lacks access-cost fields")
    observed = False
    for row in rows:
        observed = True
        access = str(row[indexes["type"]]).upper()
        estimate_value = row[indexes["rows"]]
        try:
            estimate = int(estimate_value)
        except (TypeError, ValueError):
            raise SourceValidationError("MariaDB EXPLAIN row estimate is unavailable") from None
        if access not in ALLOWED_ACCESS_TYPES:
            raise SourceValidationError("MariaDB query plan uses an unapproved access path")
        if estimate < 0 or estimate > max_estimated_rows:
            raise SourceValidationError("MariaDB query plan exceeds the estimated row-work bound")
    if not observed:
        raise SourceValidationError("MariaDB EXPLAIN returned no plan rows")


def _safe_file(path: Path, *, require_private: bool = True) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise SecurityGateError("protected configuration path is not a regular file")
    info = resolved.stat()
    if os.name == "posix":
        if require_private and info.st_uid != os.getuid():
            raise SecurityGateError("protected file owner does not match the ETL user")
        if not require_private and info.st_uid not in {0, os.getuid()}:
            raise SecurityGateError("trusted file must be owned by root or the ETL user")
        mode = stat.S_IMODE(info.st_mode)
        if require_private and mode & 0o077:
            raise SecurityGateError("protected file must not be accessible to group or others")
        if not require_private and mode & 0o022:
            raise SecurityGateError("trusted host-key file must not be group/other writable")
    return resolved


def _outside_repository(path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    try:
        path.resolve().relative_to(repo_root)
    except ValueError:
        pass
    else:
        raise SecurityGateError("credential-bearing files must remain outside the repository")
    resolved = path.resolve()
    if any((parent / ".git").exists() for parent in (resolved,) + tuple(resolved.parents)):
        raise SecurityGateError("credential-bearing files must remain outside all Git worktrees")


@dataclass(frozen=True)
class RuntimeConfig:
    source_transport: str
    source_host: str
    source_port: int
    source_database: str
    source_user: str
    source_password_file: Path
    source_ca_file: Optional[Path]
    ssh_host: Optional[str]
    ssh_port: int
    ssh_user: Optional[str]
    ssh_jump_host: Optional[str]
    ssh_jump_user: Optional[str]
    known_hosts_file: Optional[Path]
    ssh_host_fingerprints: Tuple[str, ...]
    ssh_jump_fingerprints: Tuple[str, ...]
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
    target_ssh_host_fingerprints: Tuple[str, ...]
    target_ssh_jump_fingerprints: Tuple[str, ...]
    compose_project_dir: Path
    compose_file: Path


def load_runtime_config(path: Path) -> RuntimeConfig:
    """Load external INI config while ensuring no credential file is in Git."""
    config_path = _safe_file(path)
    _outside_repository(config_path)
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            parser.read_file(handle)
        source = parser["source"]
        target = parser["target"]
        guard = parser["guard"]
        source_password = _safe_file(Path(source["password_file"]))
        target_password = _safe_file(Path(target["password_file"]))
        _outside_repository(source_password)
        _outside_repository(target_password)
        transport = source.get("transport", "").strip().lower()
        if transport not in {"tls", "ssh-tunnel"}:
            raise SecurityGateError("source transport must be TLS or a verified SSH tunnel")
        ca_text = source.get("ca_file", "").strip()
        ca_file = _safe_file(Path(ca_text), require_private=False) if ca_text else None
        known_text = source.get("known_hosts_file", "").strip()
        known_hosts = _safe_file(Path(known_text), require_private=False) if known_text else None
        host_fingerprints = _fingerprints(source.get("ssh_host_fingerprints", ""))
        jump_fingerprints = _fingerprints(source.get("ssh_jump_fingerprints", ""))
        target_known_text = target.get("known_hosts_file", "").strip()
        target_known_hosts = _safe_file(Path(target_known_text), require_private=False) if target_known_text else None
        target_host_fingerprints = _fingerprints(target.get("ssh_host_fingerprints", ""))
        target_jump_fingerprints = _fingerprints(target.get("ssh_jump_fingerprints", ""))
        project_dir = Path(guard["compose_project_dir"])
        compose_file = Path(guard.get("compose_file", "j4b-analytics-compose.yaml"))
        if not compose_file.is_absolute():
            compose_file = project_dir / compose_file
        if project_dir.as_posix() != "/home/service/app/j4b-analytics" or compose_file.name != "j4b-analytics-compose.yaml":
            raise SecurityGateError("storage guard must target the isolated Analytics Compose project")
        if source["database"] != SOURCE_DATABASE:
            raise SecurityGateError("source database is not the configured allowlisted database")
        if target["database"] != TARGET_DATABASE or target["user"] != TARGET_USER:
            raise SecurityGateError("target must be the dedicated Analytics loader identity")
        if target["host"] != "postgres" or int(target.get("port", "5432")) != 5432:
            raise SecurityGateError("target must use the private PostgreSQL Compose service endpoint")
        if target.get("transport", "") != "ssh-tunnel":
            raise SecurityGateError("PostgreSQL must be reached through the approved private SSH tunnel")
        if target_known_hosts is None or not target_host_fingerprints or not target.get("ssh_host") or not target.get("ssh_user"):
            raise SecurityGateError("target tunnel requires a trusted host key and independently approved fingerprint")
        if target.get("ssh_jump_host", "").strip() and not target_jump_fingerprints:
            raise SecurityGateError("target jump-host fingerprints must be independently approved")
        if transport == "tls":
            if source.getboolean("verify_identity", fallback=False) is not True:
                raise SecurityGateError("TLS certificate and hostname verification are mandatory")
        else:
            if known_hosts is None or not host_fingerprints or not source.get("ssh_host") or not source.get("ssh_user"):
                raise SecurityGateError("SSH tunnel requires a trusted host key, fingerprint, and target identity")
            if source.get("ssh_host", "").strip() != source.get("host", "lk.j4b.ru").strip():
                raise SecurityGateError("source SSH endpoint must be the MariaDB host itself")
            if source.get("ssh_jump_host", "").strip() and not jump_fingerprints:
                raise SecurityGateError("SSH jump host fingerprints must be independently approved")
        return RuntimeConfig(
            source_transport=transport,
            source_host=source.get("host", "lk.j4b.ru").strip(),
            source_port=int(source.get("port", "3306")),
            source_database=source["database"],
            source_user=source["user"],
            source_password_file=source_password,
            source_ca_file=ca_file,
            ssh_host=source.get("ssh_host", "").strip() or None,
            ssh_port=int(source.get("ssh_port", "22")),
            ssh_user=source.get("ssh_user", "").strip() or None,
            ssh_jump_host=source.get("ssh_jump_host", "").strip() or None,
            ssh_jump_user=source.get("ssh_jump_user", "").strip() or None,
            known_hosts_file=known_hosts,
            ssh_host_fingerprints=host_fingerprints,
            ssh_jump_fingerprints=jump_fingerprints,
            target_host=target["host"],
            target_port=int(target.get("port", "5432")),
            target_database=target["database"],
            target_user=target["user"],
            target_password_file=target_password,
            target_ssh_host=target["ssh_host"].strip(),
            target_ssh_port=int(target.get("ssh_port", "22")),
            target_ssh_user=target["ssh_user"].strip(),
            target_ssh_jump_host=target.get("ssh_jump_host", "").strip() or None,
            target_ssh_jump_user=target.get("ssh_jump_user", "").strip() or None,
            target_known_hosts_file=target_known_hosts,
            target_ssh_host_fingerprints=target_host_fingerprints,
            target_ssh_jump_fingerprints=target_jump_fingerprints,
            compose_project_dir=project_dir,
            compose_file=compose_file,
        )
    except (KeyError, configparser.Error, ValueError, OSError) as exc:
        if isinstance(exc, SecurityGateError):
            raise
        raise SecurityGateError("runtime config is incomplete or invalid") from None


def _fingerprints(value: str) -> Tuple[str, ...]:
    values = tuple(item.strip() for item in value.split(",") if item.strip())
    if any(not item.startswith("SHA256:") for item in values):
        raise SecurityGateError("SSH fingerprints must use OpenSSH SHA256 format")
    return values


def _approved_ssh_args(
    host: str,
    port: int,
    user: str,
    known_hosts_file: Path,
    fingerprints: Sequence[str],
    jump_host: Optional[str] = None,
    jump_user: Optional[str] = None,
    jump_fingerprints: Sequence[str] = (),
) -> List[str]:
    verify_known_host_fingerprints(host, known_hosts_file, fingerprints, port=port)
    if jump_host:
        verify_known_host_fingerprints(jump_host, known_hosts_file, jump_fingerprints)
    args = [
        "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "-o", "ExitOnForwardFailure=yes", "-o", "ForwardAgent=no",
        "-o", "ConnectTimeout=8", "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "UserKnownHostsFile=" + str(known_hosts_file), "-p", str(port),
    ]
    if jump_host:
        jump = jump_user + "@" + jump_host if jump_user else jump_host
        args.extend(["-J", jump])
    args.append(user + "@" + host)
    return args


def run_approved_ssh_command(
    host: str,
    port: int,
    user: str,
    known_hosts_file: Path,
    fingerprints: Sequence[str],
    remote_command: str,
    *,
    jump_host: Optional[str] = None,
    jump_user: Optional[str] = None,
    jump_fingerprints: Sequence[str] = (),
    timeout: int = 20,
) -> str:
    args = _approved_ssh_args(
        host, port, user, known_hosts_file, fingerprints,
        jump_host, jump_user, jump_fingerprints,
    )
    try:
        result = subprocess.run(
            args + [remote_command], capture_output=True, text=True,
            timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SecurityGateError("verified SSH command failed or timed out") from None
    if result.returncode != 0:
        raise SecurityGateError("verified SSH command returned a nonzero status")
    return result.stdout.strip()


@contextmanager
def open_ssh_forward(
    host: str,
    port: int,
    user: str,
    known_hosts_file: Path,
    fingerprints: Sequence[str],
    remote_host: str,
    remote_port: int,
    *,
    jump_host: Optional[str] = None,
    jump_user: Optional[str] = None,
    jump_fingerprints: Sequence[str] = (),
) -> Iterator[Tuple[str, int]]:
    if not fingerprints:
        raise SecurityGateError("independently approved SSH fingerprint is required")
    args = _approved_ssh_args(
        host, port, user, known_hosts_file, fingerprints,
        jump_host, jump_user, jump_fingerprints,
    )
    reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reservation.bind(("127.0.0.1", 0))
    local_port = reservation.getsockname()[1]
    reservation.close()
    destination = args.pop()
    args.extend([
        "-N", "-T", "-L",
        "127.0.0.1:%d:%s:%d" % (local_port, remote_host, remote_port),
    ])
    args.append(destination)
    try:
        process = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except OSError:
        raise SecurityGateError("OpenSSH could not be started") from None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise SecurityGateError("verified SSH tunnel did not start")
            try:
                probe = socket.create_connection(("127.0.0.1", local_port), timeout=0.25)
                probe.close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise SecurityGateError("verified SSH tunnel did not become ready")
        yield "127.0.0.1", local_port
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def read_password(path: Path) -> str:
    """Read a password from an external owner-only file; never log or return it."""
    safe_path = _safe_file(path)
    _outside_repository(safe_path)
    value = safe_path.read_text(encoding="utf-8").strip()
    if not value or "\n" in value or "\r" in value or "\x00" in value:
        raise SecurityGateError("protected password file must contain one non-empty line")
    return value


def verify_known_host_fingerprints(
    host: str,
    known_hosts_file: Path,
    expected_fingerprints: Sequence[str],
    *,
    port: int = 22,
    ssh_keygen: str = "ssh-keygen",
) -> None:
    """Compare existing known_hosts keys with independently approved fingerprints.

    This does not fetch keys, update known_hosts, or accept unknown keys.
    """
    if not expected_fingerprints:
        raise SecurityGateError("independent SSH host-key confirmation is required")
    safe_file = _safe_file(known_hosts_file, require_private=False)
    host_token = host if port == 22 else "[%s]:%d" % (host, port)
    try:
        result = subprocess.run(
            [ssh_keygen, "-F", host_token, "-f", str(safe_file)],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SecurityGateError("SSH host-key verification tool is unavailable") from None
    if result.returncode != 0:
        raise SecurityGateError("SSH host has no matching pre-existing known_hosts entry")
    observed = set()
    for line in result.stdout.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        key_b64 = parts[2]
        try:
            key_blob = base64.b64decode(key_b64, validate=True)
        except ValueError:
            continue
        digest = base64.b64encode(hashlib.sha256(key_blob).digest()).decode("ascii").rstrip("=")
        observed.add("SHA256:" + digest)
    if not observed or observed != set(expected_fingerprints):
        raise SecurityGateError("SSH host-key fingerprint does not match the independently approved set")


@contextmanager
def open_ssh_tunnel(config: RuntimeConfig) -> Iterator[Tuple[str, int]]:
    """Create a temporary, host-key-verified SSH forward to DB loopback.

    The SSH endpoint must be the MariaDB host itself; jump hosts are transport
    intermediaries, not the final forwarding endpoint.
    """
    if config.known_hosts_file is None or config.ssh_host is None or config.ssh_user is None:
        raise SecurityGateError("verified SSH tunnel configuration is incomplete")
    with open_ssh_forward(
        config.ssh_host, config.ssh_port, config.ssh_user, config.known_hosts_file,
        config.ssh_host_fingerprints, "127.0.0.1", config.source_port,
        jump_host=config.ssh_jump_host, jump_user=config.ssh_jump_user,
        jump_fingerprints=config.ssh_jump_fingerprints,
    ) as endpoint:
        yield endpoint


class FailClosedTLSConnection:
    """Factory mixin is dynamically combined with PyMySQL when needed."""

    def _request_authentication(self) -> None:
        require_mysql_tls_capability(self.server_capabilities)
        if not self.ssl or self.ctx.verify_mode != ssl.CERT_REQUIRED or not self.ctx.check_hostname:
            raise SecurityGateError("MariaDB TLS certificate/hostname verification is not enabled")
        super()._request_authentication()

    def set_character_set(self, charset: str, collation: Optional[str] = None) -> None:
        # The charset is already negotiated in the initial protocol packet.
        # Do not issue PyMySQL's implicit SET NAMES against production.
        return None


class NoImplicitSourceSQL:
    """Skip PyMySQL's implicit SET NAMES on a verified SSH-encrypted session."""
    def set_character_set(self, charset: str, collation: Optional[str] = None) -> None:
        return None


def connect_mariadb(config: RuntimeConfig) -> Any:
    """Open a read-only-capable source session; never fall back to plaintext."""
    import pymysql

    host, port = config.source_host, config.source_port
    tunnel = None
    tunnel_entered = False
    try:
        if config.source_transport == "ssh-tunnel":
            tunnel = open_ssh_tunnel(config)
            host, port = tunnel.__enter__()
            tunnel_entered = True
        password = read_password(config.source_password_file)
        context = None
        if config.source_transport == "tls":
            context = ssl.create_default_context(
                cafile=str(config.source_ca_file) if config.source_ca_file else None
            )
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
        base_cls = pymysql.connections.Connection
        if config.source_transport == "tls":
            connection_cls = type("_StrictTLSConnection", (FailClosedTLSConnection, base_cls), {})
        else:
            connection_cls = type("_SSHOnlyConnection", (NoImplicitSourceSQL, base_cls), {})
        conn = connection_cls(
            host=host,
            port=port,
            user=config.source_user,
            password=password,
            database=config.source_database,
            connect_timeout=8,
            read_timeout=30,
            write_timeout=8,
            charset="utf8mb4",
            ssl=context if config.source_transport == "tls" else None,
            autocommit=None,
            local_infile=False,
            cursorclass=pymysql.cursors.SSCursor,
        )
        conn._etl_tunnel = tunnel
        return conn
    except Exception:
        if tunnel is not None and tunnel_entered:
            tunnel.__exit__(*sys.exc_info())
        if isinstance(sys.exc_info()[1], SecurityGateError):
            raise
        raise SecurityGateError("MariaDB connection failed; credentials and provider errors suppressed") from None


def close_mariadb(conn: Any) -> None:
    tunnel = getattr(conn, "_etl_tunnel", None)
    try:
        conn.close()
    finally:
        if tunnel is not None:
            tunnel.__exit__(None, None, None)


def _in_placeholders(count: int) -> str:
    if count < 1:
        raise ETLError("empty project selection is not allowed")
    return ",".join(["%s"] * count)


def source_select_sql(entity: str, project_count: int) -> str:
    """Fixed column/table SQL; only placeholder count depends on ID count."""
    marks = _in_placeholders(project_count)
    if entity == "proj":
        return "SELECT i, visits, typ, dt, dt1, dt2 FROM proj WHERE i IN (" + marks + ")"
    if entity == "visit":
        return "SELECT i, proj, dt, dt1, dt2 FROM visit WHERE proj IN (" + marks + ")"
    if entity == "my":
        return (
            "SELECT v.proj AS project_id, m.i, m.visit, m.state, m.dt, m.dt_visit "
            "FROM `my` AS m INNER JOIN visit AS v ON v.i = m.visit "
            "WHERE v.proj IN (" + marks + ")"
        )
    if entity == "claim":
        return (
            "SELECT c.i, c.proj, v.proj AS linked_visit_project_id, c.visit, c.my, "
            "c.typ, c.state, c.dt, c.repdt FROM claim AS c "
            "LEFT JOIN visit AS v ON v.i = c.visit WHERE c.proj IN (" + marks + ") "
            "OR v.proj IN (" + marks + ")"
        )
    raise ETLError("unknown source entity")


def source_count_sql(entity: str, project_count: int) -> str:
    marks = _in_placeholders(project_count)
    if entity == "proj":
        return "SELECT COUNT(*) FROM proj WHERE i IN (" + marks + ")"
    if entity == "visit":
        return "SELECT COUNT(*) FROM visit WHERE proj IN (" + marks + ")"
    if entity == "my":
        return (
            "SELECT COUNT(*) FROM `my` AS m INNER JOIN visit AS v ON v.i=m.visit "
            "WHERE v.proj IN (" + marks + ")"
        )
    if entity == "claim":
        return (
            "SELECT COUNT(*) FROM claim AS c LEFT JOIN visit AS v ON v.i=c.visit "
            "WHERE c.proj IN (" + marks + ") OR v.proj IN (" + marks + ")"
        )
    raise ETLError("unknown source entity")


class MariaDBSource:
    def __init__(self, connection: Any, *, max_rows_per_entity: int = MAX_ROWS_PER_ENTITY):
        self.connection = connection
        self.max_rows_per_entity = max_rows_per_entity

    def _fetchone(self, sql: str, params: Sequence[Any] = ()) -> Sequence[Any]:
        with self.connection.cursor() as cursor:
            cursor.execute(sql, tuple(params))
            return cursor.fetchone()

    def begin_readonly_snapshot(self) -> None:
        # Requires transactional source tables; validated before extraction.
        with self.connection.cursor() as cursor:
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")

    def verify_readonly_privileges(self) -> None:
        grantee_row = self._fetchone(
            "SELECT CONCAT(QUOTE(SUBSTRING_INDEX(CURRENT_USER(), '@', 1)), '@', "
            "QUOTE(SUBSTRING_INDEX(CURRENT_USER(), '@', -1)))"
        )
        if not grantee_row or not grantee_row[0]:
            raise SourceValidationError("source account identity could not be verified")
        grantee = grantee_row[0]
        sql = (
            "SELECT 'GLOBAL' AS scope, PRIVILEGE_TYPE, NULL AS TABLE_SCHEMA, IS_GRANTABLE "
            "FROM information_schema.USER_PRIVILEGES WHERE GRANTEE=%s "
            "UNION ALL SELECT 'SCHEMA', PRIVILEGE_TYPE, TABLE_SCHEMA, IS_GRANTABLE "
            "FROM information_schema.SCHEMA_PRIVILEGES WHERE GRANTEE=%s "
            "UNION ALL SELECT 'TABLE', PRIVILEGE_TYPE, TABLE_SCHEMA, IS_GRANTABLE "
            "FROM information_schema.TABLE_PRIVILEGES WHERE GRANTEE=%s "
            "UNION ALL SELECT 'COLUMN', PRIVILEGE_TYPE, TABLE_SCHEMA, IS_GRANTABLE "
            "FROM information_schema.COLUMN_PRIVILEGES WHERE GRANTEE=%s"
        )
        with self.connection.cursor() as cursor:
            cursor.execute(sql, (grantee, grantee, grantee, grantee))
            validate_readonly_grants(cursor.fetchall(), SOURCE_DATABASE)
            cursor.execute("SELECT CURRENT_ROLE()")
            active_role = cursor.fetchone()[0]
        if active_role not in (None, "NONE", "none"):
            raise SourceValidationError("active source roles are not included in the verified grant contract")

    def validate_schema_and_engines(self) -> None:
        required = {
            table: set(spec.source_columns)
            for table, spec in ENTITY_SPECS.items()
        }
        table_names = tuple(required)
        marks = _in_placeholders(len(table_names))
        sql = (
            "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME IN (" + marks + ")"
        )
        with self.connection.cursor() as cursor:
            cursor.execute(sql, (SOURCE_DATABASE,) + table_names)
            columns: Dict[str, set] = {table: set() for table in table_names}
            for table_name, column_name in cursor.fetchall():
                if table_name in columns:
                    columns[table_name].add(column_name)
            for table_name, expected in required.items():
                if not expected.issubset(columns[table_name]):
                    raise SourceValidationError("source table schema differs from the allowlisted contract")
            cursor.execute(
                "SELECT TABLE_NAME, ENGINE FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME IN (" + marks + ")",
                (SOURCE_DATABASE,) + table_names,
            )
            engines = {table: engine for table, engine in cursor.fetchall()}
        if set(engines) != set(table_names) or any(
            str(engines[table]).upper() != "INNODB" for table in table_names
        ):
            raise SourceValidationError("all selected source tables must support InnoDB consistent snapshots")

    def validate_query_plans(self, project_ids: Sequence[int]) -> None:
        """Run read-only EXPLAIN for every extraction/count query before row reads."""
        for entity in ENTITY_SPECS:
            queries = (source_select_sql(entity, len(project_ids)), source_count_sql(entity, len(project_ids)))
            params = tuple(project_ids) * (2 if entity == "claim" else 1)
            for query in queries:
                with self.connection.cursor() as cursor:
                    cursor.execute("EXPLAIN " + query, params)
                    columns = [description[0] for description in cursor.description]
                    validate_explain_plan(columns, cursor.fetchall())

    def preflight(self, project_ids: Sequence[int]) -> Dict[str, int]:
        marks = _in_placeholders(len(project_ids))
        project_rows = self._fetchone(
            "SELECT COUNT(*) FROM proj WHERE i IN (" + marks + ")", project_ids
        )
        if int(project_rows[0]) != len(project_ids):
            raise SourceValidationError("one or more explicitly selected projects are absent or non-unique")
        counts: Dict[str, int] = {}
        for entity in ENTITY_SPECS:
            params = tuple(project_ids) * (2 if entity == "claim" else 1)
            row = self._fetchone(source_count_sql(entity, len(project_ids)), params)
            counts[entity] = int(row[0])
            if counts[entity] > self.max_rows_per_entity:
                raise SourceValidationError("bounded source row limit exceeded before target writes")
        return counts

    def extract(self, project_ids: Sequence[int], batch_size: int) -> Dict[str, List[Dict[str, Any]]]:
        result: Dict[str, List[Dict[str, Any]]] = {}
        for entity in ENTITY_SPECS:
            params = tuple(project_ids) * (2 if entity == "claim" else 1)
            rows: List[Dict[str, Any]] = []
            with self.connection.cursor() as cursor:
                cursor.execute(source_select_sql(entity, len(project_ids)), params)
                names = [description[0] for description in cursor.description]
                while True:
                    batch = cursor.fetchmany(batch_size)
                    if not batch:
                        break
                    for values in batch:
                        rows.append(dict(zip(names, values)))
                        if len(rows) > self.max_rows_per_entity:
                            raise SourceValidationError("bounded source row limit exceeded")
            result[entity] = [_transform_row(entity, row) for row in rows]
        return result

    def close_snapshot(self) -> None:
        try:
            self.connection.rollback()
        except Exception:
            pass


def _int_or_none(value: Any, field_name: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise SourceValidationError("unexpected non-integer source value in allowlisted numeric field")
    if value < -(2**63) or value > (2**63 - 1):
        raise SourceValidationError("numeric source value exceeds PostgreSQL BIGINT range")
    return value


def _transform_row(entity: str, row: Mapping[str, Any]) -> Dict[str, Any]:
    """Map only approved identifiers, raw states, raw epoch values, and plan/type codes."""
    if entity == "proj":
        return {
            "source_id": _int_or_none(row["i"], "i"),
            "planned_visits": _int_or_none(row["visits"], "visits"),
            "raw_type": _int_or_none(row["typ"], "typ"),
            "dt": _int_or_none(row["dt"], "dt"),
            "dt1": _int_or_none(row["dt1"], "dt1"),
            "dt2": _int_or_none(row["dt2"], "dt2"),
        }
    if entity == "visit":
        return {
            "project_id": _int_or_none(row["proj"], "proj"),
            "source_id": _int_or_none(row["i"], "i"),
            "dt": _int_or_none(row["dt"], "dt"),
            "dt1": _int_or_none(row["dt1"], "dt1"),
            "dt2": _int_or_none(row["dt2"], "dt2"),
        }
    if entity == "my":
        return {
            "project_id": _int_or_none(row["project_id"], "project_id"),
            "source_id": _int_or_none(row["i"], "i"),
            "visit_id": _int_or_none(row["visit"], "visit"),
            "raw_state": _int_or_none(row["state"], "state"),
            "dt": _int_or_none(row["dt"], "dt"),
            "dt_visit": _int_or_none(row["dt_visit"], "dt_visit"),
        }
    if entity == "claim":
        return {
            "source_id": _int_or_none(row["i"], "i"),
            "claim_project_id": _int_or_none(row["proj"], "proj"),
            "linked_visit_project_id": _int_or_none(row["linked_visit_project_id"], "linked_visit_project_id"),
            "visit_id": _int_or_none(row["visit"], "visit"),
            "my_id": _int_or_none(row["my"], "my"),
            "raw_type": _int_or_none(row["typ"], "typ"),
            "raw_state": _int_or_none(row["state"], "state"),
            "dt": _int_or_none(row["dt"], "dt"),
            "repdt": _int_or_none(row["repdt"], "repdt"),
        }
    raise ETLError("unknown source entity")


def validate_dataset(
    project_ids: Sequence[int], datasets: Mapping[str, Sequence[Mapping[str, Any]]]
) -> Dict[str, int]:
    """Validate uniqueness and links; retain claim-project conflicts as QA counts."""
    projects = {row["source_id"] for row in datasets["proj"]}
    if projects != set(project_ids):
        raise SourceValidationError("selected project set does not match source projection")
    visits: Dict[Tuple[int, int], Mapping[str, Any]] = {}
    visit_id_to_projects: Dict[int, set] = {}
    duplicate_count = 0
    for row in datasets["visit"]:
        key = (row["project_id"], row["source_id"])
        if key in visits:
            duplicate_count += 1
        visits[key] = row
        visit_id_to_projects.setdefault(row["source_id"], set()).add(row["project_id"])
    if duplicate_count:
        raise SourceValidationError("duplicate source visit keys detected")
    if any(len(projects_for_id) > 1 for projects_for_id in visit_id_to_projects.values()):
        raise SourceValidationError("visit ID maps to multiple selected projects; source links are ambiguous")
    for row in datasets["visit"]:
        if row["project_id"] not in projects:
            raise SourceValidationError("visit references a project outside the explicit selection")

    seen_keys: Dict[str, set] = {entity: set() for entity in ENTITY_SPECS}
    for entity, rows in datasets.items():
        action_projects: Dict[int, set] = {}
        for row in rows:
            spec = ENTITY_SPECS[entity]
            if any(row[column] is None for column in spec.conflict_columns):
                raise SourceValidationError("source primary key contains NULL")
            key = tuple(row[column] for column in spec.conflict_columns)
            if key in seen_keys[entity]:
                raise SourceValidationError("duplicate source entity key detected")
            seen_keys[entity].add(key)
            if entity == "my":
                action_projects.setdefault(row["source_id"], set()).add(row["project_id"])
        if entity == "my" and any(len(projects_for_action) > 1 for projects_for_action in action_projects.values()):
            raise SourceValidationError("source action ID maps to multiple projects")
    my_broken = 0
    for row in datasets["my"]:
        key = (row["project_id"], row["visit_id"])
        if key not in visits:
            my_broken += 1
    if my_broken:
        raise SourceValidationError("my-to-visit relation failed validation")
    claim_missing_visit = 0
    claim_project_mismatch = 0
    my_ids = {row["source_id"] for row in datasets["my"]}
    claim_missing_my = 0
    for row in datasets["claim"]:
        linked_project = row["linked_visit_project_id"]
        claim_project = row["claim_project_id"]
        if linked_project is None:
            claim_missing_visit += 1
        elif claim_project is not None and linked_project != claim_project:
            claim_project_mismatch += 1
        if row["my_id"] is not None and row["my_id"] not in my_ids:
            claim_missing_my += 1
    return {
        "duplicate_source_keys": duplicate_count,
        "my_visit_broken": my_broken,
        "claim_visit_missing": claim_missing_visit,
        "claim_project_visit_mismatch": claim_project_mismatch,
        "claim_my_missing_in_selection": claim_missing_my,
    }


def chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    if size < 1:
        raise ValueError("batch size must be positive")
    for start in range(0, len(items), size):
        yield items[start:start + size]


@dataclass
class RunSummary:
    run_id: str
    status: str
    project_count: int
    read: Dict[str, int] = field(default_factory=dict)
    written: Dict[str, int] = field(default_factory=dict)
    skipped: Dict[str, int] = field(default_factory=dict)
    errors: int = 0
    qa: Dict[str, int] = field(default_factory=dict)


class ETLRunner:
    """Run one explicit cohort; dependencies are injectable for offline tests."""
    def __init__(
        self,
        source: Any,
        target: Any,
        guard: Any,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_rows_per_entity: int = MAX_ROWS_PER_ENTITY,
    ):
        if batch_size < 1 or batch_size > 5_000:
            raise ETLError("batch size is outside its safe bound")
        self.source = source
        self.target = target
        self.guard = guard
        self.batch_size = batch_size
        self.max_rows_per_entity = max_rows_per_entity

    def _guard(self) -> None:
        result = self.guard.check()
        if result is not True:
            raise GuardBlocked("storage guard did not return ALLOW; no next write was performed")

    def run(self, selected_project_ids: Sequence[int]) -> RunSummary:
        project_ids = validate_project_ids(selected_project_ids)
        run_id = str(uuid.uuid4())
        summary = RunSummary(run_id=run_id, status="PREPARING", project_count=len(project_ids))
        run_started = False
        try:
            self.source.begin_readonly_snapshot()
            self.source.verify_readonly_privileges()
            self.source.validate_schema_and_engines()
            self.source.validate_query_plans(project_ids)
            plan = self.source.preflight(project_ids)
            data = self.source.extract(project_ids, self.batch_size)
            summary.read = {entity: len(data[entity]) for entity in ENTITY_SPECS}
            if summary.read != plan:
                raise SourceValidationError("source preflight counts changed during the read snapshot")
            summary.qa = validate_dataset(project_ids, data)
            self.target.verify_target()
            self._guard()
            self.target.ensure_tables()
            self._guard()
            self.target.start_run(run_id, len(project_ids))
            run_started = True
            summary.status = "RUNNING"
            for entity in ENTITY_SPECS:
                summary.written[entity] = 0
                summary.skipped[entity] = 0
                for batch in chunks(data[entity], self.batch_size):
                    self._guard()
                    written, skipped = self.target.upsert_batch(run_id, entity, batch)
                    summary.written[entity] += written
                    summary.skipped[entity] += skipped
            self._guard()
            self.target.finish_run(run_id, "SUCCEEDED", summary)
            summary.status = "SUCCEEDED"
            return summary
        except GuardBlocked:
            summary.status = "STOPPED_BY_GUARD"
            summary.errors += 1
            raise
        except Exception as exc:
            summary.status = "FAILED"
            summary.errors += 1
            if run_started:
                try:
                    self._guard()
                    self.target.finish_run(run_id, "FAILED", summary)
                except Exception:
                    # Do not bypass a failed guard just to finalize a journal row.
                    pass
            # Failure text is intentionally not surfaced; it may contain SQL or values.
            raise ETLError("ETL stopped before further writes; inspect only redacted error class") from None
        finally:
            self.source.close_snapshot()


class ComposeStorageGuard:
    def __init__(self, config: RuntimeConfig, *, timeout: int = 30):
        self.config = config
        self.timeout = timeout

    def check(self) -> bool:
        try:
            output = run_approved_ssh_command(
                self.config.target_ssh_host,
                self.config.target_ssh_port,
                self.config.target_ssh_user,
                self.config.target_known_hosts_file,
                self.config.target_ssh_host_fingerprints,
                "cd /home/service/app/j4b-analytics && "
                "docker compose -f j4b-analytics-compose.yaml --profile operations "
                "run --rm storage-guard",
                jump_host=self.config.target_ssh_jump_host,
                jump_user=self.config.target_ssh_jump_user,
                jump_fingerprints=self.config.target_ssh_jump_fingerprints,
                timeout=self.timeout,
            )
        except SecurityGateError:
            return False
        lines = [line.strip() for line in output.splitlines()]
        return any(line.startswith("STORAGE_GUARD=ALLOW ") for line in lines)


def resolve_postgres_container_ip(config: RuntimeConfig) -> str:
    """Read-only inspect: require healthy PG on the existing internal network only."""
    host_args = (
        config.target_ssh_host,
        config.target_ssh_port,
        config.target_ssh_user,
        config.target_known_hosts_file,
        config.target_ssh_host_fingerprints,
    )
    common = {
        "jump_host": config.target_ssh_jump_host,
        "jump_user": config.target_ssh_jump_user,
        "jump_fingerprints": config.target_ssh_jump_fingerprints,
        "timeout": 15,
    }
    network_template = "{{.Internal}}|{{range .IPAM.Config}}{{.Subnet}};{{end}}"
    ip_template = (
        '{{range $name, $network := .NetworkSettings.Networks}}'
        '{{if eq $name "j4b_analytics_internal"}}{{$network.IPAddress}}{{end}}{{end}}'
    )
    network_info = run_approved_ssh_command(
        *host_args,
        "docker network inspect --format " + shlex.quote(network_template) + " j4b_analytics_internal",
        **common,
    )
    try:
        internal, raw_subnets = network_info.split("|", 1)
        subnets = [ipaddress.ip_network(item) for item in raw_subnets.split(";") if item]
    except (ValueError, TypeError):
        raise SecurityGateError("Analytics internal network metadata is invalid") from None
    if internal.lower() != "true" or not subnets:
        raise SecurityGateError("Analytics PostgreSQL network is not verified internal")
    ip_text = run_approved_ssh_command(
        *host_args,
        "docker inspect --format " + shlex.quote(ip_template) + " j4b-analytics-postgres-1",
        **common,
    )
    try:
        container_ip = ipaddress.ip_address(ip_text)
    except ValueError:
        raise SecurityGateError("Analytics PostgreSQL private address is invalid") from None
    if not any(container_ip in subnet for subnet in subnets):
        raise SecurityGateError("Analytics PostgreSQL address is outside its private network")
    health = run_approved_ssh_command(
        *host_args,
        "docker inspect --format " + shlex.quote("{{.State.Health.Status}}") + " j4b-analytics-postgres-1",
        **common,
    )
    if health != "healthy":
        raise SecurityGateError("Analytics PostgreSQL is not healthy")
    ports = run_approved_ssh_command(*host_args, "docker port j4b-analytics-postgres-1", **common)
    if ports.strip():
        raise SecurityGateError("Analytics PostgreSQL has an unexpected published port")
    return str(container_ip)


@contextmanager
def open_postgres_tunnel(config: RuntimeConfig) -> Iterator[Tuple[str, int]]:
    container_ip = resolve_postgres_container_ip(config)
    with open_ssh_forward(
        config.target_ssh_host,
        config.target_ssh_port,
        config.target_ssh_user,
        config.target_known_hosts_file,
        config.target_ssh_host_fingerprints,
        str(container_ip),
        config.target_port,
        jump_host=config.target_ssh_jump_host,
        jump_user=config.target_ssh_jump_user,
        jump_fingerprints=config.target_ssh_jump_fingerprints,
    ) as endpoint:
        yield endpoint


class PostgresTarget:
    """PostgreSQL writer restricted to the private Analytics service identity."""
    def __init__(self, connection: Any):
        self.connection = connection

    def verify_target(self) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_user")
            row = cursor.fetchone()
            cursor.execute("SELECT schema_name FROM information_schema.schemata WHERE schema_name=%s", (TARGET_SCHEMA,))
            schema = cursor.fetchone()
        if row != (TARGET_DATABASE, TARGET_USER) or schema is None:
            raise SecurityGateError("target is not the dedicated Analytics loader endpoint")

    def ensure_tables(self) -> None:
        ddl = [
            """CREATE TABLE IF NOT EXISTS raw.portal_mariadb_proj (
                source_id BIGINT PRIMARY KEY, planned_visits BIGINT, raw_type BIGINT,
                dt BIGINT, dt1 BIGINT, dt2 BIGINT)""",
            """CREATE TABLE IF NOT EXISTS raw.portal_mariadb_visit (
                project_id BIGINT NOT NULL REFERENCES raw.portal_mariadb_proj(source_id),
                source_id BIGINT NOT NULL, dt BIGINT, dt1 BIGINT, dt2 BIGINT,
                PRIMARY KEY (project_id, source_id))""",
            """CREATE TABLE IF NOT EXISTS raw.portal_mariadb_my (
                project_id BIGINT NOT NULL, source_id BIGINT NOT NULL, visit_id BIGINT NOT NULL,
                raw_state BIGINT, dt BIGINT, dt_visit BIGINT,
                PRIMARY KEY (project_id, source_id),
                FOREIGN KEY (project_id, visit_id) REFERENCES raw.portal_mariadb_visit(project_id, source_id))""",
            """CREATE TABLE IF NOT EXISTS raw.portal_mariadb_claim (
                source_id BIGINT PRIMARY KEY, claim_project_id BIGINT, linked_visit_project_id BIGINT,
                visit_id BIGINT, my_id BIGINT, raw_type BIGINT, raw_state BIGINT, dt BIGINT, repdt BIGINT)""",
            """CREATE TABLE IF NOT EXISTS raw.portal_mariadb_etl_runs (
                run_id UUID PRIMARY KEY, started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                finished_at TIMESTAMPTZ, status TEXT NOT NULL, project_count INTEGER NOT NULL,
                proj_read BIGINT NOT NULL DEFAULT 0, proj_written BIGINT NOT NULL DEFAULT 0, proj_skipped BIGINT NOT NULL DEFAULT 0,
                visit_read BIGINT NOT NULL DEFAULT 0, visit_written BIGINT NOT NULL DEFAULT 0, visit_skipped BIGINT NOT NULL DEFAULT 0,
                my_read BIGINT NOT NULL DEFAULT 0, my_written BIGINT NOT NULL DEFAULT 0, my_skipped BIGINT NOT NULL DEFAULT 0,
                claim_read BIGINT NOT NULL DEFAULT 0, claim_written BIGINT NOT NULL DEFAULT 0, claim_skipped BIGINT NOT NULL DEFAULT 0,
                error_count INTEGER NOT NULL DEFAULT 0, qa JSONB NOT NULL DEFAULT '{}'::jsonb)""",
        ]
        with self.connection:
            with self.connection.cursor() as cursor:
                for statement in ddl:
                    cursor.execute(statement)

    def start_run(self, run_id: str, project_count: int) -> None:
        with self.connection:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO raw.portal_mariadb_etl_runs(run_id,status,project_count) VALUES (%s,%s,%s)",
                    (run_id, "RUNNING", project_count),
                )

    def upsert_batch(
        self, run_id: str, entity: str, rows: Sequence[Mapping[str, Any]]
    ) -> Tuple[int, int]:
        spec = ENTITY_SPECS[entity]
        columns = spec.target_columns
        table = "raw." + spec.target_table
        column_sql = ",".join(columns)
        conflict_sql = ",".join(spec.conflict_columns)
        update_columns = [column for column in columns if column not in spec.conflict_columns]
        assignments = ",".join("%s=EXCLUDED.%s" % (column, column) for column in update_columns)
        changed_predicate = " OR ".join(
            "%s IS DISTINCT FROM EXCLUDED.%s" % (table + "." + col, col) for col in update_columns
        )
        sql = (
            "INSERT INTO %s (%s) VALUES %%s ON CONFLICT (%s) DO UPDATE SET %s "
            "WHERE %s RETURNING 1"
        ) % (table, column_sql, conflict_sql, assignments, changed_predicate)
        values = [tuple(row[column] for column in columns) for row in rows]
        try:
            from psycopg2.extras import execute_values
        except ImportError:
            raise ETLError("psycopg2 is required by the live PostgreSQL writer") from None
        with self.connection:
            with self.connection.cursor() as cursor:
                changed = execute_values(cursor, sql, values, page_size=len(values), fetch=True)
                written = len(changed)
                skipped = len(values) - written
                read_col, written_col, skipped_col = COUNTER_COLUMNS[entity]
                cursor.execute(
                    "UPDATE raw.portal_mariadb_etl_runs SET %s=%s+%%s,%s=%s+%%s,%s=%s+%%s WHERE run_id=%%s"
                    % (read_col, read_col, written_col, written_col, skipped_col, skipped_col),
                    (len(values), written, skipped, run_id),
                )
        return written, skipped

    def finish_run(self, run_id: str, status: str, summary: RunSummary) -> None:
        try:
            from psycopg2.extras import Json
        except ImportError:
            raise ETLError("psycopg2 is required by the live PostgreSQL writer") from None
        with self.connection:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE raw.portal_mariadb_etl_runs SET status=%s,finished_at=%s,error_count=%s,qa=%s WHERE run_id=%s",
                    (status, datetime.now(timezone.utc), summary.errors, Json(summary.qa), run_id),
                )


def _json_dumps(value: Mapping[str, Any]) -> str:
    import json
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def connect_postgres(config: RuntimeConfig, endpoint: Tuple[str, int]) -> Any:
    """Connect only through the host-key-verified forward to the private container."""
    password = read_password(config.target_password_file)
    try:
        import psycopg2
        return psycopg2.connect(
            host=endpoint[0],
            port=endpoint[1],
            dbname=config.target_database,
            user=config.target_user,
            password=password,
            connect_timeout=5,
            sslmode="disable",  # private internal Compose network only
        )
    except ImportError:
        raise ETLError("psycopg2 is required by the live PostgreSQL writer") from None
    except Exception:
        raise SecurityGateError("Analytics PostgreSQL connection failed; details suppressed") from None


def run_live(config: RuntimeConfig, project_ids: Sequence[int], batch_size: int) -> RunSummary:
    with open_postgres_tunnel(config) as target_endpoint:
        source_connection = connect_mariadb(config)
        target_connection = None
        try:
            target_connection = connect_postgres(config, target_endpoint)
            runner = ETLRunner(
                MariaDBSource(source_connection),
                PostgresTarget(target_connection),
                ComposeStorageGuard(config),
                batch_size=batch_size,
            )
            return runner.run(project_ids)
        finally:
            close_mariadb(source_connection)
            if target_connection is not None:
                target_connection.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Bounded, guarded Portal MariaDB ETL")
    parser.add_argument("--config", required=True, type=Path, help="external owner-only INI; never store in Git")
    parser.add_argument("--project-id", action="append", required=True, type=int, help="explicit selected project ID; repeatable")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--run-live", action="store_true", help="explicitly allow source read and Analytics writes")
    args = parser.parse_args(argv)
    try:
        project_ids = validate_project_ids(args.project_id)
        config = load_runtime_config(args.config)
        if not args.run_live:
            print("PREPARATION_ONLY=YES project_id_count=%d live_connections=NO" % len(project_ids))
            return 0
        summary = run_live(config, project_ids, args.batch_size)
        print(
            "ETL_STATUS=%s run_id=%s projects=%d read=%s written=%s skipped=%s qa=%s"
            % (
                summary.status, summary.run_id, summary.project_count,
                _json_dumps(summary.read), _json_dumps(summary.written),
                _json_dumps(summary.skipped), _json_dumps(summary.qa),
            )
        )
        return 0
    except GuardBlocked:
        print("ETL_STATUS=STOPPED_BY_GUARD writes_after_stop=0", file=sys.stderr)
        return 3
    except ETLError as exc:
        print("ETL_STATUS=BLOCKED error_class=%s" % type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
