import asyncio
import json
import logging
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


def message_printer_id(message: dict[str, Any]) -> int | None:
    """The printer a broadcast is about: top-level ``printer_id``, else ``data.printer_id``."""
    pid = message.get("printer_id")
    if pid is None:
        data = message.get("data")
        if isinstance(data, dict):
            pid = data.get("printer_id")
    try:
        return int(pid) if pid is not None else None
    except (TypeError, ValueError):
        return None


def connection_may_receive(connection: WebSocket, message: dict[str, Any]) -> bool:
    """Per-printer scoping (#57): a scoped socket only hears about its printers.

    ``connection.state.printer_visible_ids`` is None for an unscoped socket and
    a set of printer ids for a scoped one (stamped at connect, refreshed by
    :meth:`ConnectionManager.refresh_printer_scopes`). Messages that name no
    printer go to everyone, as before.
    """
    visible = getattr(connection.state, "printer_visible_ids", None)
    if visible is None:
        return True
    pid = message_printer_id(message)
    return pid is None or pid in visible


class ConnectionManager:
    """Manages WebSocket connections and broadcasts."""

    def __init__(self):
        self.active_connections: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        """Accept a new WebSocket connection."""
        await websocket.accept()
        async with self._lock:
            self.active_connections.append(websocket)

    async def disconnect(self, websocket: WebSocket):
        """Remove a WebSocket connection."""
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)

    async def broadcast(self, message: dict[str, Any]):
        """Broadcast a message to all connected clients."""
        if not self.active_connections:
            return

        data = json.dumps(message)
        async with self._lock:
            disconnected = []
            for connection in self.active_connections:
                if not connection_may_receive(connection, message):
                    continue
                try:
                    await connection.send_text(data)
                except Exception:
                    disconnected.append(connection)

            # Clean up disconnected clients
            for conn in disconnected:
                if conn in self.active_connections:
                    self.active_connections.remove(conn)

    async def broadcast_to_user(self, user_id: int | None, message: dict[str, Any]):
        """Send a message to every connection authenticated as the given user.

        When ``user_id`` is None the message fans out to all connections —
        this is the auth-disabled single-user path, where neither the queue
        item's ``created_by_id`` nor the WS principal is set, and the
        existing fan-out semantics are exactly what the user wants.

        Per-user routing reads ``websocket.state.bambuddy_principal_user_id``
        stamped at connect time (``routes/websocket.py``). Connections
        without a stamped id are skipped on the targeted path so an
        anonymous reader never receives another user's dispatch toast.
        """
        if user_id is None:
            await self.broadcast(message)
            return

        if not self.active_connections:
            return

        data = json.dumps(message)
        async with self._lock:
            disconnected = []
            for connection in self.active_connections:
                conn_uid = getattr(connection.state, "bambuddy_principal_user_id", None)
                if conn_uid != user_id:
                    continue
                if not connection_may_receive(connection, message):
                    continue
                try:
                    await connection.send_text(data)
                except Exception:
                    disconnected.append(connection)

            for conn in disconnected:
                if conn in self.active_connections:
                    self.active_connections.remove(conn)

    async def refresh_printer_scopes(self) -> None:
        """Recompute every socket's visible printers after a scope-relevant change.

        Called when a group's scope or membership changes and when printers
        are added, removed or moved to another location. Sockets without a
        scope principal (auth disabled, ownerless key) are left alone.
        """
        from backend.app.core import database
        from backend.app.core.printer_scope import scope_for_user_id, visible_printer_ids

        async with self._lock:
            connections = list(self.active_connections)
        targets = [c for c in connections if getattr(c.state, "printer_scope_user_id", None) is not None]
        if not targets:
            return
        try:
            async with database.async_session() as db:
                cache: dict[int, set[int] | None] = {}
                for conn in targets:
                    uid = conn.state.printer_scope_user_id
                    if uid not in cache:
                        cache[uid] = await visible_printer_ids(db, await scope_for_user_id(db, uid))
                    conn.state.printer_visible_ids = cache[uid]
        except Exception:  # SEC-AUTH-EXC: a failed refresh must not widen access; scoped sockets see nothing
            logger.warning("Printer scope refresh failed; hiding all printers from scoped sockets", exc_info=True)
            for conn in targets:
                conn.state.printer_visible_ids = set()

    async def send_printer_status(self, printer_id: int, status: dict):
        """Send printer status update to all clients."""
        await self.broadcast(
            {
                "type": "printer_status",
                "printer_id": printer_id,
                "data": status,
            }
        )

    async def send_print_start(self, printer_id: int, data: dict):
        """Notify clients that a print has started."""
        await self.broadcast(
            {
                "type": "print_start",
                "printer_id": printer_id,
                "data": data,
            }
        )

    async def send_print_complete(self, printer_id: int, data: dict):
        """Notify clients that a print has completed."""
        await self.broadcast(
            {
                "type": "print_complete",
                "printer_id": printer_id,
                "data": data,
            }
        )

    async def send_archive_created(self, archive: dict):
        """Notify clients that a new archive was created."""
        await self.broadcast(
            {
                "type": "archive_created",
                "data": archive,
            }
        )

    async def send_archive_updated(self, archive: dict):
        """Notify clients that an archive was updated."""
        await self.broadcast(
            {
                "type": "archive_updated",
                "data": archive,
            }
        )

    async def send_queue_item_uploading(
        self,
        user_id: int | None,
        queue_item_id: int,
        printer_id: int,
        printer_name: str | None,
        file_name: str,
        total_bytes: int,
    ):
        """Toast trigger: scheduler picked the item up, FTP upload starts."""
        await self.broadcast_to_user(
            user_id,
            {
                "type": "queue_item_uploading",
                "queue_item_id": queue_item_id,
                "printer_id": printer_id,
                "printer_name": printer_name,
                "file_name": file_name,
                "total_bytes": total_bytes,
            },
        )

    async def send_queue_item_upload_progress(
        self,
        user_id: int | None,
        queue_item_id: int,
        bytes_transferred: int,
        total_bytes: int,
    ):
        """Toast update: throttled byte-level progress during the FTP upload."""
        pct = int(round(100 * bytes_transferred / total_bytes)) if total_bytes else 0
        await self.broadcast_to_user(
            user_id,
            {
                "type": "queue_item_upload_progress",
                "queue_item_id": queue_item_id,
                "bytes_transferred": bytes_transferred,
                "total_bytes": total_bytes,
                "pct": pct,
            },
        )

    async def send_queue_item_acked(
        self,
        user_id: int | None,
        queue_item_id: int,
        printer_id: int,
    ):
        """Toast trigger: watchdog confirmed the printer transitioned out of pre_state."""
        await self.broadcast_to_user(
            user_id,
            {
                "type": "queue_item_acked",
                "queue_item_id": queue_item_id,
                "printer_id": printer_id,
            },
        )

    async def send_queue_item_failed(
        self,
        user_id: int | None,
        queue_item_id: int,
        printer_id: int | None,
        reason: str,
    ):
        """Toast trigger: dispatch failed at any stage. Toast turns red, auto-dismisses."""
        await self.broadcast_to_user(
            user_id,
            {
                "type": "queue_item_failed",
                "queue_item_id": queue_item_id,
                "printer_id": printer_id,
                "reason": reason,
            },
        )

    async def send_missing_spool_assignment(
        self,
        printer_id: int,
        printer_name: str,
        missing_slots: list[dict[str, str]],
    ):
        """Notify clients that a print started with missing spool assignments."""
        await self.broadcast(
            {
                "type": "missing_spool_assignment",
                "printer_id": printer_id,
                "printer_name": printer_name,
                "missing_slots": missing_slots,
            }
        )


# Global connection manager
ws_manager = ConnectionManager()
