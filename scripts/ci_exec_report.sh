#!/usr/bin/env bash
# =============================================================================
# NEXUS EXCHANGE ERP — run a command and report its failure with evidence
# =============================================================================
# Usage: scripts/ci_exec_report.sh "<title>" <command> [args...]
#
# Streams the command's output unchanged, then, when the command fails, emits a
# GitHub check annotation containing the last lines of that output.
#
# Why: a failed CI step is only actionable if its output can be read. Job logs are
# not always retrievable (the log endpoints can fail, and archived logs expire),
# while check-run annotations always travel with the check result and are readable
# through the API. A bare `docker compose exec ...` in a workflow step produces
# "exit code 1" and nothing else when the log is unavailable; this wrapper makes
# the reason part of the check result itself.
#
# The title must not contain a comma (it is a property separator in the workflow
# command syntax). Annotation messages are percent-encoded, so newlines survive.
# =============================================================================
set -uo pipefail

if [[ "$#" -lt 2 ]]; then
  echo "usage: $(basename "$0") <title> <command> [args...]" >&2
  exit 2
fi

title="$1"
shift

capture="$(mktemp)"
trap 'rm -f "${capture}"' EXIT

set +e
"$@" 2>&1 | tee "${capture}"
status="${PIPESTATUS[0]}"
set -e

if [[ "${status}" -ne 0 ]]; then
  # Percent-encode the three characters the workflow-command syntax reserves,
  # then keep the tail short enough to be read at a glance.
  tail_text="$(tail -n 25 "${capture}" | sed -e 's/%/%25/g' -e 's/\r//g' | tr '\n' '\036')"
  tail_text="${tail_text//$'\036'/%0A}"
  printf '::error title=%s::%s\n' "${title}" "${tail_text}"
fi

exit "${status}"
