"""Elegoo SDCP v3 client (Centauri Carbon) over the printer's WebSocket.

Added by OpenPrintHQ, AGPL-3.0. Untested on hardware; see docs/elegoo-printers.md.

Protocol (SDCP v3.0.0, github.com/cbd-tech/SDCP-Smart-Device-Control-Protocol-V3.0.0,
and Elegoo's own elegoo-link SDK, Apache-2.0):

- WebSocket ``ws://<ip>:3030/websocket``. Text ``"ping"`` keeps it open.
- Requests: ``{"Id", "Data": {"Cmd", "Data", "RequestID", "MainboardID",
  "TimeStamp", "From"}, "Topic": "sdcp/request/<MainboardID>"}``.
- The printer pushes ``sdcp/status/<id>`` (``Status``), ``sdcp/attributes``,
  ``sdcp/response`` (``Data.Cmd`` + ``Data.Data.Ack``).
- Cmd 0 status, 1 attributes, 128 start (``Filename``, ``StartLayer``,
  ``Calibration_switch``, ``PrintPlatformType``, ``Tlp_Switch`` and, with a
  CANVAS, ``slot_map``), 129 pause, 130 stop, 131 resume, 324 CANVAS status,
  403 temperatures, 402 home.
- The MainboardID comes from the printer's UDP discovery reply (``M99999`` to
  port 3000), a status frame's topic, or the printer's serial field.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time
import uuid
from collections.abc import Callable

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.elegoo.status_map import apply_sdcp_status, canvas_slot_map, parse_canvas
from backend.app.services.material_slots import apply_units_to_state

logger = logging.getLogger(__name__)

SDCP_PORT = 3030
DISCOVERY_PORT = 3000

CMD_STATUS = 0
CMD_ATTRIBUTES = 1
CMD_START = 128
CMD_PAUSE = 129
CMD_STOP = 130
CMD_RESUME = 131
CMD_CANVAS_STATUS = 324
CMD_HOME = 402
CMD_CONTROL = 403

_PING_SECONDS = 10.0
_CANVAS_POLL_SECONDS = 30.0
_RECONNECT_BACKOFF_SECONDS = (2, 5, 10, 30, 60, 120, 300)
_ACTIVE = frozenset({"RUNNING", "PAUSE", "PREPARE"})


def discover(ip: str, timeout: float = 2.0) -> dict | None:
    """Unicast SDCP discovery: ``{"Id", "Data": {"MainboardID", ...}}`` or None."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(b"M99999", (ip, DISCOVERY_PORT))
        data, _ = sock.recvfrom(4096)
        reply = json.loads(data.decode("utf-8", errors="replace"))
        return reply if isinstance(reply, dict) else None
    except (OSError, ValueError):
        return None
    finally:
        sock.close()


