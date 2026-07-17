"""Duet / RepRapFirmware HTTP client.

Auto-detects the transport on connect: RRF standalone (``rr_*`` on the Duet
board) vs DSF (``/machine/*`` on an SBC). Polls the RRF object model into the
shared ``PrinterState`` and issues control over g-code. Satisfies
``PrinterClient``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from urllib.parse import quote

import httpx

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.duet.status_map import apply_duet_status

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 3.0
_RECONNECT_BACKOFF_SECONDS = (2, 5, 10, 15)
_ACTIVE_STATES = frozenset({"RUNNING", "PAUSE"})
_COMPLETE_THRESHOLD = 99.0


class DuetClient:
    def __init__(
        self,
        ip_address: str,
        port: int = 80,
        api_key: str | None = None,  # DWC password (reused moonraker_api_key)
        serial_number: str | None = None,
        use_https: bool = False,
        on_state_change: Callable[[PrinterState], None] | None = None,
        on_print_start: Callable[[dict], None] | None = None,
        on_print_complete: Callable[[dict], None] | None = None,
        on_print_running_observed: Callable[[dict], None] | None = None,
        on_layer_change: Callable[[int], None] | None = None,
        on_bed_temp_update: Callable[[float], None] | None = None,
    ):
        self.ip_address = ip_address
        self.port = port
        self.password = api_key or "reprap"  # RRF default password
        self.serial_number = serial_number
        self._scheme = "https" if use_https else "http"

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
        self._mode: str | None = None  # "rrf" | "dsf"
        self._session_key: str | None = None

        self._prev_state: str | None = None
        self._prev_bed_temp: float | None = None

    @property
    def base_url(self) -> str:
        return f"{self._scheme}://{self.ip_address}:{self.port}/"

    def _headers(self) -> dict[str, str]:
        return {"X-Session-Key": self._session_key} if self._session_key else {}

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

    # -- connection detection ----------------------------------------------
    async def _detect_and_auth(self, client: httpx.AsyncClient) -> None:
        """Detect RRF vs DSF and establish an RRF session if needed."""
        try:
            r = await client.get(f"{self.base_url}rr_connect?password={quote(self.password)}")
            if r.status_code == 200:
                self._mode = "rrf"
                data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                sk = data.get("sessionKey")
                self._session_key = str(sk) if sk not in (None, "", "n/a") else None
                return
        except Exception:  # noqa: BLE001
            pass
        # Fall back to DSF (SBC).
        self._mode = "dsf"
        self._session_key = None

    async def _fetch_model(self, client: httpx.AsyncClient) -> dict:
        if self._mode == "dsf":
            r = await client.get(f"{self.base_url}machine/status", headers=self._headers())
            r.raise_for_status()
            return r.json() or {}
        # RRF: full object model.
        r = await client.get(f"{self.base_url}rr_model?flags=d99fn", headers=self._headers())
        if r.status_code == 401:
            self._session_key = None
            raise httpx.HTTPStatusError("session expired", request=r.request, response=r)
        r.raise_for_status()
        body = r.json() or {}
        return body.get("result", body)

    # -- poll loop ----------------------------------------------------------
    async def _run_loop(self) -> None:
        attempt = 0
        while not self._stop:
            try:
                async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
                    await self._detect_and_auth(client)
                    model = await self._fetch_model(client)
                    self._apply(model)
                    self.state.connected = True
                    attempt = 0
                    self._emit_state_change()
                    while not self._stop:
                        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                        self._apply(await self._fetch_model(client))
                        self._emit_state_change()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] Duet poll error: %s", self.serial_number, exc)
                if self.state.connected:
                    self.state.connected = False
                    self.state.state = "unknown"
                    self._emit_state_change()
            if self._stop:
                break
            delay = _RECONNECT_BACKOFF_SECONDS[min(attempt, len(_RECONNECT_BACKOFF_SECONDS) - 1)]
            attempt += 1
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

    def _apply(self, model: dict) -> None:
        mapped = apply_duet_status(self.state, model)
        self._detect_transitions(mapped)
        self._signal_bed()

    # -- transitions --------------------------------------------------------
    def _detect_transitions(self, mapped: str) -> None:
        prev = self._prev_state
        if mapped == prev:
            return
        self._prev_state = mapped
        data = {
            "filename": self.state.gcode_file,
            "subtask_name": self.state.subtask_name,
            "gcode_file": self.state.gcode_file,
        }
        if mapped == "RUNNING" and (prev is None or prev not in _ACTIVE_STATES):
            if self._on_print_start:
                self._on_print_start(dict(data))
            if self._on_print_running_observed:
                self._on_print_running_observed(dict(data))
        elif prev in _ACTIVE_STATES and mapped in ("IDLE", "FAILED"):
            done = dict(data)
            if mapped == "FAILED":
                done["status"] = "failed"
            else:
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

    # -- outbound gcode (fire-and-forget) -----------------------------------
    def _gcode(self, cmd: str) -> bool:
        if not self._loop:
            return False

        async def _do() -> None:
            try:
                async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
                    if self._mode == "dsf":
                        await client.post(f"{self.base_url}machine/code", content=cmd, headers=self._headers())
                    else:
                        await client.get(
                            f"{self.base_url}rr_gcode?gcode={quote(cmd)}", headers=self._headers()
                        )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] Duet gcode %r failed: %s", self.serial_number, cmd, exc)

        try:
            asyncio.run_coroutine_threadsafe(_do(), self._loop)
            return True
        except Exception:  # noqa: BLE001
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
        return self._gcode(f'M32 "0:/gcodes/{filename}"')

    def stop_print(self) -> bool:
        return self._gcode("M0")

    def pause_print(self) -> bool:
        return self._gcode("M25")

    def resume_print(self) -> bool:
        return self._gcode("M24")

    def send_gcode(self, gcode: str) -> bool:
        return self._gcode(gcode)

    def set_chamber_light(self, on: bool) -> bool:
        return False

    def set_nozzle_temp(self, temp: float) -> bool:
        return self._gcode(f"M104 S{int(temp)}")

    def set_bed_temp(self, temp: float) -> bool:
        return self._gcode(f"M140 S{int(temp)}")

    def home(self, axes: list[str] | None = None) -> bool:
        return self._gcode("G28")
