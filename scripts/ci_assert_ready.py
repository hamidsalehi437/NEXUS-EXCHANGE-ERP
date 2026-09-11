#!/usr/bin/env python3
"""Assert the readiness payload that nginx serves for the compose acceptance job.

Run by ``.github/workflows/ci.yml`` (compose job) through
``scripts/ci_exec_report.sh``, so a failure becomes a check annotation carrying the
payload: ``http://127.0.0.1:8080/api/v1/health/ready``.

The payload is the contract of the containerised stack (PART 44): the API reports
itself ready, the migrated schema revision is the expected one, and every
component it depends on answers. A silently degraded stack must fail the
acceptance job, not pass it.

Usage:
    scripts/ci_assert_ready.py /tmp/ready.json
"""

from __future__ import annotations

import json
import pathlib
import sys

EXPECTED_REVISION = "0001_initial_schema"
EXPECTED_COMPONENTS = {"postgresql": "ok", "redis": "ok"}


def _fail(message: str) -> None:
    raise SystemExit(message)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        _fail("usage: ci_assert_ready.py <readiness-payload.json>")

    path = pathlib.Path(argv[1])
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        _fail(f"the readiness endpoint did not answer (no payload at {path}): {exc}")
    except json.JSONDecodeError as exc:
        _fail(f"the readiness endpoint returned a non-JSON payload: {exc}")

    # The payload is printed before any assertion: whoever reads the annotation sees
    # the evidence even when the assertion below is the thing that failed.
    print(json.dumps(payload, indent=2, sort_keys=True))

    if not isinstance(payload, dict):
        _fail(f"unexpected readiness payload type: {type(payload).__name__}")

    status = payload.get("status")
    if status != "ready":
        _fail(f"the stack is not ready: status={status!r}")

    revision = payload.get("schema_revision")
    if revision != EXPECTED_REVISION:
        _fail(f"unexpected schema revision: {revision!r} != {EXPECTED_REVISION!r}")

    components = {
        component.get("name"): component.get("status")
        for component in payload.get("components", [])
        if isinstance(component, dict)
    }
    if components != EXPECTED_COMPONENTS:
        _fail(f"component health mismatch: {components!r} != {EXPECTED_COMPONENTS!r}")

    print(f"readiness ok: revision={revision} components={sorted(components)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
