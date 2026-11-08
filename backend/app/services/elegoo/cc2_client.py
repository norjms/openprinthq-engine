"""Elegoo Centauri Carbon 2 client (MQTT broker on the printer).

Added by OpenPrintHQ, AGPL-3.0. Untested on hardware; see docs/elegoo-printers.md.

Protocol (Elegoo's elegoo-link SDK, Apache-2.0, and the CC2 protocol notes of
github.com/danielcherubini/elegoo-homeassistant, MIT, which include packet
captures of ElegooSlicer):

- MQTT 3.1.1 on ``<ip>:1883``, user ``elegoo``, password the access code
  (``123456`` when none is set). LAN-only mode must be on.
- Register: publish ``{"client_id", "request_id"}`` to
  ``elegoo/<sn>/api_register``; the answer on
  ``elegoo/<sn>/<request_id>/register_response`` must say ``"error": "ok"``.
- Commands ``{"id", "method", "params"}`` to ``elegoo/<sn>/<client_id>/api_request``,
  answers on ``.../api_response``; ``{"type": "PING"}`` every 10 s.
- Status: method 6000 on ``elegoo/<sn>/api_status``, a full status then deltas.
- Methods: 1002 status, 1020 start (``storage_media``, ``filename``,
  ``config.slot_map``), 1021 pause, 1022 stop, 1023 resume, 1026 home,
  1028 temperatures, 1029 light, 2005 CANVAS status.
- The serial (``sn``) comes from UDP discovery on port 52700
  (``{"id": 0, "method": 7000}``) or the printer's serial field.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import socket
import time
from collections.abc import Callable

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.elegoo.status_map import apply_cc2_status, canvas_slot_map, merge_delta, parse_canvas
from backend.app.services.material_slots import apply_units_to_state

logger = logging.getLogger(__name__)

CC2_MQTT_PORT = 1883
CC2_DISCOVERY_PORT = 52700
CC2_DEFAULT_PASSWORD = "123456"

M_STATUS = 1002
M_START = 1020
M_PAUSE = 1021
M_STOP = 1022
M_RESUME = 1023
M_HOME = 1026
M_TEMPERATURE = 1028
M_LIGHT = 1029
M_CANVAS = 2005
M_EVENT_STATUS = 6000

_HEARTBEAT_SECONDS = 10.0
_CANVAS_POLL_SECONDS = 30.0
_FULL_STATUS_SECONDS = 300.0
_ACTIVE = frozenset({"RUNNING", "PAUSE", "PREPARE"})


def discover_cc2(ip: str, timeout: float = 3.0) -> dict | None:
    """Directed CC2 discovery: the ``result`` dict (``sn``, ``lan_status``...) or None."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(json.dumps({"id": 0, "method": 7000}).encode(), (ip, CC2_DISCOVERY_PORT))
        data, _ = sock.recvfrom(4096)
        reply = json.loads(data.decode("utf-8", errors="replace"))
        return reply.get("result") if isinstance(reply, dict) else None
    except (OSError, ValueError):
        return None
    finally:
        sock.close()


def _client_id() -> str:
    ts = format(int(time.time() * 1000), "x")[-5:]
    return f"0cli{ts}{format(random.randint(0, 4095), 'x')}"[:10]  # noqa: S311 - not a secret


def _request_id() -> str:
    return f"{random.getrandbits(64):016x}{format(int(time.time() * 1000), 'x')}"


