"""Memory-scoping primitives for the artifact store.

Split out of ``artifact_store.py`` to keep the MEMORY-specific surface —
scope types, caller-identity object, scope authz, SQL clause builders,
and shape validation — in one place. ``artifact_store.py`` imports from
here; everything else (routes, MCP tools, tests) should import memory
symbols directly from this module as well.

Nothing here talks to the DB directly; these helpers just build
SQLAlchemy clauses or validate shapes. The DB I/O lives in
``artifact_store.py`` and consumes the clauses produced here.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import and_, or_
from sqlmodel import col

from ypl.db.agent_harness import SCOPED_INLINE_ARTIFACT_TYPES, AgentArtifact

# The three valid values of ``memory_scope`` on a MEMORY artifact row.
MemoryScope = Literal["user", "agent", "topic"]
VALID_MEMORY_SCOPES: frozenset[str] = frozenset({"user", "agent", "topic"})


@dataclass(frozen=True)
class MemoryCallerContext:
    """Caller identity used to filter MEMORY reads / authorize MEMORY writes.

    ``user_id`` is the ``users.user_id`` the caller is acting on behalf of
    (usually from the session's ``requesting_user_id`` / ``X-User-ID`` header).
    ``agent_name`` is the ``agents.name`` the caller is executing as (usually
    from the AHS runner's ``X-AHS-Agent-Name`` header, which is tamper-proof).

    Either may be ``None`` when the caller lacks that identity (e.g. an
    operator script only sets ``X-User-ID``). A caller with neither identity
    set has no MEMORY read access — the read filter returns no rows — which
    makes it impossible to leak cross-user/cross-agent data to an
    unauthenticated caller.
    """

    user_id: str | None = None
    agent_name: str | None = None

    @property
    def has_identity(self) -> bool:
        return self.user_id is not None or self.agent_name is not None


def memory_read_clause(caller: MemoryCallerContext) -> Any:
    """Return a SQLAlchemy clause selecting MEMORY rows readable by ``caller``.

    The resulting clause is ``topic`` ∪ (``user`` ∧ subject = caller.user_id) ∪
    (``agent`` ∧ subject = caller.agent_name). A caller with neither identity
    set matches no rows (conservative default; admin routes should bypass the
    filter instead).
    """
    # Wrap column references in ``col()`` so SQLAlchemy's / mypy's typing
    # treats the comparisons as ``ColumnElement[bool]`` rather than plain
    # ``bool`` — matches how the rest of this module uses ``col()``.
    scope_col = col(AgentArtifact.memory_scope)
    subject_col = col(AgentArtifact.memory_scope_subject)
    branches: list[Any] = [scope_col == "topic"]
    if caller.user_id is not None:
        branches.append(and_(scope_col == "user", subject_col == caller.user_id))
    if caller.agent_name is not None:
        branches.append(and_(scope_col == "agent", subject_col == caller.agent_name))
    return or_(*branches) if len(branches) > 1 else branches[0]


def caller_can_write_memory(
    caller: MemoryCallerContext,
    scope: str,
    subject: str | None,
) -> bool:
    """Return True if ``caller`` is allowed to write to (scope, subject).

    Rules (see design doc §'Access control'):

    - scope=topic     → any authenticated caller.
    - scope=user, X   → caller.user_id must equal X.
    - scope=agent, Y  → caller.agent_name must equal Y.
    """
    if scope == "topic":
        return True
    if scope == "user":
        return caller.user_id is not None and subject == caller.user_id
    if scope == "agent":
        return caller.agent_name is not None and subject == caller.agent_name
    return False


def validate_memory_scope_shape(scope: str, subject: str | None) -> None:
    """Raise :class:`ArtifactError` if (scope, subject) is not a legal pair.

    Mirrors the DB ``ck_agent_artifacts_memory_subject_presence`` check so we
    fail fast in the service layer before we ever hit Postgres.
    """
    # Lazy import: ``ArtifactError`` lives in ``artifact_store`` which imports
    # this module at load time. The lazy import breaks the cycle and only
    # runs when validation is actually invoked.
    from ypl.agent_harness_service.artifact_store import ArtifactError

    if scope not in VALID_MEMORY_SCOPES:
        raise ArtifactError(f"Invalid memory_scope {scope!r}; allowed: {sorted(VALID_MEMORY_SCOPES)}")
    if scope == "topic":
        if subject is not None:
            raise ArtifactError("memory_scope='topic' forbids memory_scope_subject")
    elif not subject:
        raise ArtifactError(f"memory_scope={scope!r} requires memory_scope_subject")


def apply_memory_filters(
    stmt: Any,
    *,
    memory_caller: MemoryCallerContext | None,
    memory_scope: str | None,
    memory_scope_subject: str | None,
) -> Any:
    """Apply scoped-inline visibility + scope/subject narrowing to ``stmt``.

    Affects MEMORY and SKILL rows (the scoped-inline artifact types). Other
    artifact types are preserved when ``memory_scope`` / ``subject`` are
    unset (they don't take part in the scope filter); narrowing to a specific
    scope or subject implicitly excludes them because their scope columns
    are NULL.
    """
    if memory_caller is not None:
        # Scoped-inline rows (MEMORY, SKILL) are constrained to the caller's
        # visibility (topic + own user + own agent). Other rows pass through.
        scoped_types = list(SCOPED_INLINE_ARTIFACT_TYPES)
        stmt = stmt.where(
            or_(
                col(AgentArtifact.artifact_type).notin_(scoped_types),
                memory_read_clause(memory_caller),
            )
        )
    if memory_scope is not None:
        stmt = stmt.where(col(AgentArtifact.memory_scope) == memory_scope)
    if memory_scope_subject is not None:
        stmt = stmt.where(col(AgentArtifact.memory_scope_subject) == memory_scope_subject)
    return stmt
