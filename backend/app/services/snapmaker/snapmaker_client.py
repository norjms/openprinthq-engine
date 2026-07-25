"""Snapmaker LAN HTTP client — the Snapmaker equivalent of the OctoPrint/MKS
clients. Added by OpenPrintHQ (2026-07-25), AGPL-3.0.

Snapmaker's networked printers expose an HTTP API on :8080. Access is
token-based: POST /api/v1/connect returns a token and the printer's touchscreen
shows an authorization dialog the user accepts once; subsequent calls carry
``?token=``. We poll /api/v1/status on an interval and map into the shared
``PrinterState``. Satisfies ``PrinterClient``.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import httpx

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.snapmaker.status_map import apply_snapmaker_status

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 3.0
_COMPLETE_THRESHOLD = 99.0
_ACTIVE_STATES = frozenset({"RUNNING", "PAUSE"})


class SnapmakerClient:
    """HTTP polling client for Snapmaker 2.0 / Artisan / J1 printers."""

    def __init__(
        self,
        ip_address: str,
        port: int = 8080,
        token: str | None = None,
        serial_number: str | None = None,
        on_state_change: Callable | None = None,
        on_print_start: Callable | None = None,
        on_print_complete: Callable | None = None,
        on_print_running_observed: Callable | None = None,
        on_layer_change: Callable | None = None,
        on_bed_temp_update: Callable | None = None,
    ) -> None:
        self.ip_address = ip_address
        self.port = port or 8080
        self.token = token or None
        self.serial_number = serial_number
        self.state = PrinterState()
        self._on_state_change = on_state_change
        self._on_print_start = on_print_start
        self._on_print_complete = on_print_complete
        self._on_print_running_observed = on_print_running_observed
        self._on_layer_change = on_layer_change
        self._on_bed_temp_update = on_bed_temp_update
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._stop = False
        self._prev_state: str | None = None
        self._prev_bed_temp: float | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.ip_address}:{self.port}/api/v1"

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

    async def _ensure_token(self, client: httpx.AsyncClient) -> str | None:
        if self.token:
            return self.token
        # First connect triggers the printer's on-screen authorization dialog.
        r = await client.post(f"{self.base_url}/connect", data={})
        r.raise_for_status()
        self.token = (r.json() or {}).get("token")
        return self.token

    async def _run_loop(self) -> None:
        while not self._stop:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await self._ensure_token(client)
                    await self._poll_once(client)
                    self.state.connected = True
                    self._emit_state_change()
                    while not self._stop:
                        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                        await self._poll_once(client)
                        self._emit_state_change()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] Snapmaker poll error: %s", self.serial_number, exc)
                if self.state.connected:
                    self.state.connected = False
                    self.state.state = "unknown"
                    self._emit_state_change()
            if self._stop:
                break
            await asyncio.sleep(5)

    async def _poll_once(self, client: httpx.AsyncClient) -> None:
        params = {"token": self.token} if self.token else None
        r = await client.get(f"{self.base_url}/status", params=params)
        if r.status_code in (401, 403):
            # Not authorized yet (touchscreen dialog not accepted) — re-prompt.
            self.token = None
            r.raise_for_status()
        r.raise_for_status()
        mapped = apply_snapmaker_status(self.state, r.json() or {})
        self._detect_transitions(mapped)
        self._signal_bed()

    def _detect_transitions(self, mapped: str) -> None:
        prev = self._prev_state
        self._prev_state = mapped
        data = {
            "filename": self.state.gcode_file,
            "subtask_name": self.state.subtask_name,
            "gcode_file": self.state.gcode_file,
        }
        if mapped == "RUNNING" and prev not in _ACTIVE_STATES:
            if self._on_print_start:
                self._on_print_start(dict(data))
            if self._on_print_running_observed:
                self._on_print_running_observed(dict(data))
        elif prev in _ACTIVE_STATES and mapped == "IDLE":
            done = dict(data)
            done["status"] = "completed" if (self.state.progress or 0) >= _COMPLETE_THRESHOLD else "cancelled"
            done["last_progress"] = self.state.progress
            done["print_duration"] = self.state.raw_data.get("print_duration")
            if self._on_print_complete:
                self._on_print_complete(done)

    def _signal_bed(self) -> None:
        bed = self.state.temperatures.get("bed")
        if bed is not None and bed != self._prev_bed_temp:
            self._prev_bed_temp = bed
            if self._on_bed_temp_update:
                self._on_bed_temp_update(float(bed))

    def _emit_state_change(self) -> None:
        if self._on_state_change:
            self._on_state_change(self.state)

    # -- outbound HTTP (fire-and-forget, scheduled on the loop) -------------
    def _post(self, path: str, data: dict | None = None) -> bool:
        if not self._loop:
            return False

        async def _do() -> None:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    params = {"token": self.token} if self.token else None
                    resp = await client.post(f"{self.base_url}{path}", params=params, data=data or {})
                    if resp.status_code >= 400:
                        logger.warning("[%s] Snapmaker POST %s -> %s", self.serial_number, path, resp.status_code)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] Snapmaker POST %s failed: %s", self.serial_number, path, exc)

        try:
            asyncio.run_coroutine_threadsafe(_do(), self._loop)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] Snapmaker schedule %s failed: %s", self.serial_number, path, exc)
            return False

    # -- PrinterClient control surface --------------------------------------
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
        # Snapmaker uploads-and-starts in one call (snapmaker_files.upload_gcode);
        # this is the explicit start fallback.
        return self._post("/start_print")

    def stop_print(self) -> bool:
        return self._post("/stop_print")

    def pause_print(self) -> bool:
        return self._post("/pause_print")

    def resume_print(self) -> bool:
        return self._post("/resume_print")

    def send_gcode(self, gcode: str) -> bool:
        return self._post("/execute_code", data={"code": gcode})

    def set_chamber_light(self, on: bool) -> bool:
        return False  # not modelled for Snapmaker

    # -- convenience controls ----------------------------------------------
    def set_nozzle_temp(self, temp: float) -> bool:
        return self._post("/set_temperature", data={"nozzleTemperatureValue": int(temp)})

    def set_bed_temp(self, temp: float) -> bool:
        return self._post("/set_temperature", data={"heatedBedTemperatureValue": int(temp)})
