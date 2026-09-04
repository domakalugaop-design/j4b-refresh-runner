#!/usr/bin/env python3
"""Fail CI if obviously private material is committed to this public repository.

This is a defensive guard, not a substitute for human review or GitHub secret scanning.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_BASENAMES = {
    ".env",
    "credentials.json",
    "service-account.json",
    "service_account.json",
}

FORBIDDEN_SUFFIXES = {
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".xlsx",
    ".xls",
    ".parquet",
    ".ndjson",
    ".jsonl",
}

FORBIDDEN_PATH_PARTS = {
    "artifacts",
    "backups",
    "snapshots",
    "outputs",
    "output",
    "logs",
    "cache",
}

# Deliberately conservative high-signal patterns only.
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password)\s*[=:]\s*['\"][^'\"]{8,}['\"]"),
]

TEXT_SCAN_MAX_BYTES = 2_000_000


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    )
    return [ROOT / p.decode("utf-8") for p in result.stdout.split(b"\0") if p]


def is_allowed_example(path: Path) -> bool:
    rel = path.relative_to(ROOT).as_posix()
    return rel == ".env.example"


def main() -> int:
    violations: list[str] = []

    for path in tracked_files():
        rel = path.relative_to(ROOT)
        rel_parts = set(rel.parts[:-1])
        name = path.name.lower()

        if not is_allowed_example(path):
            if name in FORBIDDEN_BASENAMES:
                violations.append(f"forbidden filename: {rel}")
            if path.suffix.lower() in FORBIDDEN_SUFFIXES:
                violations.append(f"forbidden artifact type: {rel}")
            if rel_parts & FORBIDDEN_PATH_PARTS:
                violations.append(f"forbidden runtime-artifact directory: {rel}")

        try:
            if path.stat().st_size > TEXT_SCAN_MAX_BYTES:
                continue
            raw = path.read_bytes()
            if b"\0" in raw:
                continue
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                violations.append(f"possible secret pattern: {rel}")
                break

    if violations:
        print("PUBLIC_REPO_SAFETY_CHECK=FAIL")
        for violation in sorted(set(violations)):
            print(f"- {violation}")
        return 1

    print("PUBLIC_REPO_SAFETY_CHECK=PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
