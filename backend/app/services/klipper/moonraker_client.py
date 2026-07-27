"""Moonraker websocket client — the Klipper equivalent of ``BambuMQTTClient``.

Connects to a Klipper printer's Moonraker API over a JSON-RPC websocket,
subscribes to the printer objects we care about, and maps incoming status into
the shared ``PrinterState``. It satisfies the ``PrinterClient`` protocol so
``PrinterManager`` can hold it interchangeably with the Bambu client.

Design notes:
- Control methods (start/stop/pause/resume/light/gcode) are *synchronous*
  wrappers — like the Bambu client — that schedule a fire-and-forget JSON-RPC
  on the client's event loop and return immediately. Confirmation arrives via
  the next ``notify_status_update``.
- A single background task owns the websocket: connect → subscribe → read
  loop, with reconnect + backoff. Connection loss flips ``state.connected``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

import httpx

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.klipper.profiles import KlipperProfile, get_profile
from backend.app.services.klipper.status_map import (
    ACTIVE_KLIPPER_STATES,
    KLIPPER_COMPLETION_STATUS,
    apply_status_objects,
)

logger = logging.getLogger(__name__)

# Objects to subscribe to. ``None`` = all fields of that object.
_SUBSCRIBE_OBJECTS = {
    "print_stats": None,
    "virtual_sdcard": None,
    "display_status": None,
    "extruder": None,
    "heater_bed": None,
    "fan": None,
    "toolhead": ["homed_axes", "print_time"],
}

_RECONNECT_BACKOFF_SECONDS = (1, 2, 5, 10, 15)


class MoonrakerClient:
    def __init__(
        self,
        ip_address: str,
        port: int = 7125,
        api_key: str | None = None,
        model: str | None = None,
        serial_number: str | None = None,
        on_state_change: Callable[[PrinterState], None] | None = None,
        on_print_start: Callable[[dict], None] | None = None,
        on_print_complete: Callable[[dict], None] | None = None,
        on_print_running_observed: Callable[[dict], None] | None = None,
        on_layer_change: Callable[[int], None] | None = None,
        on_bed_temp_update: Callable[[float], None] | None = None,
    ):
        self.ip_address = ip_address
        self.port = port
        self.api_key = api_key or None
        self.serial_number = serial_number
        self.profile: KlipperProfile = get_profile(model)

        self.state = PrinterState()

        self._on_state_change = on_state_change
        self._on_print_start = on_print_start
        self._on_print_complete = on_print_complete
        self._on_print_running_observed = on_print_running_observed
        self._on_layer_change = on_layer_change
        self._on_bed_temp_update = on_bed_temp_update

        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._ws = None  # active websocket connection
        self._stop = False
        self._rpc_id = 0

        # Moonraker object name for the chamber temperature. The profile gives a
        # default, but the real name varies by Klipper config (temperature_sensor
        # chamber / temperature_fan chamber / heater_generic chamber), so we
        # auto-discover it from /printer/objects/list on connect.
        self._chamber_object: str | None = self.profile.chamber_object

        # transition / change tracking
        self._prev_raw_state: str | None = None
        self._prev_layer: int = 0
        self._prev_bed_temp: float | None = None

    # -- urls ---------------------------------------------------------------
    @property
    def _ws_url(self) -> str:
        return f"ws://{self.ip_address}:{self.port}/websocket"

    @property
    def _http_base(self) -> str:
        return f"http://{self.ip_address}:{self.port}"

    # -- lifecycle ----------------------------------------------------------
    def connect(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Start the background connection task (non-blocking)."""
        self._loop = loop or asyncio.get_event_loop()
        self._stop = False
        self._task = self._loop.create_task(self._run_loop())

    def disconnect(self, timeout: float = 0) -> None:
        self._stop = True
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self._ws = None
        self.state.connected = False

    def check_staleness(self) -> bool:
        """Moonraker pushes only on change; liveness is the ws connection
        itself (auto keep-alive pings). The read loop flips ``connected`` on
        drop, so just report the current flag."""
        return self.state.connected

    # -- the connection task ------------------------------------------------
    async def _run_loop(self) -> None:
        from websockets.asyncio.client import connect as ws_connect

        attempt = 0
        while not self._stop:
            try:
                headers = {"X-Api-Key": self.api_key} if self.api_key else None
                url = self._ws_url
                token = await self._maybe_oneshot_token()
                if token:
                    url = f"{url}?token={token}"

                async with ws_connect(url, additional_headers=headers, max_size=8 * 1024 * 1024) as ws:
                    self._ws = ws
                    attempt = 0
                    logger.info("[%s] Moonraker connected at %s", self.serial_number, self._ws_url)
                    await self._resolve_chamber_object()
                    await self._subscribe(ws)
                    await self._query_printer_info(ws)
                    self.state.connected = True
                    self._emit_state_change()
                    async for raw in ws:
                        if self._stop:
                            break
                        self._handle_message(raw)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 — log and reconnect
                logger.warning("[%s] Moonraker connection error: %s", self.serial_number, exc)
            finally:
                self._ws = None
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

    async def _maybe_oneshot_token(self) -> str | None:
        """Fetch a websocket oneshot token when an API key is configured.

        Open-LAN Moonraker (the default) needs no key; this is a no-op then.
        """
        if not self.api_key:
            return None
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{self._http_base}/access/oneshot_token",
                    headers={"X-Api-Key": self.api_key},
                )
                resp.raise_for_status()
                return resp.json().get("result")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] Moonraker oneshot token failed: %s", self.serial_number, exc)
            return None

    def _subscribe_objects(self) -> dict:
        """Objects to subscribe to, including the resolved chamber object."""
        objects = dict(_SUBSCRIBE_OBJECTS)
        if self._chamber_object:
            objects[self._chamber_object] = None
        return objects

    async def _resolve_chamber_object(self) -> None:
        """Discover the chamber temperature object for this printer.

        Chamber sensors are named inconsistently across Klipper configs
        (``temperature_sensor chamber``, ``temperature_fan chamber``,
        ``heater_generic chamber``, …). Query the object list and pick the best
        match so chamber temp works regardless of config. Falls back to the
        profile default on any error.
        """
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                headers = {"X-Api-Key": self.api_key} if self.api_key else None
                resp = await client.get(f"{self._http_base}/printer/objects/list", headers=headers)
                resp.raise_for_status()
                names = resp.json().get("result", {}).get("objects", [])
        except Exception as exc:  # noqa: BLE001
            logger.info("[%s] chamber object discovery skipped: %s", self.serial_number, exc)
            return

        # Prefer a real heater (has a target), then a temperature_fan, then a
        # plain sensor. Only consider objects whose name mentions "chamber".
        candidates = [n for n in names if "chamber" in n.lower()]
        for prefix in ("heater_generic", "temperature_fan", "temperature_sensor"):
            match = next((n for n in candidates if n.lower().startswith(prefix)), None)
            if match:
                self._chamber_object = match
                logger.info("[%s] chamber object resolved to %r", self.serial_number, match)
                return
        # No chamber object on this printer — clear so we don't subscribe to a
        # non-existent object (the profile default may not exist here).
        self._chamber_object = None

    async def _subscribe(self, ws) -> None:
        req_id = self._next_id()
        await ws.send(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "printer.objects.subscribe",
                    "params": {"objects": self._subscribe_objects()},
                    "id": req_id,
                }
            )
        )
        # The subscribe response carries the initial full status snapshot; the
        # generic read loop will apply it when it arrives (result.status).

    async def _query_printer_info(self, ws) -> None:
        """Request ``printer.info`` so we can surface the Klipper version.

        The response (handled in _handle_message) carries ``software_version``,
        which we map onto ``state.firmware_version`` for the card's version badge.
        """
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "printer.info", "id": self._next_id()}))

    # -- message handling ---------------------------------------------------
    def _handle_message(self, raw: str | bytes) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return

        method = msg.get("method")
        if method == "notify_status_update":
            params = msg.get("params") or []
            if params and isinstance(params[0], dict):
                self._apply_and_signal(params[0])
        elif method in ("notify_klippy_disconnected", "notify_klippy_shutdown"):
            self.state.state = "unknown"
            self._emit_state_change()
        elif "result" in msg and isinstance(msg["result"], dict):
            result = msg["result"]
            status = result.get("status")
            if isinstance(status, dict):
                # subscribe / objects.query response: {"status": {...}, "eventtime": ...}
                self._apply_and_signal(status)
            elif "software_version" in result:
                # printer.info response — surface the Klipper version on the card.
                self.state.firmware_version = result.get("software_version")
                self._emit_state_change()

    def _apply_and_signal(self, objects: dict) -> None:
        raw_state = apply_status_objects(self.state, objects, self._chamber_object)
        self._detect_transitions(raw_state)
        self._signal_layer_and_bed()
        self._emit_state_change()

    def _detect_transitions(self, raw_state: str | None) -> None:
        if raw_state is None or raw_state == self._prev_raw_state:
            return
        prev = self._prev_raw_state
        self._prev_raw_state = raw_state

        data = {
            "filename": self.state.gcode_file,
            "subtask_name": self.state.subtask_name,
            "gcode_file": self.state.gcode_file,
        }

        if raw_state == "printing" and (prev is None or prev not in ACTIVE_KLIPPER_STATES):
            if self._on_print_start:
                self._on_print_start(dict(data))
            if self._on_print_running_observed:
                self._on_print_running_observed(dict(data))
        elif raw_state in KLIPPER_COMPLETION_STATUS and prev in ACTIVE_KLIPPER_STATES:
            done = dict(data)
            done["status"] = KLIPPER_COMPLETION_STATUS[raw_state]
            done["last_layer_num"] = self.state.layer_num
            done["last_progress"] = self.state.progress
            done["print_duration"] = self.state.raw_data.get("print_duration")
            if self._on_print_complete:
                self._on_print_complete(done)

    def _signal_layer_and_bed(self) -> None:
        if self.state.layer_num != self._prev_layer:
            self._prev_layer = self.state.layer_num
            if self._on_layer_change and self.state.layer_num > 0:
                self._on_layer_change(self.state.layer_num)

        bed = self.state.temperatures.get("bed")
        if bed is not None and bed != self._prev_bed_temp:
            self._prev_bed_temp = bed
            if self._on_bed_temp_update:
                self._on_bed_temp_update(float(bed))

    def _emit_state_change(self) -> None:
        if self._on_state_change:
            self._on_state_change(self.state)

    # -- outbound JSON-RPC --------------------------------------------------
    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    def _send_rpc(self, method: str, params: dict | None = None) -> bool:
        """Schedule a fire-and-forget JSON-RPC on the connection loop.

        Returns True if it was scheduled (not whether the printer accepted it),
        matching the Bambu client's publish-and-return semantics.
        """
        if not self._loop:
            logger.warning("[%s] Moonraker RPC %s dropped: no loop", self.serial_number, method)
            return False

        async def _do() -> None:
            ws = self._ws
            if ws is None:
                logger.warning("[%s] Moonraker RPC %s dropped: not connected", self.serial_number, method)
                return
            payload = {"jsonrpc": "2.0", "method": method, "id": self._next_id()}
            if params:
                payload["params"] = params
            try:
                await ws.send(json.dumps(payload))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] Moonraker RPC %s failed: %s", self.serial_number, method, exc)

        try:
            asyncio.run_coroutine_threadsafe(_do(), self._loop)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] Moonraker RPC %s schedule failed: %s", self.serial_number, method, exc)
            return False

    # -- PrinterClient control surface --------------------------------------
    def request_status_update(self) -> bool:
        return self._send_rpc("printer.objects.query", {"objects": self._subscribe_objects()})

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
        # Bambu-specific args are ignored on Klipper; the file must already be
        # uploaded to Moonraker's gcode store (see moonraker_files.upload_gcode).
        return self._send_rpc("printer.print.start", {"filename": filename})

    def stop_print(self) -> bool:
        return self._send_rpc("printer.print.cancel")

    def pause_print(self) -> bool:
        return self._send_rpc("printer.print.pause")

    def resume_print(self) -> bool:
        return self._send_rpc("printer.print.resume")

    def send_gcode(self, gcode: str) -> bool:
        return self._send_rpc("printer.gcode.script", {"script": gcode})

    def set_chamber_light(self, on: bool) -> bool:
        macro = self.profile.macros.light_on if on else self.profile.macros.light_off
        if not macro:
            return False
        return self.send_gcode(macro)

    # -- Voron / Klipper convenience controls -------------------------------
    def home(self) -> bool:
        return self.send_gcode(self.profile.macros.home)

    def level(self) -> bool:
        if not self.profile.macros.level:
            return False
        return self.send_gcode(self.profile.macros.level)

    def emergency_stop(self) -> bool:
        return self._send_rpc("printer.emergency_stop")

    def set_nozzle_temp(self, temp: float) -> bool:
        return self.send_gcode(f"M104 S{int(temp)}")

    def set_bed_temp(self, temp: float) -> bool:
        return self.send_gcode(f"M140 S{int(temp)}")

    # Names the generic temperature routes call (parity with the Bambu client,
    # which exposes set_nozzle_temperature/set_bed_temperature). Without these,
    # POST /temperature/{nozzle,bed} raised AttributeError on Klipper printers —
    # the Set/Off temp buttons on a Moonraker printer did nothing (surfaced as a
    # misleading "auth unavailable" 503 from the fail-closed auth middleware).
    # Klipper here is single-extruder, so the nozzle index is ignored.
    def set_nozzle_temperature(self, target: int, nozzle: int = 0) -> bool:
        return self.set_nozzle_temp(float(target))

    def set_bed_temperature(self, target: int) -> bool:
        return self.set_bed_temp(float(target))
