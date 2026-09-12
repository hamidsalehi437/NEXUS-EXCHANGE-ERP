"""Idempotent seed data for NEXUS EXCHANGE ERP.

Modules are numbered so their dependencies are explicit:

==== =====================================================================
001  currencies (the base currency is configured, not hard-coded)
002  permissions, roles and the default role → permission matrix
003  chart of accounts (needs currencies)
004  development administrator (guarded: development only, needs 002)
==== =====================================================================
"""

from __future__ import annotations

__all__ = ["SEED_MODULES"]

SEED_MODULES = (
    "seeds.001_currencies",
    "seeds.002_roles_permissions",
    "seeds.003_chart_of_accounts",
    "seeds.004_dev_admin",
)
