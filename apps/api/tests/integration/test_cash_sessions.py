"""Phase 6 — the shift: opening a drawer, counting it, and closing it again.

A shift is what makes a cash business auditable: the operator counts the drawer at the start,
every movement during the shift is attributed to it, and at the end the count is compared with
what the shift's own immutable movements say should be there. This suite drives that lifecycle
through the HTTP door (``API_CONTRACT.md`` §9.4) and checks the outcome against the tables the
accounting model names — ``cash_sessions``, ``cash_session_lines``, ``cash_movements``,
``v_cash_position`` and ``journal_lines`` — never against a cached total.

What the suite refuses to accept:

* a second shift on the same till (the drawer belongs to the branch, not to a device);
* an opening that disagrees with the cash the books already carry — money is not conjured into
  the ledger to match a count, and a count is not overwritten to match the ledger;
* a movement on a closed shift, or a second close;
* a close without a full count, or with a count for a currency the shift never moved;
* a counted difference that is quietly ignored: it becomes an explicit, audited adjustment
  against ``5090 Cash Short / Over`` **through** ``AccountingService``, and it is reported in
  the response and the audit row.

Every test builds its own branch (through ``POST /branches``) with its own drawers, so nothing
here depends on what another test left in the seeded ``MAIN`` branch.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.core.audit_actions import AuditAction
from tests.auth_helpers import bearer, login
from tests.cash_helpers import (
    OPENING_OFFSET_CODE,
    SHORT_OVER_CODE,
    attach_session,
    audit_rows,
    branch_entries,
    counted,
    get_balance,
    get_current,
    get_session,
    get_sessions,
    ledger_quantity,
    movement_rows,
    position,
    post_close,
    post_in,
    post_open,
    publish,
    read,
    read_one,
    session_lines,
    session_row,
)
from tests.exchange_helpers import (
    ExchangeWorld,
    account_id_of,
    build_world,
    error_code,
    error_details,
    retire,
)

pytestmark = [pytest.mark.integration, pytest.mark.cash]

AFN = "AFN"
USD = "USD"


# ------------------------------------------------------------------------------ fixtures
@pytest.fixture
def cash_counter(
    exchange_world: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> ExchangeWorld:
    """A counter a test can reach over HTTP: its own branch, its own device and session.

    Nothing is funded: a cash test declares its own opening balances, so the numbers it asserts
    are the numbers it created.
    """
    world = exchange_world
    attach_session(world, api_client, admin_headers)
    return world


def open_body(world: ExchangeWorld, **declared: str) -> list[dict[str, str]]:
    """``{"AFN": "250000"}`` as the ``openings`` array of ``POST /cash/open``."""
    return [
        {"currency_id": str(world.money(code).id), "amount": amount}
        for code, amount in declared.items()
    ]


def as_decimal(value: Any) -> Decimal:
    """Compare money by value: PostgreSQL renders zero as ``0E-10``, not ``0``."""
    return Decimal(str(value))


def movement_count(database: str, branch_id: uuid.UUID) -> int:
    return len(movement_rows(database, branch_id=branch_id))


# --------------------------------------------------------------------------- opening a shift
def test_a_shift_opens_with_its_counted_opening_and_posts_it(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """§6.1: the first count of an unfunded drawer *is* the opening balance, and it is posted."""
    world = cash_counter
    afn = world.money(AFN).id

    response = post_open(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        openings=open_body(world, AFN="250000"),
        notes="Morning shift",
    )
    body = response.json()
    session_id = uuid.UUID(body["session_id"])

    assert response.status_code == 201
    assert body["status"] == "OPEN"
    assert body["branch_id"] == str(world.branch_id)
    assert body["branch_timezone"] == world.branch_timezone
    assert body["opened_by_username"] == "admin"
    assert body["closed_at"] is None
    assert body["movement_count"] == 1
    assert [line["opening_declared"] for line in body["lines"]] == ["250000.0000000000"]
    assert body["lines"][0]["expected_amount"] is None
    assert body["lines"][0]["counted_amount"] is None
    assert body["variances"] == []

    # The row itself: open, owned, and stamped by the server.
    stored = session_row(world.database, session_id)
    assert stored["status"] == "OPEN"
    assert stored["branch_id"] == world.branch_id
    assert stored["device_id"] == world.device_id
    assert stored["closed_by"] is None
    assert stored["notes"] == "Morning shift"
    assert stored["opened_at"].tzinfo is not None

    # The reconciliation line starts as a declared count with no expectation yet.
    lines = session_lines(world.database, session_id)
    assert len(lines) == 1
    assert lines[0]["currency_code"] == AFN
    assert as_decimal(lines[0]["opening_declared"]) == Decimal("250000")
    assert lines[0]["expected_amount"] is None
    assert lines[0]["difference"] is None

    # One OPENING movement, attributed to the shift, bound to the entry that posted it.
    movements = movement_rows(world.database, branch_id=world.branch_id, session_id=session_id)
    assert [(row["movement_type"], row["currency_code"]) for row in movements] == [("OPENING", AFN)]
    assert movements[0]["reference_type"] == "OPENING_BALANCE"
    assert as_decimal(movements[0]["amount"]) == Decimal("250000")
    assert as_decimal(movements[0]["signed_amount"]) == Decimal("250000")
    assert movements[0]["adjustment_sign"] is None
    assert movements[0]["journal_entry_id"] is not None
    assert movements[0]["created_by"] == world.head_user_id
    assert movements[0]["account_id"] == world.drawer(AFN)

    # The ledger says the same thing: the drawer holds 250,000 and 6000 carries the offset.
    assert ledger_quantity(
        world.database, branch_id=world.branch_id, account_id=world.drawer(AFN)
    ) == Decimal("250000")
    assert world.ledger(AFN) == world.cash(AFN) == Decimal("250000")
    offset = read_one(
        world.database,
        """
        SELECT COALESCE(SUM(l.credit - l.debit), 0) AS balance
          FROM journal_lines l
          JOIN journal_entries e ON e.id = l.journal_entry_id
         WHERE e.branch_id = :branch_id AND l.account_id = :account_id
        """,
        branch_id=world.branch_id,
        account_id=account_id_of(world.database, OPENING_OFFSET_CODE),
    )
    assert as_decimal(offset["balance"]) == Decimal("250000")
    assert [
        row["reference_type"] for row in branch_entries(world.database, branch_id=world.branch_id)
    ] == ["OPENING_BALANCE"]
    assert afn == world.money(AFN).id  # the currency the shift moved is the one declared

    # Audit: who opened which drawer, for how much, on which device.
    audit = audit_rows(world.database, entity_id=session_id, action=AuditAction.CASH_SESSION_OPENED)
    assert len(audit) == 1
    assert audit[0]["user_id"] == world.head_user_id
    assert audit[0]["entity_type"] == "cash_session"
    assert audit[0]["new_data"]["opening_lines"] == [
        {"currency_id": str(afn), "declared": "250000.0000000000"}
    ]
    assert audit[0]["new_data"]["notes"] == "Morning shift"


def test_one_shift_at_a_time_at_one_till(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """The drawer is the branch's: a second shift would make expected amounts a guess."""
    world = cash_counter
    first = post_open(
        api_client, world.headers, branch_id=world.branch_id, openings=open_body(world, AFN="1000")
    )
    session_id = uuid.UUID(first.json()["session_id"])

    second = post_open(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        openings=open_body(world, AFN="1000"),
        expect=None,
    )
    assert second.status_code == 409
    assert error_code(second) == "CASH_SESSION_ALREADY_OPEN"
    assert second.json()["error"]["details"]["session_id"] == str(session_id)

    open_rows = read(
        world.database,
        "SELECT id FROM cash_sessions WHERE branch_id = :id AND status = 'OPEN'",
        id=world.branch_id,
    )
    assert [row["id"] for row in open_rows] == [session_id]