class ElegooSDCPClient:
    """SDCP v3 WebSocket client. Satisfies ``PrinterClient``."""

    # No raw G-code in this protocol: the routes answer 400 for G-code and
    # homing (which sends G28) instead of a silent failure.
    UNSUPPORTED_METHODS = frozenset({"send_gcode"})

    flavor = "elegoo"

    def __init__(
        self,
        ip_address: str,
        port: int = SDCP_PORT,
        serial_number: str | None = None,
        on_state_change: Callable | None = None,
        on_print_start: Callable | None = None,
        on_print_complete: Callable | None = None,
        on_print_running_observed: Callable | None = None,
        on_layer_change: Callable | None = None,
        on_bed_temp_update: Callable | None = None,
    ) -> None:
        self.ip_address = ip_address
        self.port = port or SDCP_PORT
        self.serial_number = serial_number
        # The MainboardID comes from discovery or the first status topic. A
        # value typed into the serial field is the fallback when discovery is
        # blocked (UDP across VLANs); the API upper-cases serials, the
        # printer's id is lower-case hex.
        self.mainboard_id: str | None = None
        self._mainboard_hint: str | None = (
            serial_number.lower() if serial_number and ":" not in serial_number and len(serial_number) >= 8 else None
        )
        self.state = PrinterState()
        self._on_state_change = on_state_change
        self._on_print_start = on_print_start
        self._on_print_complete = on_print_complete
        self._on_print_running_observed = on_print_running_observed
        self._on_layer_change = on_layer_change
        self._on_bed_temp_update = on_bed_temp_update
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._ws = None
        self._stop = False
        self._prev_state: str | None = None
        self._prev_layer = 0
        self._prev_bed: float | None = None
        self.has_canvas = False
        self._brand_id: str | None = None  # discovery "Id", echoed in requests

    # -- lifecycle ----------------------------------------------------------
    @property
    def _ws_url(self) -> str:
        return f"ws://{self.ip_address}:{self.port}/websocket"

    def connect(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
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
        return self.state.connected

    async def _run_loop(self) -> None:
        from websockets.asyncio.client import connect as ws_connect

        attempt = 0
        while not self._stop:
            try:
                if not self.mainboard_id or not self._brand_id:
                    reply = await asyncio.to_thread(discover, self.ip_address)
                    if reply:
                        self._brand_id = reply.get("Id") or None
                        self.mainboard_id = self.mainboard_id or (reply.get("Data") or {}).get("MainboardID") or None
                    self.mainboard_id = self.mainboard_id or self._mainboard_hint
                async with ws_connect(self._ws_url, max_size=4 * 1024 * 1024, ping_interval=None) as ws:
                    self._ws = ws
                    attempt = 0
                    self.state.connected = True
                    self._emit()
                    await self._send(CMD_ATTRIBUTES)
                    await self._send(CMD_STATUS)
                    await self._send(CMD_CANVAS_STATUS)
                    pinger = asyncio.create_task(self._keepalive(ws))
                    try:
                        async for raw in ws:
                            if self._stop:
                                break
                            self.handle_message(raw)
                    finally:
                        pinger.cancel()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 - log and reconnect
                log = logger.warning if attempt < 3 else logger.debug
                log("[%s] SDCP connection error: %s", self.serial_number, exc)
            finally:
                self._ws = None
                if self.state.connected:
                    self.state.connected = False
                    self.state.state = "unknown"
                    self._emit()
            if self._stop:
                break
            delay = _RECONNECT_BACKOFF_SECONDS[min(attempt, len(_RECONNECT_BACKOFF_SECONDS) - 1)]
            attempt += 1
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

    async def _keepalive(self, ws) -> None:
        last_canvas = time.monotonic()
        while True:
            await asyncio.sleep(_PING_SECONDS)
            await ws.send("ping")
            if self.has_canvas and time.monotonic() - last_canvas > _CANVAS_POLL_SECONDS:
                last_canvas = time.monotonic()
                await self._send(CMD_CANVAS_STATUS)

    # -- messages -----------------------------------------------------------
    def build_request(self, cmd: int, data: dict | None = None) -> dict:
        mid = self.mainboard_id or ""
        return {
            "Id": self._brand_id or uuid.uuid4().hex,
            "Data": {
                "Cmd": cmd,
                "Data": data or {},
                "RequestID": uuid.uuid4().hex,
                "MainboardID": mid,
                "TimeStamp": int(time.time()),
                "From": 1,
            },
            "Topic": f"sdcp/request/{mid}",
        }

    async def _send(self, cmd: int, data: dict | None = None) -> None:
        ws = self._ws
        if ws is None:
            raise ConnectionError("SDCP not connected")
        await ws.send(json.dumps(self.build_request(cmd, data)))

    def handle_message(self, raw) -> None:
        if raw in ("pong", b"pong"):
            return
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        if not isinstance(msg, dict):
            return
        topic = str(msg.get("Topic") or "")
        if not self.mainboard_id and topic.count("/") >= 2:
            self.mainboard_id = topic.rsplit("/", 1)[-1] or None
        if "Status" in msg and isinstance(msg["Status"], dict):
            self._apply_status(msg["Status"])
        elif "CurrentStatus" in msg:
            self._apply_status(msg)  # some firmware sends the status fields flat
        elif "Attributes" in msg and isinstance(msg["Attributes"], dict):
            attrs = msg["Attributes"]
            self.mainboard_id = attrs.get("MainboardID") or self.mainboard_id
            if attrs.get("FirmwareVersion"):
                self.state.firmware_version = attrs.get("FirmwareVersion")
                self._emit()
        elif topic.startswith("sdcp/response"):
            data = msg.get("Data") or {}
            inner = data.get("Data") or {}
            if data.get("Cmd") == CMD_CANVAS_STATUS and isinstance(inner, dict):
                self._apply_canvas(inner)
            elif inner.get("Ack") not in (None, 0):
                logger.warning(
                    "[%s] SDCP Cmd %s refused: Ack=%s", self.serial_number, data.get("Cmd"), inner.get("Ack")
                )

    def _apply_canvas(self, inner: dict) -> None:
        if inner.get("Ack", 0) != 0:
            return
        info = inner.get("canvas_info") if isinstance(inner.get("canvas_info"), dict) else inner
        units = parse_canvas(info)
        self.has_canvas = any(u.connected for u in units)
        apply_units_to_state(self.state, units)
        self._emit()

    def _apply_status(self, status: dict) -> None:
        mapped = apply_sdcp_status(self.state, status)
        self.state.connected = True
        self._transitions(mapped)
        if self.state.layer_num != self._prev_layer:
            self._prev_layer = self.state.layer_num
            if self._on_layer_change and self.state.layer_num > 0:
                self._on_layer_change(self.state.layer_num)
        bed = self.state.temperatures.get("bed")
        if bed is not None and bed != self._prev_bed:
            self._prev_bed = bed
            if self._on_bed_temp_update:
                self._on_bed_temp_update(float(bed))
        self._emit()

    def _transitions(self, mapped: str) -> None:
        prev, self._prev_state = self._prev_state, mapped
        data = {
            "filename": self.state.gcode_file,
            "subtask_name": self.state.subtask_name,
            "gcode_file": self.state.gcode_file,
        }
        if mapped in ("RUNNING", "PREPARE") and prev not in _ACTIVE:
            if self._on_print_start:
                self._on_print_start(dict(data))
            if self._on_print_running_observed:
                self._on_print_running_observed(dict(data))
        elif prev in _ACTIVE and mapped not in _ACTIVE:
            done = dict(data)
            done["status"] = {"FINISH": "completed", "FAILED": "failed"}.get(mapped, "cancelled")
            done["last_layer_num"] = self.state.layer_num
            done["last_progress"] = self.state.progress
            done["print_duration"] = self.state.raw_data.get("print_duration")
            if self._on_print_complete:
                self._on_print_complete(done)

    def _emit(self) -> None:
        if self._on_state_change:
            self._on_state_change(self.state)

    def _schedule(self, cmd: int, data: dict | None = None) -> bool:
        if not self._loop or self._ws is None:
            logger.warning("[%s] SDCP Cmd %s dropped: not connected", self.serial_number, cmd)
            return False

        async def _do() -> None:
            try:
                await self._send(cmd, data)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] SDCP Cmd %s failed: %s", self.serial_number, cmd, exc)

        try:
            asyncio.run_coroutine_threadsafe(_do(), self._loop)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] SDCP schedule failed: %s", self.serial_number, exc)
            return False

    # -- PrinterClient control surface --------------------------------------
    def request_status_update(self) -> bool:
        return self._schedule(CMD_STATUS)

    def start_print_payload(self, filename: str, ams_mapping: list[int] | None, bed_levelling) -> dict:
        data = {
            "Filename": filename,
            "StartLayer": 0,
            "Calibration_switch": 0 if bed_levelling in (False, "off") else 1,
            "PrintPlatformType": 0,
            "Tlp_Switch": 0,
        }
        if self.has_canvas or ams_mapping:
            data["slot_map"] = canvas_slot_map(ams_mapping or [])
        return data

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
        if ams_mapping:
            self.state.ams_mapping = list(ams_mapping)
        return self._schedule(CMD_START, self.start_print_payload(filename, ams_mapping, bed_levelling))

    def stop_print(self) -> bool:
        return self._schedule(CMD_STOP)

    def pause_print(self) -> bool:
        return self._schedule(CMD_PAUSE)

    def resume_print(self) -> bool:
        return self._schedule(CMD_RESUME)

    def send_gcode(self, gcode: str) -> bool:
        return False  # SDCP has no raw G-code command

    def set_chamber_light(self, on: bool) -> bool:
        return self._schedule(CMD_CONTROL, {"LightStatus": {"SecondLight": 1 if on else 0}})

    def set_nozzle_temperature(self, target: int, nozzle: int = 0) -> bool:
        return self._schedule(CMD_CONTROL, {"TempTargetNozzle": int(target)})

    def set_bed_temperature(self, target: int) -> bool:
        return self._schedule(CMD_CONTROL, {"TempTargetHotbed": int(target)})
