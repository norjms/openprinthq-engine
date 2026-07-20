"""Obico client — reachability check only, no live status (see package docstring).

Satisfies ``PrinterClient`` structurally so it plugs into ``PrinterManager``
like every other transport, but most of the surface is a deliberate no-op:
there is no printer state to poll, no pause/resume/stop/gcode API, and no
"start an already-uploaded file by name" call (Obico's upload always sends
the file bytes — see ``obico_files.upload_gcode``, used directly by the queue
dispatcher instead of this class's ``start_print``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import httpx

from backend.app.services.bambu_mqtt import PrinterState

logger = logging.getLogger(__name__)

_PING_INTERVAL_SECONDS = 60.0
_RECONNECT_BACKOFF_SECONDS = (5, 15, 30, 60)


def normalize_host(host: str) -> str:
    """Match OrcaSlicer's ``Obico::make_url`` — assume http:// if no scheme given."""
    if host.startswith(("http://", "https://")):
        return host.rstrip("/")
    return f"http://{host.rstrip('/')}"


class ObicoClient:
    def __init__(
        self,
        ip_address: str,
        port: int = 80,  # unused — the Obico host URL carries its own scheme/port
        api_key: str | None = None,
        serial_number: str | None = None,
        use_https: bool = False,  # unused — inferred from the host URL's scheme
        on_state_change: Callable[[PrinterState], None] | None = None,
        on_print_start: Callable[[dict], None] | None = None,
        on_print_complete: Callable[[dict], None] | None = None,
        on_print_running_observed: Callable[[dict], None] | None = None,
        on_layer_change: Callable[[int], None] | None = None,
        on_bed_temp_update: Callable[[float], None] | None = None,
    ):
        self.host = normalize_host(ip_address)
        self.api_key = api_key
        self.serial_number = serial_number

        self.state = PrinterState()

        self._on_state_change = on_state_change

        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._stop = False

    # -- lifecycle ----------------------------------------------------------
    def connect(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop or asyncio.get_event_loop()
        self._stop = False
        self._task = self._loop.create_task(self._run_loop())

    def disconnect(self, timeout: float = 0) -> None:
        self._stop = True
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self.state.connected = False

    def check_staleness(self) -> bool:
        return self.state.connected

    # -- reachability-only "poll" loop ---------------------------------------
    async def _run_loop(self) -> None:
        attempt = 0
        while not self._stop:
            try:
                reachable = await self._ping()
                self.state.connected = reachable
                self.state.state = "IDLE" if reachable else "unknown"
                attempt = 0
                self._emit_state_change()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] Obico ping error: %s", self.serial_number, exc)
                if self.state.connected:
                    self.state.connected = False
                    self.state.state = "unknown"
                    self._emit_state_change()
            if self._stop:
                break
            delay = _PING_INTERVAL_SECONDS if self.state.connected else _RECONNECT_BACKOFF_SECONDS[
                min(attempt, len(_RECONNECT_BACKOFF_SECONDS) - 1)
            ]
            if not self.state.connected:
                attempt += 1
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

    async def _ping(self) -> bool:
        if not self.api_key:
            return False
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{self.host}/api/v1/version/", headers={"Authorization": f"Bearer {self.api_key}"}
            )
            return resp.status_code == 200

    def _emit_state_change(self) -> None:
        if self._on_state_change:
            self._on_state_change(self.state)

    # -- PrinterClient control surface --------------------------------------
    # Obico exposes no pause/resume/stop/gcode/light API and no "start an
    # already-uploaded file by name" call — these are all deliberate no-ops.
    def request_status_update(self) -> bool:
        return True

    def start_print(
        self,
        filename: str,
        plate_id: int = 1,
        ams_mapping: list[int] | None = None,
        bed_levelling: bool = True,
        flow_cali: bool = False,
        vibration_cali: bool = True,
        layer_inspect: bool = False,
        timelapse: bool = False,
        use_ams: bool = True,
    ) -> bool:
        return False

    def stop_print(self) -> bool:
        return False

    def pause_print(self) -> bool:
        return False

    def resume_print(self) -> bool:
        return False

    def send_gcode(self, gcode: str) -> bool:
        return False

    def set_chamber_light(self, on: bool) -> bool:
        return False
