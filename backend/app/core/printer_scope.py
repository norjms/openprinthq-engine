"""Per-printer permission scoping.

A user group may be limited to a set of printers and/or printer locations. A
member of such a group only sees and acts on the printers inside that scope;
every other printer is hidden from them, everywhere, as if it did not exist
(routes answer 404, never 403, so the existence of a printer does not leak).

Rules (the single place they are decided):

* A group with neither ``scope_printer_ids`` nor ``scope_locations`` set
  (both NULL) is unscoped and grants the whole farm. Every group that existed
  before scoping was added is unscoped.
* A scoped group grants the union of its printer list and every printer whose
  ``location`` matches one of its locations (case-insensitive, trimmed). An
  empty list counts as "set": a scoped group with nothing in it grants no
  printers, so clearing the pickers fails closed rather than open.
* A user in several groups gets the union of their scopes, and any unscoped
  group grants full access (permissions are additive across groups, so is
  printer visibility).
* Administrators (legacy ``role="admin"`` or the Administrators group) always
  see every printer. A user in no group is unscoped (they hold no permissions
  anyway).
* An API key inherits its owner's scope. A legacy key without an owner keeps
  full access, as before.
* With authentication disabled there are no principals and no scoping.

Everything that lists or touches printers asks this module. The entry points:

``get_printer_scope(request)``
    The scope of the caller of an HTTP request (cached on ``request.state``).
    ``None`` means "every printer".
``visible_printer_ids(db, scope)``
    The set of printer ids a scope can see, or ``None`` for all.
``visible_printer_ids_for_user(db, user)`` / ``..._for_user_id``
    The same, for code that has a user rather than a request (the scheduler,
    notification fan-out, chat agents).
``ensure_printer_visible(db, scope, printer_id)``
    Raise 404 unless the printer is in scope.
``printer_scope_guard``
    Router dependency: resolves the scope once and 404s any request whose
    ``printer_id`` path or query parameter names a printer outside it.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request, status
from sqlalchemy import false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from backend.app.models.user import User

logger = logging.getLogger(__name__)

PRINTER_NOT_FOUND = "Printer not found"

# Request-state attribute names. Kept as constants so tests and other modules
# do not depend on spelling.
_STATE_RESOLVED = "printer_scope_resolved"
_STATE_SCOPE = "printer_scope"
_STATE_USER_ID = "printer_scope_user_id"
_STATE_KNOWN = "printer_scope_principal_known"
_STATE_DEFERRED = "printer_scope_deferred"


def normalize_location(value: str | None) -> str | None:
    """Canonical form used to compare printer locations."""
    if value is None:
        return None
    norm = value.strip().lower()
    return norm or None


@dataclass(frozen=True)
class PrinterScope:
    """The printers a scoped principal may see.

    ``None`` (not an instance of this class) is used everywhere for "no
    restriction"; an instance always restricts.
    """

    user_id: int | None
    printer_ids: frozenset[int] = field(default_factory=frozenset)
    locations: frozenset[str] = field(default_factory=frozenset)

    def allows(self, printer_id: int | None, location: str | None) -> bool:
        if printer_id is not None and printer_id in self.printer_ids:
            return True
        loc = normalize_location(location)
        return loc is not None and loc in self.locations


def group_is_scoped(group: Any) -> bool:
    return getattr(group, "scope_printer_ids", None) is not None or getattr(group, "scope_locations", None) is not None


def scope_for_user(user: User | None) -> PrinterScope | None:
    """Combine the scopes of every group the user belongs to.

    Pure function over the loaded ``user.groups``; see the module docstring
    for the rules.
    """
    if user is None or user.is_admin:
        return None
    groups = list(user.groups or [])
    if not groups:
        return None
    ids: set[int] = set()
    locs: set[str] = set()
    for group in groups:
        if not group_is_scoped(group):
            return None
        for pid in group.scope_printer_ids or []:
            try:
                ids.add(int(pid))
            except (TypeError, ValueError):
                continue
        for loc in group.scope_locations or []:
            norm = normalize_location(loc) if isinstance(loc, str) else None
            if norm:
                locs.add(norm)
    return PrinterScope(user_id=user.id, printer_ids=frozenset(ids), locations=frozenset(locs))


def printer_scope_clause(scope: PrinterScope | None):
    """SQL predicate on the ``printers`` table for this scope (None = no filter)."""
    from backend.app.models.printer import Printer

    if scope is None:
        return None
    parts = []
    if scope.printer_ids:
        parts.append(Printer.id.in_(sorted(scope.printer_ids)))
    if scope.locations:
        parts.append(func.lower(func.trim(Printer.location)).in_(sorted(scope.locations)))
    if not parts:
        return false()
    return or_(*parts)


def apply_printer_scope(query, scope: PrinterScope | None):
    """Add the scope predicate to a ``select(Printer ...)`` query."""
    clause = printer_scope_clause(scope)
    return query if clause is None else query.where(clause)


async def visible_printer_ids(db: AsyncSession, scope: PrinterScope | None) -> set[int] | None:
    """Printer ids the scope can see, or ``None`` when it can see every printer."""
    from backend.app.models.printer import Printer

    if scope is None:
        return None
    clause = printer_scope_clause(scope)
    rows = await db.execute(select(Printer.id).where(clause))
    return {r[0] for r in rows.all()}


async def is_printer_visible(db: AsyncSession, scope: PrinterScope | None, printer_id: int | None) -> bool:
    from backend.app.models.printer import Printer

    if scope is None:
        return True
    if printer_id is None:
        return False
    if printer_id in scope.printer_ids:
        return True
    if not scope.locations:
        return False
    loc = (await db.execute(select(Printer.location).where(Printer.id == printer_id))).scalar_one_or_none()
    return scope.allows(printer_id, loc)


async def ensure_printer_visible(db: AsyncSession, scope: PrinterScope | None, printer_id: int | None) -> None:
    """Raise 404 ``Printer not found`` unless ``printer_id`` is in scope."""
    if not await is_printer_visible(db, scope, printer_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=PRINTER_NOT_FOUND)


def column_in_scope(column, visible: set[int] | None, *, include_null: bool = False):
    """Predicate restricting a ``printer_id`` column to the visible set.

    ``visible=None`` means no restriction (returns None). ``include_null``
    keeps rows that are not tied to any printer (an uploaded archive, a smart
    plug with no printer).
    """
    if visible is None:
        return None
    clause = column.in_(sorted(visible)) if visible else false()
    if include_null:
        clause = or_(clause, column.is_(None))
    return clause


async def _load_user(db: AsyncSession, *, user_id: int | None = None, username: str | None = None) -> User | None:
    from sqlalchemy.orm import selectinload

    from backend.app.models.user import User

    q = select(User).options(selectinload(User.groups))
    if user_id is not None:
        q = q.where(User.id == user_id)
    elif username:
        q = q.where(User.username == username)
    else:
        return None
    return (await db.execute(q)).scalar_one_or_none()


async def scope_for_user_id(db: AsyncSession, user_id: int | None) -> PrinterScope | None:
    if user_id is None:
        return None
    user = await _load_user(db, user_id=user_id)
    if user is None:
        return None
    return scope_for_user(user)


async def visible_printer_ids_for_user(db: AsyncSession, user: User | None) -> set[int] | None:
    """Printer ids ``user`` may see, or ``None`` for every printer.

    The entry point for code that acts on behalf of a user outside an HTTP
    request (scheduler, notifications, chat agents). Pass ``None`` for the
    system / auth-disabled principal.
    """
    if user is None:
        return None
    reloaded = await _load_user(db, user_id=user.id)
    return await visible_printer_ids(db, scope_for_user(reloaded) if reloaded else None)


async def visible_printer_ids_for_user_id(db: AsyncSession, user_id: int | None) -> set[int] | None:
    """As :func:`visible_printer_ids_for_user`, by id (``None`` = unscoped)."""
    return await visible_printer_ids(db, await scope_for_user_id(db, user_id))


# --- API key owner cache ---------------------------------------------------
# Resolving an API key costs a pbkdf2 verify. The permission dependency on the
# route already pays that once; resolving the scope pays it again. The scope
# only needs "which user owns this key", so remember that for a short time,
# keyed by a hash of the presented secret. The cache never grants access: the
# route's own dependency still validates the key (and refuses a revoked one).
_APIKEY_OWNER_TTL = 60.0
_apikey_owner_cache: dict[str, tuple[float, int | None]] = {}


def clear_api_key_owner_cache() -> None:
    _apikey_owner_cache.clear()


async def _api_key_owner_id(db: AsyncSession, key_value: str) -> tuple[bool, int | None]:
    """Return (valid, owner_user_id) for an API key secret."""
    from backend.app.core import auth as _auth

    digest = hashlib.sha256(key_value.encode()).hexdigest()
    now = time.monotonic()
    hit = _apikey_owner_cache.get(digest)
    if hit is not None and hit[0] > now:
        return True, hit[1]
    api_key = await _auth._validate_api_key(db, key_value)
    if api_key is None:
        _apikey_owner_cache.pop(digest, None)
        return False, None
    if len(_apikey_owner_cache) > 1024:
        _apikey_owner_cache.clear()
    _apikey_owner_cache[digest] = (now + _APIKEY_OWNER_TTL, api_key.user_id)
    return True, api_key.user_id


async def _resolve_principal_user_id(db: AsyncSession, request: Request) -> tuple[bool, int | None]:
    """Work out who is calling, from the request headers.

    Returns ``(known, user_id)``. ``known`` is False when there are no usable
    credentials (the route's own auth dependency then decides; this module
    never raises 401 itself). ``user_id`` is None for a principal with no user
    (legacy ownerless API key).
    """
    import jwt
    from jwt.exceptions import PyJWTError as JWTError

    from backend.app.core import auth as _auth

    x_api_key = request.headers.get("x-api-key")
    if x_api_key:
        valid, owner = await _api_key_owner_id(db, x_api_key)
        if valid:
            return True, owner
    authz = request.headers.get("authorization") or ""
    scheme, _, token = authz.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return False, None
    if token.startswith("bb_"):
        valid, owner = await _api_key_owner_id(db, token)
        return (True, owner) if valid else (False, None)
    try:
        payload = jwt.decode(token, _auth.SECRET_KEY, algorithms=[_auth.ALGORITHM])
    except JWTError:
        return False, None
    username = payload.get("sub")
    if not username:
        return False, None
    user = await _load_user(db, username=username)
    if user is None:
        return False, None
    return True, user.id


async def resolve_request_scope(request: Request) -> PrinterScope | None:
    """Resolve and cache the scope of the principal behind ``request``."""
    state = request.state
    if getattr(state, _STATE_RESOLVED, False):
        return getattr(state, _STATE_SCOPE, None)

    from backend.app.core import auth as _auth

    scope: PrinterScope | None = None
    user_id: int | None = None
    # "known" is False when the request carries no usable header credentials:
    # either auth is disabled (no scoping at all) or the route authenticates
    # some other way (a ?token= query parameter), in which case the token
    # checker sets the scope later and runs any deferred checks.
    known = False
    async with _auth.async_session() as db:
        if await _auth.is_auth_enabled(db):
            known, user_id = await _resolve_principal_user_id(db, request)
            if known and user_id is not None:
                user = await _load_user(db, user_id=user_id)
                if user is None or not user.is_active:
                    # A key whose owner is gone or disabled: fail closed. The
                    # route's own auth dependency refuses such callers anyway.
                    scope = PrinterScope(user_id=user_id)
                else:
                    scope = scope_for_user(user)
        else:
            known = True
    setattr(state, _STATE_SCOPE, scope)
    setattr(state, _STATE_USER_ID, user_id)
    setattr(state, _STATE_KNOWN, known)
    setattr(state, _STATE_RESOLVED, True)
    return scope


def set_request_scope(request: Request, scope: PrinterScope | None, user_id: int | None) -> None:
    """Record a scope resolved by other means (camera and Cam Wall tokens)."""
    setattr(request.state, _STATE_SCOPE, scope)
    setattr(request.state, _STATE_USER_ID, user_id)
    setattr(request.state, _STATE_KNOWN, True)
    setattr(request.state, _STATE_RESOLVED, True)


async def run_deferred_scope_checks(request: Request) -> None:
    """Run resource checks that waited for a query-token principal."""
    checks = getattr(request.state, _STATE_DEFERRED, None) or []
    setattr(request.state, _STATE_DEFERRED, [])
    for check in checks:
        await check(request)


async def get_printer_scope(request: Request) -> PrinterScope | None:
    """FastAPI dependency: the caller's printer scope (None = every printer)."""
    return await resolve_request_scope(request)


