# Per-printer permission scoping

A user group can be limited to some printers. Members of a limited group only
see and act on those printers; every other printer is hidden from them, in
every part of the engine, as if it did not exist.

## Setting it up

Settings > Users > edit a group > **Printer access**:

- **All printers** (the default): the group is not limited.
- **Only selected printers and locations**: pick printers, locations, or both.
  Members see the union: every picked printer, plus every printer whose
  location is one of the picked locations. Location matching ignores case and
  surrounding spaces, and follows the printer: move a printer to a picked
  location and it appears for the group, move it away and it disappears.

The API is the group endpoints:

```
POST  /api/v1/groups/        {"name": "Bay 1 crew", "permissions": [...],
                              "scope_printer_ids": [3, 7], "scope_locations": ["Bay 1"]}
PATCH /api/v1/groups/{id}    {"scope_printer_ids": [3], "scope_locations": null}
PATCH /api/v1/groups/{id}    {"scope_printer_ids": null, "scope_locations": null}   # back to all printers
```

On `PATCH`, a scope field that is sent (even as `null`) replaces the stored
value and an omitted field is left as it is. Unknown printer ids are refused
with 400. `GET /api/v1/groups` and `GET /api/v1/groups/{id}` return both fields.

## Rules

| Situation | Printers the user sees |
|---|---|
| Group with both fields `null` | every printer (all groups created before this feature) |
| Group with a list (even empty) | the listed printers plus the printers in the listed locations |
| Limited group with nothing picked | none (fails closed, the editor warns) |
| User in several groups | the union of their scopes; any unlimited group gives every printer |
| Administrator (`role=admin` or the Administrators group) | every printer, always; the Administrators group cannot be limited |
| User in no group | every printer (they hold no permissions anyway) |
| API key with an owner | the owner's printers |
| Legacy API key with no owner | every printer, as before |
| Authentication disabled | every printer (there are no users to limit) |

Scoping only narrows which printers a user reaches. What they may do with a
printer they can see is still decided by the group's permissions.

## What "hidden" means

An out-of-scope printer answers **404 Printer not found**, never 403, so a
limited user cannot tell a hidden printer from one that does not exist. Rows
that belong to a hidden printer answer 404 with their own not-found message
(`Archive not found`, `Queue item not found`, `Smart plug not found`, ...).
When a hidden printer is named in a request body (queueing, smart plug,
notification provider) the answer is the same 400 a missing printer gets.

Covered:

- **Printers**: the list, the out-of-service list, developer-mode warnings,
  available filaments, and every `/printers/{printer_id}/...` route (detail,
  status, pause, resume, stop, G-code, light, fans, temperatures, AMS and
  slots, out of service, files, plate, nozzles, filament paths, K-profiles,
  overlay status, maintenance, alarms acknowledgement). Any route with
  a `printer_id` path or query parameter is checked before it runs.
- **Queue**: the list, item routes (`/queue/{id}`, cancel, stop, start, mark,
  preflight), create, update, bulk update, reorder, stats, `POST
  /queue/preflight` (candidate printers are limited to the scope) and library
  `add-to-queue`. A limited user sees queue rows on their printers plus the
  unassigned (model or group target) rows they created themselves. Their
  model and group jobs only ever go to printers they can see: the scheduler
  limits its candidates to the job creator's scope.
- **Cameras**: stream and snapshot tokens carry the scope of whoever minted
  them (the key owner for an API key), and so do long-lived camera, Cam Wall
  and overlay tokens (their owner's). The Cam Wall only lists in-scope
  printers.
- **Alarms and failure events**: feed, summary, single alarm, acknowledge.
- **History and stats**: archives (list, search, detail and every
  `/archives/{id}/...` route, compare, export, failure analysis), print log
  (list, entry routes, clearing only removes in-scope entries), archive
  stats, `/stats/farm`, queue stats, firmware update list, maintenance
  overview and summary. Archives and log entries that belong to no printer
  stay visible.
- **Smart plugs**: plugs tied to a hidden printer are hidden; plugs tied to
  no printer stay visible.
- **Notifications**: providers tied to a hidden printer and log rows about one
  are hidden. Browser push for an event about a printer skips browsers of
  users who cannot see it. Shared providers (Discord, ntfy, email, ...) are
  farm-wide channels set up by an administrator and are not filtered.
- **Live updates**: a websocket connection only receives events about its
  printers (anything carrying a `printer_id`, at the top level or in `data`).
  Its printer set is worked out before it joins, and recomputed when a
  group's scope or members change and when a printer is added or moves
  location.
- **Printer groups and spool assignments**: members outside the scope are not
  listed; editing a group keeps the hidden members.

Not filtered: public media URLs that already need no login (archive
thumbnails, the printer cover) and administrator-only areas (settings, users,
virtual printer setup).

## For integrations

Code that acts for a user outside an HTTP request uses
`backend.app.core.printer_scope`:

```python
from backend.app.core.printer_scope import visible_printer_ids_for_user, visible_printer_ids_for_user_id

ids = await visible_printer_ids_for_user(db, user)          # set[int] | None
ids = await visible_printer_ids_for_user_id(db, user_id)    # None = every printer
```

`None` means "every printer"; a set (possibly empty) is the allow-list. Inside
a route, depend on `get_printer_scope` and filter with `apply_printer_scope`
(printer queries), `column_in_scope` (tables keyed by `printer_id`) or
`ensure_printer_visible`.

Artisan Hub talks to the engine with an API key whose owner is unlimited, so
it keeps seeing the whole farm.

## Upgrading

The upgrade adds `groups.scope_printer_ids`, `groups.scope_locations` and
`auth_ephemeral_tokens.scope_user_id`, all empty, so every existing group stays
unlimited and nothing changes until an administrator limits a group.