class ElegooCC2Client:
    """Centauri Carbon 2 MQTT client. Satisfies ``PrinterClient``."""

    # No raw G-code in this protocol: the routes answer 400 for G-code and
    # homing (which sends G28) instead of a silent failure.
    UNSUPPORTED_METHODS = frozenset({"send_gcode"})

    flavor = "elegoo_cc2"

    def __init__(
        self,
        ip_address: str,
        port: int = CC2_MQTT_PORT,
        serial_number: str | None = None,
        access_code: str | None = None,
        on_state_change: Callable | None = None,
        on_print_start: Callable | None = None,
        on_print_complete: Callable | None = None,
        on_print_running_observed: Callable | None = None,
        on_layer_change: Callable | None = None,
        on_bed_temp_update: Callable | None = None,
    ) -> None:
        self.ip_address = ip_address
        self.port = port or CC2_MQTT_PORT
        self.serial_number = serial_number
        self.access_code = access_code or None
        # The serial used in topics comes from discovery; one typed into the
        # serial field is the fallback when UDP discovery cannot reach the printer.
        self.sn: str | None = None
        self._sn_hint: str | None = serial_number if serial_number and ":" not in serial_number else None
        self.state = PrinterState()
        self._on_state_change = on_state_change
        self._on_print_start = on_print_start
        self._on_print_complete = on_print_complete
        self._on_print_running_observed = on_print_running_observed
        self._on_layer_change = on_layer_change
        self._on_bed_temp_update = on_bed_temp_update
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._mqtt = None
        self._stop = False
        self._registered = False
        self._client_id = _client_id()
        self._register_id = _request_id()
        self._seq = 0
        self._status: dict = {}
        self._prev_state: str | None = None
        self._prev_layer = 0
        self._prev_bed: float | None = None
        self.has_canvas = False

    # -- topics -------------------------------------------------------------
    def _t(self, suffix: str) -> str:
        return f"elegoo/{self.sn}/{suffix}"

    # -- lifecycle ----------------------------------------------------------
    def connect(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop or asyncio.get_event_loop()
        self._stop = False
        self._task = self._loop.create_task(self._run())

    def disconnect(self, timeout: float = 0) -> None:
        self._stop = True
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        if self._mqtt is not None:
            try:
                self._mqtt.loop_stop()
                self._mqtt.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._mqtt = None
        self._registered = False
        self.state.connected = False

    def check_staleness(self) -> bool:
        return self.state.connected

    async def _run(self) -> None:
        import paho.mqtt.client as mqtt

        while not self._stop and not self.sn:
            found = await asyncio.to_thread(discover_cc2, self.ip_address)
            if found and found.get("sn"):
                self.sn = str(found["sn"])
                if found.get("lan_status") == 0:
                    logger.warning("[%s] Centauri Carbon 2 is in cloud mode; enable LAN Only Mode", self.sn)
                break
            if self._sn_hint:
                self.sn = self._sn_hint
                break
            await asyncio.sleep(30)
        if self._stop:
            return

        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self._client_id,
            protocol=mqtt.MQTTv311,
        )
        client.username_pw_set("elegoo", self.access_code or CC2_DEFAULT_PASSWORD)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=2, max_delay=120)
        self._mqtt = client
        client.connect_async(self.ip_address, self.port, keepalive=60)
        client.loop_start()

        last_canvas = last_full = 0.0
        try:
            while not self._stop:
                await asyncio.sleep(_HEARTBEAT_SECONDS)
                if not self._registered:
                    continue
                self._publish({"type": "PING"})
                now = time.monotonic()
                if self.has_canvas and now - last_canvas > _CANVAS_POLL_SECONDS:
                    last_canvas = now
                    self._command(M_CANVAS)
                if now - last_full > _FULL_STATUS_SECONDS:
                    last_full = now
                    self._command(M_STATUS)
        except asyncio.CancelledError:
            pass

    # -- paho callbacks (network thread) ------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if getattr(reason_code, "is_failure", False):
            logger.warning("[%s] CC2 MQTT connect refused: %s", self.sn, reason_code)
            return
        self._registered = False
        self._register_id = _request_id()
        client.subscribe(self._t(f"{self._register_id}/register_response"))
        client.subscribe(self._t("api_status"))
        client.subscribe(self._t(f"{self._client_id}/api_response"))
        client.publish(
            self._t("api_register"), json.dumps({"client_id": self._client_id, "request_id": self._register_id})
        )

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self._registered = False
        if self.state.connected:
            self.state.connected = False
            self.state.state = "unknown"
            self._emit()

    def _on_message(self, client, userdata, msg) -> None:
        self.handle_message(msg.topic, msg.payload)

    def handle_message(self, topic: str, payload) -> None:
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        if topic.endswith("/register_response"):
            if data.get("error") == "ok":
                self._registered = True
                self.state.connected = True
                self._emit()
                self._command(M_STATUS)
                self._command(M_CANVAS)
            else:
                logger.warning("[%s] CC2 registration refused: %s", self.sn, data.get("error"))
            return
        if data.get("type") == "PONG":
            return
        method = data.get("method")
        result = data.get("result") if isinstance(data.get("result"), dict) else None
        if result is None:
            return
        if method == M_STATUS:
            self._status = dict(result)
            self._apply()
        elif method == M_EVENT_STATUS:
            if not self._status:
                self._status = dict(result)
            else:
                merge_delta(self._status, result)
            self._apply()
        elif method == M_CANVAS:
            info = result.get("canvas_info")
            if isinstance(info, dict):
                units = parse_canvas(info)
                self.has_canvas = any(u.connected for u in units)
                apply_units_to_state(self.state, units)
                self._emit()
        elif result.get("error_code") not in (None, 0):
            logger.warning("[%s] CC2 method %s failed: error_code %s", self.sn, method, result.get("error_code"))

    def _apply(self) -> None:
        mapped = apply_cc2_status(self.state, self._status)
        self.state.connected = True
        prev, self._prev_state = self._prev_state, mapped
        data = {
            "filename": self.state.gcode_file,
            "subtask_name": self.state.subtask_name,
            "gcode_file": self.state.gcode_file,
        }
        if mapped in _ACTIVE and prev not in _ACTIVE:
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
            done["filament_used_mm"] = self.state.raw_data.get("filament_used_mm")
            if self._on_print_complete:
                self._on_print_complete(done)
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

    def _emit(self) -> None:
        if self._on_state_change:
            self._on_state_change(self.state)

    # -- outbound -----------------------------------------------------------
    def _publish(self, payload: dict) -> bool:
        if self._mqtt is None or not self.sn:
            return False
        try:
            info = self._mqtt.publish(self._t(f"{self._client_id}/api_request"), json.dumps(payload))
            return info.rc == 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] CC2 publish failed: %s", self.sn, exc)
            return False

    def build_command(self, method: int, params: dict | None = None) -> dict:
        self._seq += 1
        return {"id": self._seq, "method": method, "params": params or {}}

    def _command(self, method: int, params: dict | None = None) -> bool:
        if not self._registered:
            logger.warning("[%s] CC2 method %s dropped: not registered", self.sn, method)
            return False
        return self._publish(self.build_command(method, params))

    # -- PrinterClient control surface --------------------------------------
    def request_status_update(self) -> bool:
        return self._command(M_STATUS)

    def start_print_params(self, filename: str, ams_mapping: list[int] | None) -> dict:
        return {
            "storage_media": "local",
            "filename": filename,
            "config": {"bedlevel_force": False, "slot_map": canvas_slot_map(ams_mapping or [])},
        }

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
        return self._command(M_START, self.start_print_params(filename, ams_mapping))

    def stop_print(self) -> bool:
        return self._command(M_STOP)

    def pause_print(self) -> bool:
        return self._command(M_PAUSE)

    def resume_print(self) -> bool:
        return self._command(M_RESUME)

    def send_gcode(self, gcode: str) -> bool:
        return False  # the CC2 protocol has no raw G-code method

    def set_chamber_light(self, on: bool) -> bool:
        return self._command(M_LIGHT, {"brightness": 255 if on else 0})

    def set_nozzle_temperature(self, target: int, nozzle: int = 0) -> bool:
        return self._command(M_TEMPERATURE, {"extruder": int(target)})

    def set_bed_temperature(self, target: int) -> bool:
        return self._command(M_TEMPERATURE, {"heater_bed": int(target)})
