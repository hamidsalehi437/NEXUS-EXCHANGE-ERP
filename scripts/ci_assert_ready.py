#!/usr/bin/env python3
"""Assert the readiness payload that nginx serves for the compose acceptance job.

Run by ``.github/workflows/ci.yml`` (compose job) through
``scripts/ci_exec_report.sh``, so a failure becomes a check annotation carrying the
payload: ``http://127.0.0.1:8080/api/v1/health/ready``.

The payload is the contract of the containerised stack (PART 44): the API reports
itself ready, the migrated schema revision is the expected one, and every
component it depends on answers. A silently degraded stack must fail the
acceptance job, not pass it.

The probe polls the endpoint itself instead of using ``curl``: when the request
never produces a payload, "no file" is all the annotation would otherwise say,
while the transport error (connection refused, HTTP status, timeout) is exactly
what identifies the broken hop. The payload of a ``503`` is still parsed and
reported, because a degraded stack returns its diagnosis in the body.

Usage:
    scripts/ci_assert_ready.py --url http://127.0.0.1:8080/api/v1/health/ready
    scripts/ci_assert_ready.py /tmp/ready.json
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

EXPECTED_REVISION = "0002_runtime_schema_revision"
EXPECTED_COMPONENTS = {"postgresql": "ok", "redis": "ok"}


def _fail(message: str) -> None:
    raise SystemExit(message)


def _assert_ready(payload: object) -> str:
    """Validate the readiness payload and return the revision that was reported."""
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

    return str(revision)


def _fetch(url: str, timeout: float) -> tuple[object | None, str | None]:
    """Fetch the readiness payload, returning ``(payload, transport_error)``.

    A ``503`` is not a transport error: the body carries the degradation report,
    which is the useful part of the evidence.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(body), None
        except json.JSONDecodeError:
            return None, f"HTTP {exc.code} with a non-JSON body: {body[:300]!r}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    except json.JSONDecodeError as exc:
        return None, f"HTTP 200 with a non-JSON body: {exc}"


def _probe(url: str, attempts: int, interval: float, timeout: float) -> object:
    last_error = "the endpoint was never tried"
    last_payload: object | None = None
    for attempt in range(1, attempts + 1):
        payload, error = _fetch(url, timeout)
        if payload is None:
            last_error = error or "unknown transport error"
        else:
            last_payload = payload
            if isinstance(payload, dict) and payload.get("status") == "ready":
                return payload
            print(f"attempt {attempt}: the stack is not ready yet")
        time.sleep(interval)
    if last_payload is not None:
        return last_payload
    _fail(
        f"{url} never produced a readiness payload after {attempts} attempts: {last_error}"
    )


def main(argv: list[str]) -> int:
    if "--url" in argv:
        index = argv.index("--url")
        url = argv[index + 1]
        attempts = (
            int(argv[argv.index("--attempts") + 1]) if "--attempts" in argv else 20
        )
        interval = (
            float(argv[argv.index("--interval") + 1]) if "--interval" in argv else 3.0
        )
        timeout = (
            float(argv[argv.index("--timeout") + 1]) if "--timeout" in argv else 5.0
        )
        payload = _probe(url, attempts, interval, timeout)
    elif len(argv) == 2:
        path = pathlib.Path(argv[1])
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            _fail(
                f"the readiness endpoint did not answer (no payload at {path}): {exc}"
            )
        except json.JSONDecodeError as exc:
            _fail(f"the readiness endpoint returned a non-JSON payload: {exc}")
    else:
        _fail("usage: ci_assert_ready.py <readiness-payload.json> | --url <endpoint>")

    revision = _assert_ready(payload)
    print(f"readiness ok: revision={revision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
