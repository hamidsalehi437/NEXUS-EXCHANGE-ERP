#!/usr/bin/env bash
# =============================================================================
# NEXUS EXCHANGE ERP — generate a local .env with strong secrets
# =============================================================================
# Creates <repo>/.env from .env.example when it does not exist, then fills in every
# secret field with an independent cryptographically random value. Idempotent: an
# existing .env is never overwritten (pass --force to replace it).
#
# Every replacement is line-oriented on `KEY=`, so a value in the template can be
# edited without breaking the generator, and no credential ever becomes a
# substring of another one.
#
# The file is written with mode 600 and is git-ignored. Secrets are printed only
# as a fingerprint, never as values.
#
# Usage:
#   scripts/gen_env.sh            # create .env if missing
#   scripts/gen_env.sh --force    # rotate everything, replacing .env
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT_DIR}/.env"
TEMPLATE="${ROOT_DIR}/.env.example"
FORCE="${1:-}"

require() {
  command -v "$1" >/dev/null 2>&1 || { echo "error: '$1' is required" >&2; exit 1; }
}

require python3

if [[ -f "${ENV_FILE}" && "${FORCE}" != "--force" ]]; then
  echo "refusing to overwrite existing ${ENV_FILE} (use --force to replace it)"
  exit 0
fi

if [[ ! -f "${TEMPLATE}" ]]; then
  echo "error: ${TEMPLATE} not found" >&2
  exit 1
fi

TMP_FILE="$(mktemp)"
trap 'rm -f "${TMP_FILE}"' EXIT
install -m 600 /dev/null "${TMP_FILE}"

# The generator runs in Python so the quoting rules are explicit and testable.
python3 - "${TEMPLATE}" "${TMP_FILE}" <<'PY'
"""Render .env from .env.example with fresh secrets for every secret field."""

from __future__ import annotations

import hashlib
import pathlib
import re
import secrets
import string
import sys

TEMPLATE, TARGET = (pathlib.Path(argument) for argument in sys.argv[1:3])

# Passwords are URL-safe by construction: they are embedded in DSNs in .env and in
# the compose environment, so characters that need percent-encoding are excluded
# (a database password must also be typeable into psql without escaping).
_ALPHABET = string.ascii_letters + string.digits


def token_hex() -> str:
    """64 hex characters — the minimum length the settings validator requires."""
    return secrets.token_hex(32)


def password() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(40))


secrets_map = {
    "JWT_SECRET": token_hex(),
    "JWT_REFRESH_SECRET": token_hex(),
    "POSTGRES_PASSWORD": password(),
    "NEXUS_API_PASSWORD": password(),
    "NEXUS_MIGRATOR_PASSWORD": password(),
    "REDIS_REQUIRED_PASSWORD": password(),
}

values = dict(secrets_map)
# Derived values: the DSNs carry the same credentials the containers will get.
values["DATABASE_URL"] = (
    "postgresql+asyncpg://nexus_api:"
    f"{secrets_map['NEXUS_API_PASSWORD']}@postgres:5432/nexus_exchange"
)
values["DATABASE_MIGRATION_URL"] = (
    "postgresql+asyncpg://nexus_migrator:"
    f"{secrets_map['NEXUS_MIGRATOR_PASSWORD']}@postgres:5432/nexus_exchange"
)
values["REDIS_URL"] = f"redis://:{secrets_map['REDIS_REQUIRED_PASSWORD']}@redis:6379/0"
values["CELERY_BROKER_URL"] = f"redis://:{secrets_map['REDIS_REQUIRED_PASSWORD']}@redis:6379/1"
values["CELERY_RESULT_BACKEND"] = f"redis://:{secrets_map['REDIS_REQUIRED_PASSWORD']}@redis:6379/2"

# Inline comments in the template are preserved: only the value in front of them
# is replaced.
lines: list[str] = []
applied: set[str] = set()
for line in TEMPLATE.read_text().splitlines():
    key = line.split("=", 1)[0].strip()
    if key in values and "=" in line and not line.lstrip().startswith("#"):
        line = f"{key}={values[key]}"
        applied.add(key)
    lines.append(line)

missing = sorted(set(values) - applied)
if missing:
    raise SystemExit(f"template is missing required keys: {', '.join(missing)}")

# A line like ``KEY=   # note`` is read by docker compose as the value "# note" for an
# empty key: the parser strips an inline comment only after a non-empty value. That
# turned the "leave the development admin password empty" placeholder into a password
# and satisfied the production backup-recipient check with a comment. Refuse to render
# such a file instead of writing one that means something different in a container.
ambiguous = [
    (number, line)
    for number, line in enumerate(lines, start=1)
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=\s*#", line)
]
if ambiguous:
    rendered = "; ".join(f"line {number}: {line.strip()!r}" for number, line in ambiguous)
    raise SystemExit(f"a trailing comment on an empty value is read as the value: {rendered}")

TARGET.write_text("\n".join(lines) + "\n")
TARGET.chmod(0o600)

for key in sorted(secrets_map):
    fingerprint = hashlib.sha256(secrets_map[key].encode()).hexdigest()[:16]
    print(f"  {key:<24} sha256:{fingerprint}")
PY

install -m 600 "${TMP_FILE}" "${ENV_FILE}"

echo "wrote ${ENV_FILE} (mode 600)"
echo
echo "next: docker compose up -d --build --wait"
echo "      docker compose exec api alembic upgrade head"
echo "      docker compose exec api python -m seeds"