async def get_scope_user_id(request: Request) -> int | None:
    """The user id behind the request (the owner for an API key), if any."""
    await resolve_request_scope(request)
    return getattr(request.state, _STATE_USER_ID, None)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def enforce_request_printer_ids(request: Request, scope: PrinterScope | None) -> None:
    """404 when a ``printer_id`` path or query parameter is outside ``scope``."""
    if scope is None:
        return
    candidates: list[int] = []
    pid = _int_or_none(request.path_params.get("printer_id"))
    if pid is not None:
        candidates.append(pid)
    for raw in request.query_params.getlist("printer_id"):
        qid = _int_or_none(raw)
        # Non-positive values are sentinels (-1 = "unassigned" on the queue).
        if qid is not None and qid > 0:
            candidates.append(qid)
    if not candidates:
        return
    from backend.app.core import auth as _auth

    async with _auth.async_session() as db:
        for candidate in candidates:
            await ensure_printer_visible(db, scope, candidate)


def printer_resource_guard(model: Any, param: str, *, column: str = "printer_id", not_found: str = "Not found"):
    """Router dependency for routes keyed by a printer-owned row.

    When the path has ``param`` (e.g. ``archive_id``), load that row's
    ``column`` and answer 404 ``not_found`` if it names a printer outside the
    caller's scope. Rows not tied to any printer (NULL) stay visible. A
    missing row is left to the route, which answers its own 404.
    """

    async def _check(request: Request) -> None:
        rid = _int_or_none(request.path_params.get(param))
        if rid is None:
            return
        scope = await resolve_request_scope(request)
        if scope is None:
            return
        from backend.app.core import auth as _auth

        async with _auth.async_session() as db:
            row = (await db.execute(select(getattr(model, column)).where(model.id == rid))).first()
            if row is None or row[0] is None:
                return
            if not await is_printer_visible(db, scope, row[0]):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=not_found)

    async def _guard(request: Request) -> None:
        if param not in request.path_params:
            return
        await resolve_request_scope(request)
        if not getattr(request.state, _STATE_KNOWN, True):
            deferred = getattr(request.state, _STATE_DEFERRED, None)
            if deferred is None:
                deferred = []
                setattr(request.state, _STATE_DEFERRED, deferred)
            deferred.append(_check)
            return
        await _check(request)

    return _guard


async def printer_scope_guard(request: Request) -> None:
    """Router-level dependency applied to every HTTP router.

    Resolves the caller's scope once per request and refuses any request that
    names an out-of-scope printer in a ``printer_id`` path or query parameter.
    Routes that take printer ids in a body, or that list printer-keyed rows,
    filter with the helpers above.
    """
    scope = await resolve_request_scope(request)
    await enforce_request_printer_ids(request, scope)