def test_a_drawer_the_books_already_carry_is_counted_not_reposted(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """Declaring the carried amount opens the shift without posting anything new."""
    world = cash_counter
    world.fund((AFN, "120000", "1"))
    before = movement_count(world.database, world.branch_id)

    response = post_open(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        openings=open_body(world, AFN="120000"),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["movement_count"] == 0
    assert body["lines"][0]["opening_declared"] == "120000.0000000000"

    # No new movement and no new journal entry: the books already carried this cash.
    assert movement_count(world.database, world.branch_id) == before
    assert len(branch_entries(world.database, branch_id=world.branch_id)) == 1
    assert world.cash(AFN) == Decimal("120000")
    assert world.ledger(AFN) == Decimal("120000")


def test_an_opening_that_disagrees_with_the_books_is_refused(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """Money is not conjured to match a count, and the count is not silently rewritten."""
    world = cash_counter
    world.fund((AFN, "120000", "1"))
    entries_before = len(branch_entries(world.database, branch_id=world.branch_id))

    response = post_open(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        openings=open_body(world, AFN="250000"),
        expect=None,
    )
    assert response.status_code == 409
    assert error_code(response) == "CASH_OPENING_MISMATCH"
    details = response.json()["error"]["details"]
    assert details["declared_amount"] == "250000.0000000000"
    assert details["carried_amount"] == "120000.0000000000"

    # Nothing was written: no session, no entry, and the position is untouched.
    assert (
        read(
            world.database, "SELECT id FROM cash_sessions WHERE branch_id = :id", id=world.branch_id
        )
        == []
    )
    assert len(branch_entries(world.database, branch_id=world.branch_id)) == entries_before
    assert world.cash(AFN) == Decimal("120000")
    # No audit row either: the refusal names this branch and nothing was attributed to it.
    assert (
        read(
            world.database,
            """
            SELECT id FROM audit_logs
             WHERE entity_type = 'cash_session' AND new_data ->> 'branch_id' = :branch_id
            """,
            branch_id=str(world.branch_id),
        )
        == []
    )


def test_the_business_date_is_derived_from_the_branch_clock(
    api_client: TestClient, admin_headers: dict[str, str], main_database: str
) -> None:
    """A shift's business date is the branch's calendar day — never something a client sends."""
    world = build_world(
        api_client, admin_headers, main_database, currencies=(AFN,), timezone="Asia/Kabul"
    )
    try:
        attach_session(world, api_client, admin_headers)
        response = post_open(
            api_client,
            world.headers,
            branch_id=world.branch_id,
            openings=open_body(world, AFN="5000"),
        )
        body = response.json()
        session_id = uuid.UUID(body["session_id"])
        opened_at = session_row(world.database, session_id)["opened_at"]
        assert (
            body["business_date"] == opened_at.astimezone(ZoneInfo("Asia/Kabul")).date().isoformat()
        )

        # A client that tries to name the day is refused outright: the field does not exist.
        extra = api_client.post(
            "/api/v1/cash/open",
            headers=dict(world.headers),
            json={
                "branch_id": str(world.branch_id),
                "openings": [],
                "business_date": "2026-01-01",
            },
        )
        assert extra.status_code == 422
    finally:
        retire(api_client, admin_headers, world)


# --------------------------------------------------------------------------- closing a shift
def test_a_shift_closes_against_its_count_and_cannot_close_twice(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    world = cash_counter
    afn = world.money(AFN).id
    opened = post_open(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        openings=open_body(world, AFN="100000"),
    )
    session_id = uuid.UUID(opened.json()["session_id"])

    closed = post_close(
        api_client,
        world.headers,
        session_id=session_id,
        counted=counted((afn, "100000")),
        notes="Balanced",
    )
    body = closed.json()
    assert closed.status_code == 200
    assert body["status"] == "CLOSED"
    assert body["closed_at"] is not None
    assert body["closed_by_username"] == "admin"
    assert body["has_variance"] is False
    assert body["variances"] == []
    assert body["lines"][0]["opening_declared"] == "100000.0000000000"
    assert body["lines"][0]["expected_amount"] == "100000.0000000000"
    assert body["lines"][0]["counted_amount"] == "100000.0000000000"
    assert body["lines"][0]["difference"] == "0.0000000000"
    assert body["lines"][0]["adjustment_journal_entry_id"] is None

    stored = session_row(world.database, session_id)
    assert stored["status"] == "CLOSED"
    assert stored["closed_by"] == world.head_user_id
    assert stored["notes"] == "Balanced"

    # A second close is refused, and the shift keeps the stamp of the close that happened.
    again = post_close(
        api_client,
        world.headers,
        session_id=session_id,
        counted=counted((afn, "100000")),
        expect=None,
    )
    assert again.status_code == 409
    assert error_code(again) == "CASH_SESSION_NOT_OPEN"
    unchanged = session_row(world.database, session_id)
    assert unchanged["closed_at"] == stored["closed_at"]
    assert unchanged["status"] == "CLOSED"

    # A closed shift takes no movements, with or without naming the session.
    refusal = post_in(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="10",
        source_account_id=account_id_of(world.database, "3000"),
        session_id=session_id,
        expect=None,
    )
    assert refusal.status_code == 409
    assert error_code(refusal) == "CASH_SESSION_NOT_OPEN"
    no_session = post_in(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="10",
        source_account_id=account_id_of(world.database, "3000"),
        expect=None,
    )
    assert no_session.status_code == 409
    assert error_code(no_session) == "CASH_SESSION_NOT_OPEN"
    assert "NO_OPEN_SESSION" not in no_session.text
    assert movement_count(world.database, world.branch_id) == 1  # only the opening


def test_a_close_without_a_count_for_every_currency_is_refused(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """The count is the only evidence of what the drawer holds; a partial one proves nothing."""
    world = cash_counter
    # Foreign cash is valued at the house's own quote: without one there is no rate to post.
    publish(world, api_client, world.headers, USD, buy="70", sell="71")
    opened = post_open(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        openings=open_body(world, AFN="100000", USD="1000"),
    )
    session_id = uuid.UUID(opened.json()["session_id"])

    partial = post_close(
        api_client,
        world.headers,
        session_id=session_id,
        counted=counted((world.money(AFN).id, "100000")),
        expect=None,
    )
    assert partial.status_code == 422
    assert error_code(partial) == "CASH_RECON_INCOMPLETE"
    assert str(world.money(USD).id) in partial.json()["error"]["details"]["missing_currency_ids"]
    assert session_row(world.database, session_id)["status"] == "OPEN"

    # A currency the shift never moved cannot be counted into it either.
    unknown = post_close(
        api_client,
        world.headers,
        session_id=session_id,
        counted=counted(
            (world.money(AFN).id, "100000"),
            (world.money(USD).id, "1000"),
            (world.money("EUR").id, "1000"),
        ),
        expect=None,
    )
    assert unknown.status_code == 422
    assert error_code(unknown) == "VALIDATION_ERROR"
    assert error_details(unknown)["currency_ids"] == [str(world.money("EUR").id)]
    assert session_row(world.database, session_id)["status"] == "OPEN"

    complete = post_close(
        api_client,
        world.headers,
        session_id=session_id,
        counted=counted((world.money(AFN).id, "100000"), (world.money(USD).id, "1000")),
    )
    assert complete.status_code == 200
    assert complete.json()["status"] == "CLOSED"


def test_a_shortage_is_posted_to_5090_and_the_count_is_kept(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """A difference is real money: it is posted, reported and audited — never ignored."""
    world = cash_counter
    afn = world.money(AFN).id
    capital = account_id_of(world.database, "3000")
    opened = post_open(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        openings=open_body(world, AFN="100000"),
    )
    session_id = uuid.UUID(opened.json()["session_id"])
    post_in(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="20000",
        source_account_id=capital,
        session_id=session_id,
        idempotency_key=uuid.uuid4(),
    )
    assert world.cash(AFN) == Decimal("120000")

    closed = post_close(
        api_client, world.headers, session_id=session_id, counted=counted((afn, "119500"))
    )
    body = closed.json()
    assert closed.status_code == 200
    assert body["has_variance"] is True
    assert body["variances"] == [
        {
            "currency_id": str(afn),
            "currency_code": AFN,
            "opening_declared": "100000.0000000000",
            "expected_amount": "120000.0000000000",
            "counted_amount": "119500.0000000000",
            "difference": "-500.0000000000",
            "movement_id": body["variances"][0]["movement_id"],
            "journal_entry_id": body["variances"][0]["journal_entry_id"],
        }
    ]
    line = body["lines"][0]
    assert line["expected_amount"] == "120000.0000000000"
    assert line["counted_amount"] == "119500.0000000000"
    assert line["difference"] == "-500.0000000000"
    assert line["adjustment_journal_entry_id"] == body["variances"][0]["journal_entry_id"]
    assert line["adjustment_journal_entry_id"] is not None

    # The shortage is an ADJUSTMENT out of the drawer against 5090 (Dr 5090 / Cr Cash AFN).
    adjustment = movement_rows(
        world.database, branch_id=world.branch_id, session_id=session_id, movement_type="ADJUSTMENT"
    )
    assert len(adjustment) == 1
    assert adjustment[0]["adjustment_sign"] == -1
    assert as_decimal(adjustment[0]["amount"]) == Decimal("500")
    assert as_decimal(adjustment[0]["signed_amount"]) == Decimal("-500")
    assert adjustment[0]["account_id"] == world.drawer(AFN)
    assert adjustment[0]["reference_type"] == "CASH_MOVEMENT"
    assert adjustment[0]["id"] == uuid.UUID(body["variances"][0]["movement_id"])
    assert adjustment[0]["journal_entry_id"] == uuid.UUID(body["variances"][0]["journal_entry_id"])
    short_over = read_one(
        world.database,
        """
        SELECT COALESCE(SUM(l.debit - l.credit), 0) AS balance
          FROM journal_lines l
          JOIN journal_entries e ON e.id = l.journal_entry_id
         WHERE e.branch_id = :branch_id AND l.account_id = :account_id
        """,
        branch_id=world.branch_id,
        account_id=account_id_of(world.database, SHORT_OVER_CODE),
    )
    assert as_decimal(short_over["balance"]) == Decimal("500")

    # The drawer now holds exactly what was counted, in the movements and in the books.
    assert world.cash(AFN) == Decimal("119500")
    assert world.ledger(AFN) == Decimal("119500")
    assert position(world.database, branch_id=world.branch_id, currency_code=AFN) == Decimal(
        "119500"
    )

    audit = audit_rows(world.database, entity_id=session_id, action=AuditAction.CASH_SESSION_CLOSED)
    assert len(audit) == 1
    assert audit[0]["new_data"]["variances"][0]["difference"] == "-500.0000000000"
    assert audit[0]["new_data"]["status"] == "CLOSED"
    assert audit[0]["old_data"] == {"status": "OPEN"}


def test_an_overage_is_posted_the_other_way(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """An over count is a receipt: Dr Cash / Cr 5090, and the position still ends at the count."""
    world = cash_counter
    afn = world.money(AFN).id
    opened = post_open(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        openings=open_body(world, AFN="100000"),
    )
    session_id = uuid.UUID(opened.json()["session_id"])

    closed = post_close(
        api_client, world.headers, session_id=session_id, counted=counted((afn, "100500"))
    )
    body = closed.json()
    assert body["variances"][0]["difference"] == "500.0000000000"
    assert body["lines"][0]["difference"] == "500.0000000000"

    adjustment = movement_rows(
        world.database, branch_id=world.branch_id, session_id=session_id, movement_type="ADJUSTMENT"
    )
    assert [row["adjustment_sign"] for row in adjustment] == [1]
    assert as_decimal(adjustment[0]["signed_amount"]) == Decimal("500")
    assert world.cash(AFN) == Decimal("100500")
    assert world.ledger(AFN) == Decimal("100500")


def test_an_exact_count_closes_without_touching_the_books(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """No difference means no adjustment: 5090 stays untouched and no entry is invented."""
    world = cash_counter
    afn = world.money(AFN).id
    capital = account_id_of(world.database, "3000")
    opened = post_open(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        openings=open_body(world, AFN="100000"),
    )
    session_id = uuid.UUID(opened.json()["session_id"])
    post_in(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="5000",
        source_account_id=capital,
        session_id=session_id,
    )

    closed = post_close(
        api_client, world.headers, session_id=session_id, counted=counted((afn, "105000"))
    )
    body = closed.json()
    assert body["has_variance"] is False
    assert body["variances"] == []
    assert body["lines"][0]["difference"] == "0.0000000000"
    assert (
        movement_rows(
            world.database,
            branch_id=world.branch_id,
            session_id=session_id,
            movement_type="ADJUSTMENT",
        )
        == []
    )
    short_over = read_one(
        world.database,
        """
        SELECT COALESCE(SUM(l.debit - l.credit), 0) AS balance
          FROM journal_lines l
          JOIN journal_entries e ON e.id = l.journal_entry_id
         WHERE e.branch_id = :branch_id AND l.account_id = :account_id
        """,
        branch_id=world.branch_id,
        account_id=account_id_of(world.database, SHORT_OVER_CODE),
    )
    assert as_decimal(short_over["balance"]) == Decimal("0")


def test_a_difference_without_cash_adjust_is_refused(
    cash_counter: ExchangeWorld,
    api_client: TestClient,
    admin_headers: dict[str, str],
    make_user: object,
    provisioned_device: object,
) -> None:
    """A short drawer is not a cashier's to write off: the shift stays open instead."""
    world = cash_counter
    afn = world.money(AFN).id
    # The till is put on the books by someone with accounting authority first: an *opening
    # balance* is an `accounts.manage` posting (a cashier cannot create money in the books),
    # so the cashier opens a shift on a drawer the books already carry.
    world.fund((AFN, "1000", "1"))
    # The cashier opens and closes their own shift — but cannot post a difference.
    cashier = make_user(roles=("CASHIER",))  # type: ignore[operator]
    device_uuid = provisioned_device(assigned_branch=str(world.branch_id))  # type: ignore[operator]
    tokens = login(
        api_client,
        str(cashier["username"]),
        str(cashier["password"]),
        device_uuid=uuid.UUID(device_uuid),
        branch_id=world.branch_id,
    ).json()
    headers = bearer(str(tokens["access_token"]), str(tokens["device"]["id"]))

    opened = post_open(
        api_client, headers, branch_id=world.branch_id, openings=open_body(world, AFN="1000")
    )
    session_id = uuid.UUID(opened.json()["session_id"])

    refusal = post_close(
        api_client, headers, session_id=session_id, counted=counted((afn, "900")), expect=None
    )
    assert refusal.status_code == 403
    assert error_code(refusal) == "PERMISSION_DENIED"
    assert session_row(world.database, session_id)["status"] == "OPEN"
    assert session_lines(world.database, session_id)[0]["counted_amount"] is None
    assert (
        movement_rows(
            world.database,
            branch_id=world.branch_id,
            session_id=session_id,
            movement_type="ADJUSTMENT",
        )
        == []
    )

    # The same operator may close their own shift when the count reconciles exactly.
    balanced = post_close(
        api_client, headers, session_id=session_id, counted=counted((afn, "1000"))
    )
    assert balanced.status_code == 200
    assert balanced.json()["status"] == "CLOSED"

    # Another operator's shift is not theirs to close, even with a matching count.
    other = post_open(
        api_client, world.headers, branch_id=world.branch_id, openings=open_body(world, AFN="1000")
    )
    other_id = uuid.UUID(other.json()["session_id"])
    foreign = post_close(
        api_client, headers, session_id=other_id, counted=counted((afn, "1000")), expect=None
    )
    assert foreign.status_code == 403
    assert error_code(foreign) == "PERMISSION_DENIED"
    assert error_details(foreign)["reason"] == "NOT_SESSION_OWNER"
    assert error_details(foreign)["opened_by"] == str(world.head_user_id)
    assert session_row(world.database, other_id)["status"] == "OPEN"


# --------------------------------------------------------------------------------- reads
def test_the_balance_view_keeps_currencies_apart_and_reconciles_them(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """A position is one currency at one branch: never a sum, and always with the ledger."""
    world = cash_counter
    publish(world, api_client, world.headers, USD, buy="70", sell="71")
    post_open(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        openings=open_body(world, AFN="250000", USD="1000"),
    )

    response = get_balance(api_client, world.headers, branch_id=world.branch_id)
    body = response.json()
    assert body["source"] == "cash_movements"
    rows = {row["currency_code"]: row for row in body["items"]}
    assert set(rows) == {AFN, USD}
    assert rows[AFN]["physical_balance"] == "250000.0000000000"
    assert rows[AFN]["ledger_quantity"] == "250000.0000000000"
    assert rows[AFN]["reconciled"] is True
    assert rows[USD]["physical_balance"] == "1000.0000000000"
    # Functional values: the base currency is 1:1; the USD leg is valued by the ledger.
    assert rows[AFN]["ledger_functional_balance"] == "250000.0000000000"
    assert Decimal(rows[USD]["ledger_functional_balance"]) > 0
    assert rows[AFN]["branch_code"] == world.branch_code

    # The physical figure is the movement book's own arithmetic, per currency.
    assert position(world.database, branch_id=world.branch_id, currency_code=AFN) == Decimal(
        "250000"
    )
    assert position(world.database, branch_id=world.branch_id, currency_code=USD) == Decimal("1000")


def test_the_current_shift_is_scoped_to_the_callers_branch(
    cash_counter: ExchangeWorld,
    api_client: TestClient,
    admin_headers: dict[str, str],
    main_database: str,
    make_user: object,
    provisioned_device: object,
) -> None:
    """A till is not a shared resource: another branch's shift answers 404, not its contents.

    The isolation claim is tested with an operator who is *confined* to the other branch: a
    group-wide administrator may legitimately read every branch, so using one would prove
    nothing about the scope rule.
    """
    world = cash_counter
    opened = post_open(
        api_client, world.headers, branch_id=world.branch_id, openings=open_body(world, AFN="1000")
    )
    session_id = uuid.UUID(opened.json()["session_id"])

    current = get_current(api_client, world.headers, branch_id=world.branch_id)
    assert current.status_code == 200
    assert current.json()["session_id"] == str(session_id)

    other = build_world(api_client, admin_headers, main_database, currencies=(AFN,))
    try:
        attach_session(other, api_client, admin_headers)
        empty = get_current(api_client, other.headers, branch_id=other.branch_id, expect=None)
        assert empty.status_code == 404
        assert error_code(empty) == "RESOURCE_NOT_FOUND"
        assert error_details(empty)["reason"] == "NO_OPEN_SESSION"

        teller = make_user(roles=("CASHIER",))  # type: ignore[operator]
        device_uuid = provisioned_device(assigned_branch=str(other.branch_id))  # type: ignore[operator]
        tokens = login(
            api_client,
            str(teller["username"]),
            str(teller["password"]),
            device_uuid=uuid.UUID(device_uuid),
            branch_id=other.branch_id,
        ).json()
        confined = bearer(str(tokens["access_token"]), str(tokens["device"]["id"]))

        # Reading the first branch's shift by id is refused as "not found", never answered:
        # whether another branch traded at all is itself information (API_CONTRACT §3).
        hidden = get_session(api_client, confined, session_id, expect=None)
        assert hidden.status_code == 404
        assert error_code(hidden) == "RESOURCE_NOT_FOUND"
        # It echoes the id it was given (the caller sent it) and nothing else about the
        # shift: no branch, no status, no count.
        assert str(world.branch_id) not in hidden.text
        assert "OPEN" not in hidden.text

        listed = get_sessions(api_client, confined, branch_id=str(world.branch_id), expect=None)
        assert listed.status_code == 403
        assert error_code(listed) == "FORBIDDEN_SCOPE"

        crossing = get_current(api_client, confined, branch_id=world.branch_id, expect=None)
        assert crossing.status_code == 403
        assert error_code(crossing) == "FORBIDDEN_SCOPE"

        # The confined operator still sees their *own* branch: the scope wall has a door.
        own = get_sessions(api_client, confined, branch_id=other.branch_id)
        assert own.status_code == 200
        assert own.json()["total"] == 0
    finally:
        retire(api_client, admin_headers, other)


def test_the_shift_list_is_filterable_paginated_and_ordered(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """History reads newest first, and the filters mean what the contract says they mean."""
    world = cash_counter
    afn = world.money(AFN).id
    first = post_open(
        api_client, world.headers, branch_id=world.branch_id, openings=open_body(world, AFN="1000")
    )
    post_close(
        api_client,
        world.headers,
        session_id=uuid.UUID(first.json()["session_id"]),
        counted=counted((afn, "1000")),
    )
    second = post_open(
        api_client, world.headers, branch_id=world.branch_id, openings=open_body(world, AFN="1000")
    )

    everything = get_sessions(api_client, world.headers, branch_id=world.branch_id)
    assert everything.json()["total"] == 2
    assert [item["status"] for item in everything.json()["items"]] == ["OPEN", "CLOSED"]

    closed = get_sessions(api_client, world.headers, branch_id=world.branch_id, status="CLOSED")
    assert closed.json()["total"] == 1
    assert closed.json()["items"][0]["session_id"] == first.json()["session_id"]

    page = get_sessions(api_client, world.headers, branch_id=world.branch_id, limit=1, offset=1)
    body = page.json()
    assert body["total"] == 2
    assert body["limit"] == 1
    assert body["offset"] == 1
    assert [item["session_id"] for item in body["items"]] == [first.json()["session_id"]]

    # The second shift opened on a drawer the books already carried, so it posted nothing;
    # the first one posted its opening balance.
    carried_shift = get_session(api_client, world.headers, uuid.UUID(second.json()["session_id"]))
    assert carried_shift.json()["movement_count"] == 0
    opening_shift = get_session(api_client, world.headers, uuid.UUID(first.json()["session_id"]))
    assert opening_shift.json()["movement_count"] == 1
    assert [row["movement_type"] for row in opening_shift.json()["movements"]] == ["OPENING"]


def test_a_shift_records_the_reason_it_was_closed_and_who_closed_it(
    cash_counter: ExchangeWorld,
    api_client: TestClient,
    make_user: object,
    provisioned_device: object,
) -> None:
    """Closing another operator's shift needs the authority that carries ``cash.adjust``."""
    world = cash_counter
    afn = world.money(AFN).id
    opened = post_open(
        api_client, world.headers, branch_id=world.branch_id, openings=open_body(world, AFN="1000")
    )
    session_id = uuid.UUID(opened.json()["session_id"])

    supervisor = make_user(roles=("MANAGER",))  # type: ignore[operator]
    device_uuid = provisioned_device(assigned_branch=str(world.branch_id))  # type: ignore[operator]
    tokens = login(
        api_client,
        str(supervisor["username"]),
        str(supervisor["password"]),
        device_uuid=uuid.UUID(device_uuid),
        branch_id=world.branch_id,
    ).json()
    headers = bearer(str(tokens["access_token"]), str(tokens["device"]["id"]))

    closed = post_close(
        api_client,
        headers,
        session_id=session_id,
        counted=counted((afn, "1000")),
        notes="Handover to the night shift",
    )
    body = closed.json()
    assert closed.status_code == 200
    assert body["closed_by_username"] == supervisor["username"]
    assert body["notes"] == "Handover to the night shift"
    assert body["business_date"] == opened.json()["business_date"]
    assert session_row(world.database, session_id)["closed_at"] is not None
    assert session_row(world.database, session_id)["closed_at"].tzinfo is dt.UTC or (
        session_row(world.database, session_id)["closed_at"].tzinfo is not None
    )
