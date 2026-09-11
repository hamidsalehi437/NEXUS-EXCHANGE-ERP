"""Seed CLI: ``python -m seeds`` (PART 45, PART 58).

Behaviour:

* connects with the **migration** credentials (the schema owner), because the
  chart of accounts, roles and grants are reference data that the runtime role may
  not create;
* runs every seed module inside one transaction — a failure leaves the database
  exactly as it was;
* is idempotent: a second run reports ``unchanged`` for every row and writes no
  audit event;
* supports ``--check`` (report what would change, change nothing) for use in CI
  and pre-deployment gates;
* ``--only`` selects modules by numeric prefix for targeted reruns;
* never prints a secret, and never resets an existing password.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import create_sync_engine, create_sync_session_factory
from app.core.logging import configure_logging, get_logger
from seeds.base import SeedContext, SeedCounts, rows_summary

logger = get_logger("seeds")

SEED_MODULES: tuple[str, ...] = (
    "seeds.001_currencies",
    "seeds.002_roles_permissions",
    "seeds.003_chart_of_accounts",
    "seeds.004_dev_admin",
)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m seeds",
        description="Apply NEXUS idempotent seed data.",
    )
    parser.add_argument(
        "--only",
        default=None,
        metavar="PREFIXES",
        help="Comma-separated module prefixes to run, e.g. --only 001,003",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report what would change without writing anything (exit 1 if changes needed)",
    )
    return parser.parse_args(argv)


def _select_modules(only: str | None) -> list[str]:
    if not only:
        return list(SEED_MODULES)
    wanted = {part.strip() for part in only.split(",") if part.strip()}
    selected = [
        module for module in SEED_MODULES if module.rsplit(".", 1)[-1].split("_", 1)[0] in wanted
    ]
    unknown = wanted - {module.rsplit(".", 1)[-1].split("_", 1)[0] for module in selected}
    if unknown:
        raise SystemExit(f"unknown seed module prefix(es): {', '.join(sorted(unknown))}")
    return selected


_NO_MIGRATION_MESSAGE = "the database has no applied migration; run 'alembic upgrade head' first"


def _require_migrated_schema(session: Any) -> str:
    """Return the applied revision, refusing to seed a database with no schema.

    ``to_regclass`` is used first because querying a missing ``alembic_version``
    raises an undefined-table error: an operator who forgot to migrate should get
    one sentence telling them what to do, not a driver traceback.
    """
    if session.execute(text("SELECT to_regclass('public.alembic_version')")).scalar() is None:
        raise SystemExit(_NO_MIGRATION_MESSAGE)

    revision = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    if revision is None:
        raise SystemExit(_NO_MIGRATION_MESSAGE)
    return str(revision)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    configure_logging(level=settings.log_level, fmt=settings.log_format)

    engine = create_sync_engine(settings, purpose="seed")
    session_factory = create_sync_session_factory(engine)
    session = session_factory()

    total = SeedCounts()
    try:
        revision = _require_migrated_schema(session)
        logger.info(
            "seed_begin",
            environment=settings.app_env,
            schema_revision=revision,
            dry_run=args.check,
            modules=_select_modules(args.only),
        )

        context = SeedContext(session=session, settings=settings, dry_run=args.check)

        for module_name in _select_modules(args.only):
            module = importlib.import_module(module_name)
            before = SeedCounts(
                inserted=context.counts.inserted,
                updated=context.counts.updated,
                unchanged=context.counts.unchanged,
                removed=context.counts.removed,
            )
            module.run(context)
            delta = SeedCounts(
                inserted=context.counts.inserted - before.inserted,
                updated=context.counts.updated - before.updated,
                unchanged=context.counts.unchanged - before.unchanged,
                removed=context.counts.removed - before.removed,
            )
            logger.info("seed_module_applied", module=module_name, **delta.as_dict())
            print(rows_summary(delta, module_name))

        if args.check:
            session.rollback()
        else:
            session.commit()

        total = context.counts
        print(rows_summary(total, "TOTAL"))
        logger.info("seed_complete", dry_run=args.check, **total.as_dict())

        if args.check and total.changed:
            print("seed check: changes are required", file=sys.stderr)
            return 1
        return 0
    except Exception:
        session.rollback()
        logger.exception("seed_failed")
        raise
    finally:
        session.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
