# SPDX-License-Identifier: AGPL-3.0-or-later
#
# OpenPrintHQ broker single-port engine shim.
#
# In the broker/rendezvous model (docs/broker-architecture.md) every printer a
# remote client fronts is reached through the client's ONE public gateway port.
# The engine, however, connects to printers with fixed protocol clients that
# begin TLS on the first byte (Bambu MQTT 8883, FTPS 990) - there is no place to
# inject the client's "OPHQ1 <token> <printerId> <targetPort>" routing line into
# those sockets. So this shim runs INSIDE the engine container and gives each
# routed printer a plain loopback address the engine can dial unchanged:
#
#     engine client  --TLS-->  127.0.0.1:<shimPort>  (this shim)
#                                      |
#                                      |  opens ONE tunnel to the client's single
#                                      |  gateway port, writes the OPHQ1 preamble,
#                                      v  then splices bytes verbatim
#                              client_host:client_port  --raw-->  printer:realPort
#
# The engine's TLS ClientHello and everything after it are forwarded byte-for-byte
# to the printer, so Bambu MQTT/FTPS stays end-to-end encrypted and cert-pinned;
# the shim only prepends a plaintext routing header the client consumes.
#
# ISOLATION: OpenPrintHQ is instance-per-user - one engine container per tenant.
# This shim runs in that container, binds its listeners to 127.0.0.1 ONLY (never
# 0.0.0.0, never the docker net), and derives its routing solely from this
# tenant's own printer rows. It can neither see nor reach another tenant.
#
# The control-plane writes the routing into each printer's endpoint_overrides:
#     endpoint_overrides["_broker"] = {
#         "client_host": "...", "client_port": 16384, "token": "...",
#         "printer_id": "3", "ports": { "<loopbackPort>": <realPrinterPort> } }
# The engine's own mqtt/ftp/moonraker_port consumers ignore "_broker".

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from backend.app.core.database import async_session
from backend.app.models.printer import Printer

log = logging.getLogger(__name__)

# How often to reconcile listeners against the DB (route changes, endpoint
# refreshes). Cheap: it just diffs a small dict of desired loopback ports.
_RECONCILE_INTERVAL_S = 15.0
_LOOPBACK = "127.0.0.1"
_PREAMBLE_GUARD_BYTES = 512  # never used here (we write, not read, the preamble)


class _Listener:
    """One loopback listener for a single (printer, realPort) mapping."""

    __slots__ = ("server", "spec")

    def __init__(self, server: asyncio.AbstractServer, spec: dict):
        self.server = server
        self.spec = spec  # {client_host, client_port, token, printer_id, target_port}


class BrokerShim:
    def __init__(self) -> None:
        # loopback_port -> _Listener
        self._listeners: dict[int, _Listener] = {}
        self._task: asyncio.Task | None = None
        self._stopping = False

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._run())
            log.info("broker shim started")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        for lp in list(self._listeners):
            await self._close_listener(lp)
        log.info("broker shim stopped")

    # ---- reconcile loop --------------------------------------------------
    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # never let the loop die
                log.warning("broker shim reconcile error: %s", e)
            try:
                await asyncio.sleep(_RECONCILE_INTERVAL_S)
            except asyncio.CancelledError:
                raise

    async def _desired(self) -> dict[int, dict]:
        """Build the desired {loopback_port -> spec} map from this tenant's
        printer rows. Only printers whose endpoint_overrides carry a valid
        `_broker` block contribute; everything else is left untouched."""
        desired: dict[int, dict] = {}
        async with async_session() as db:
            rows = (await db.execute(select(Printer))).scalars().all()
        for p in rows:
            ov = getattr(p, "endpoint_overrides", None)
            if not isinstance(ov, dict):
                continue
            br = ov.get("_broker")
            if not isinstance(br, dict):
                continue
            host = br.get("client_host")
            cport = br.get("client_port")
            token = br.get("token")
            ports = br.get("ports")
            pid = str(br.get("printer_id") or getattr(p, "id", ""))
            if not (host and cport and token and isinstance(ports, dict)):
                continue
            for loopback_str, real in ports.items():
                try:
                    loopback = int(loopback_str)
                    target_port = int(real)
                except (TypeError, ValueError):
                    continue
                desired[loopback] = {
                    "client_host": str(host),
                    "client_port": int(cport),
                    "token": str(token),
                    "printer_id": pid,
                    "target_port": target_port,
                }
        return desired

    async def _reconcile_once(self) -> None:
        desired = await self._desired()
        # Close listeners no longer wanted, or whose spec changed (host/port/
        # token/target moved) so a fresh endpoint replaces a stale one.
        for lp in list(self._listeners):
            cur = self._listeners[lp].spec
            want = desired.get(lp)
            if want is None or want != cur:
                await self._close_listener(lp)
        # Open new / replaced listeners.
        for lp, spec in desired.items():
            if lp not in self._listeners:
                await self._open_listener(lp, spec)

    async def _open_listener(self, loopback_port: int, spec: dict) -> None:
        async def _on_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await self._handle(spec, reader, writer)

        try:
            server = await asyncio.start_server(_on_conn, _LOOPBACK, loopback_port)
        except OSError as e:
            log.warning("broker shim: cannot bind %s:%d (%s)", _LOOPBACK, loopback_port, e)
            return
        self._listeners[loopback_port] = _Listener(server, spec)
        log.info(
            "broker shim: %s:%d -> %s:%d (printer %s, tunnel via %s:%d)",
            _LOOPBACK, loopback_port, _LOOPBACK, loopback_port,  # readable
            spec["printer_id"], spec["client_host"], spec["client_port"],
        )

    async def _close_listener(self, loopback_port: int) -> None:
        lis = self._listeners.pop(loopback_port, None)
        if not lis:
            return
        try:
            lis.server.close()
            await lis.server.wait_closed()
        except Exception:
            pass

    # ---- per-connection tunnel ------------------------------------------
    async def _handle(
        self,
        spec: dict,
        eng_reader: asyncio.StreamReader,
        eng_writer: asyncio.StreamWriter,
    ) -> None:
        """The engine opened a connection to our loopback port. Open ONE tunnel
        to the client's single gateway port, send the OPHQ1 preamble naming the
        printer + real target port, then splice bytes both ways verbatim."""
        cli_reader = cli_writer = None
        try:
            cli_reader, cli_writer = await asyncio.wait_for(
                asyncio.open_connection(spec["client_host"], spec["client_port"]),
                timeout=15.0,
            )
            preamble = (
                f"OPHQ1 {spec['token']} {spec['printer_id']} {spec['target_port']}\n"
            ).encode("ascii")
            cli_writer.write(preamble)
            await cli_writer.drain()
            await asyncio.gather(
                self._pipe(eng_reader, cli_writer),
                self._pipe(cli_reader, eng_writer),
            )
        except Exception as e:
            log.debug("broker shim tunnel error (printer %s): %s", spec.get("printer_id"), e)
        finally:
            for w in (eng_writer, cli_writer):
                if w is not None:
                    try:
                        w.close()
                    except Exception:
                        pass

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        except Exception:
            pass
        finally:
            try:
                if writer.can_write_eof():
                    writer.write_eof()
            except Exception:
                pass


# Module-level singleton (one per engine process = one per tenant).
_shim = BrokerShim()


def start_broker_shim() -> None:
    _shim.start()


async def stop_broker_shim() -> None:
    await _shim.stop()
