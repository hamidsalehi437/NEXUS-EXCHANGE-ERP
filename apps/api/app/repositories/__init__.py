"""Repository layer: the only place that builds queries (ARCHITECTURE §5, PART 46/47).

Repositories receive an :class:`~sqlalchemy.ext.asyncio.AsyncSession`, never open or
commit transactions, and contain no business rules — a repository answers "what does
the database say", a service decides what to do about it. That split is what makes
the invariants in this phase testable: the RBAC rules live in the service layer where
they can be read, while every SQL statement exists in exactly one place.
"""
