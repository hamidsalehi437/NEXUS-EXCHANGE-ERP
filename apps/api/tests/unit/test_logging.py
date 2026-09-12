"""Structured logging must never leak a credential (SECURITY.md §4).

Redaction is tested by value (a DSN password, a bearer token, an Argon2 hash) and by
key (a field named ``password``), because both routes exist and both must hold.
"""

from __future__ import annotations

import json

import pytest

from app.core.logging import (
    REDACTION_PLACEHOLDER,
    configure_logging,
    get_logger,
    request_id_var,
)

pytestmark = pytest.mark.unit

ARGON2_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$aGFzaHZhbHVlMTIzNDU2"
DSN_WITH_PASSWORD = "postgresql://nexus:sup3rsecret@db:5432/nexus_exchange"


@pytest.fixture(autouse=True)
def _json_logging() -> None:
    configure_logging(level="INFO", fmt="json")


def _emit(capsys: pytest.CaptureFixture[str], **event: object) -> dict[str, object]:
    logger = get_logger("tests.logging")
    logger.info("probe_event", **event)
    line = capsys.readouterr().out.strip().splitlines()[-1]
    return json.loads(line)


class TestRedaction:
    def test_dsn_password_is_redacted(self, capsys: pytest.CaptureFixture[str]) -> None:
        payload = _emit(capsys, dsn=DSN_WITH_PASSWORD)
        rendered = json.dumps(payload)
        assert "sup3rsecret" not in rendered
        assert REDACTION_PLACEHOLDER in rendered
        assert "nexus" in rendered  # the user name is not a secret; the host stays readable

    def test_bearer_token_is_redacted(self, capsys: pytest.CaptureFixture[str]) -> None:
        payload = _emit(capsys, message="Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc.def")
        rendered = json.dumps(payload)
        assert "eyJhbGciOiJIUzI1NiJ9.abc.def" not in rendered
        assert "Bearer [redacted]" in rendered

    def test_argon2_hash_is_redacted(self, capsys: pytest.CaptureFixture[str]) -> None:
        payload = _emit(capsys, stored=ARGON2_HASH)
        rendered = json.dumps(payload)
        assert "aGFzaHZhbHVlMTIzNDU2" not in rendered
        assert REDACTION_PLACEHOLDER in rendered

    @pytest.mark.parametrize(
        "key", ["password", "new_password", "jwt_secret", "refresh_token", "api_key", "private_key"]
    )
    def test_secret_shaped_keys_are_redacted(
        self, capsys: pytest.CaptureFixture[str], key: str
    ) -> None:
        payload = _emit(capsys, **{key: "reveal-me-please"})
        assert payload[key] == REDACTION_PLACEHOLDER
        assert "reveal-me-please" not in json.dumps(payload)

    def test_ordinary_fields_are_not_mangled(self, capsys: pytest.CaptureFixture[str]) -> None:
        payload = _emit(capsys, currency="AFN", amount="70000.0000000000")
        assert payload["currency"] == "AFN"
        assert payload["amount"] == "70000.0000000000"


class TestEventShape:
    def test_event_name_and_level_survive(self, capsys: pytest.CaptureFixture[str]) -> None:
        payload = _emit(capsys, currency="USD")
        assert payload["event"] == "probe_event"
        assert payload["level"] == "info"

    def test_request_id_is_attached_when_set(self, capsys: pytest.CaptureFixture[str]) -> None:
        token = request_id_var.set("req-42")
        try:
            payload = _emit(capsys)
        finally:
            request_id_var.reset(token)
        assert payload["request_id"] == "req-42"

    def test_request_id_is_absent_when_unset(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert "request_id" not in _emit(capsys)

    def test_console_format_is_human_readable(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="INFO", fmt="console")
        get_logger("tests.logging").info("console_probe", currency="AFN")
        output = capsys.readouterr().out
        assert "console_probe" in output
        assert "AFN" in output

    def test_get_logger_exposes_the_standard_levels(self) -> None:
        logger = get_logger("tests.logging")
        for method in ("debug", "info", "warning", "error", "critical"):
            assert callable(getattr(logger, method))
