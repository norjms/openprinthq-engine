"""Anycubic Kobra client over the printer's LAN-mode MQTT broker.

Added by OpenPrintHQ, AGPL-3.0. Untested on hardware; see docs/anycubic-printers.md.

- Credentials and the client certificate come from the LAN handshake
  (``lan_handshake.py``); LAN mode must be on at the printer.
- MQTT over TLS on the broker the handshake names (``<ip>:9883``), mutual TLS
  with the printer's own certificate (self-signed, so not verified).
- Subscribe ``anycubic/anycubicCloud/v1/printer/public/<model>/<device>/#``.
- Queries and controls go to ``.../web/printer/<model>/<device>/<type>``
  (validated for pause, resume, stop, temperatures, light and the ACE);
  the print start goes to ``.../slicer/printer/...`` as the slicer sends it.
- ``info`` is polled every 30 s, the ACE (``multiColorBox`` ``getInfo``) every
  30 s while one is attached, since the printer does not push it while a job
  runs steadily.
- Print start: ``{"type": "print", "action": "start", "data": {"taskid": "-1",
  "filename", "filetype": 1, "ams_settings": {"use_ams", "ams_box_mapping"}}}``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import ssl
import tempfile
import time
import uuid
from collections.abc import Callable

from backend.app.services.anycubic.lan_handshake import AnycubicCloudModeError, LanCredentials, handshake
from backend.app.services.anycubic.status_map import (
    ace_units,
    ams_box_mapping,
    apply_info,
    apply_progress,
    apply_temps,
    map_project_state,
    merge_ace,
)
from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.material_slots import apply_units_to_state

logger = logging.getLogger(__name__)

PREFIX = "anycubic/anycubicCloud/v1"
_POLL_SECONDS = 30.0
_RETRY_BACKOFF_SECONDS = (5, 15, 30, 60, 120, 300)
_ACTIVE = frozenset({"RUNNING", "PAUSE", "PREPARE"})
LIGHT_CHAMBER = 2


class AnycubicClient:
    """Anycubic LAN-mode MQTT client. Satisfies ``PrinterClient``."""

    flavor = "anycubic"
    # No raw G-code over the LAN protocol: the routes answer 400 for G-code and
    # homing (which sends G28).
    UNSUPPORTED_METHODS = frozenset({"send_gcode"})

    def __init__(
        self,
        ip_address: str,
        serial_number: str | None = None,
        on_state_change: Callable | None = None,
        on_print_start: Callable | None = None,
        on_print_complete: Callable | None = None,
        on_print_running_observed: Callable | None = None,
        on_layer_change: Callable | None = None,
        on_bed_temp_update: Callable | None = None,
    ) -> None:
        self.ip_address = ip_address
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
        self._mqtt = None
        self._stop = False
        self._creds: LanCredentials | None = None
        self._cert_dir: str | None = None
        self._ace: dict[int, dict] = {}
        self._prev_state: str | None = None
        self._prev_layer = 0
        self._prev_bed: float | None = None

    # -- topics -------------------------------------------------------------
    def _topic(self, kind: str, msg_type: str) -> str:
        c = self._creds
        return f"{PREFIX}/{kind}/printer/{c.model_id}/{c.device_id}/{msg_type}"

    @property
    def has_ace(self) -> bool:
        return bool(self._ace)

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
        self._close_mqtt()
        self.state.connected = False

    def check_staleness(self) -> bool:
        return self.state.connected

    def _close_mqtt(self) -> None:
        if self._mqtt is not None:
            try:
                self._mqtt.loop_stop()
                self._mqtt.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._mqtt = None
        if self._cert_dir:
            shutil.rmtree(self._cert_dir, ignore_errors=True)
            self._cert_dir = None

    async def _run(self) -> None:
        attempt = 0
        while not self._stop:
            try:
                self._creds = await handshake(self.ip_address)
                self._start_mqtt()
                attempt = 0
                while not self._stop:
                    await asyncio.sleep(_POLL_SECONDS)
                    self._query("info")
                    if self.has_ace:
                        self._query("multiColorBox", action="getInfo")
            except asyncio.CancelledError:
                break
            except AnycubicCloudModeError as exc:
                logger.warning("[%s] Anycubic: %s", self.serial_number, exc)
            except Exception as exc:  # noqa: BLE001 - log and retry
                log = logger.warning if attempt < 3 else logger.debug
                log("[%s] Anycubic connection error: %s", self.serial_number, exc)
            self._close_mqtt()
            if self.state.connected:
                self.state.connected = False
                self.state.state = "unknown"
                self._emit()
            if self._stop:
                break
            delay = _RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)]
            attempt += 1
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

    def _start_mqtt(self) -> None:
        import paho.mqtt.client as mqtt

        c = self._creds
        self._cert_dir = tempfile.mkdtemp(prefix="ophq-anycubic-")
        os.chmod(self._cert_dir, 0o700)
        cert = os.path.join(self._cert_dir, "client.crt")
        key = os.path.join(self._cert_dir, "client.key")
        for path, body in ((cert, c.device_cert), (key, c.device_key)):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(body)
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"ophq-{uuid.uuid4().hex[:10]}",
            protocol=mqtt.MQTTv311,
        )
        client.username_pw_set(c.username, c.password)
        # The printer's broker presents a self-signed certificate.
        client.tls_set(
            certfile=cert if c.device_cert else None, keyfile=key if c.device_key else None, cert_reqs=ssl.CERT_NONE
        )  # noqa: S501
        client.tls_insecure_set(True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(min_delay=2, max_delay=120)
        self._mqtt = client
        client.connect_async(c.broker_host, c.broker_port, keepalive=60)
        client.loop_start()

    # -- paho callbacks -----------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if getattr(reason_code, "is_failure", False):
            logger.warning("[%s] Anycubic MQTT connect refused: %s", self.serial_number, reason_code)
            return
        c = self._creds
        client.subscribe(f"{PREFIX}/printer/public/{c.model_id}/{c.device_id}/#")
        self.state.connected = True
        self._emit()
        self._query("info")
        self._query("multiColorBox", action="getInfo")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if self.state.connected:
            self.state.connected = False
            self.state.state = "unknown"
            self._emit()

    def _on_message(self, client, userdata, msg) -> None:
        self.handle_message(msg.topic, msg.payload)

    def handle_message(self, topic: str, payload) -> None:
        try:
            body = json.loads(payload)
        except (TypeError, ValueError):
            return
        if not isinstance(body, dict) or not topic.endswith("/report"):
            return
        msg_type = topic.rsplit("/", 2)[-2]
        data = body.get("data")
        mapped: str | None = None
        if msg_type == "info" and isinstance(data, dict):
            mapped = apply_info(self.state, data)
        elif msg_type == "tempature" and isinstance(data, dict):
            apply_temps(self.state, data)
        elif msg_type == "print" and isinstance(data, dict):
            if "progress" in data:
                apply_progress(self.state, data)
            # The outer state of a print report is the job's lifecycle step.
            if body.get("state") and body.get("action") in ("start", "pause", "resume", "stop"):
                mapped = map_project_state(body.get("state"))
                if mapped == "unknown":
                    mapped = None
                else:
                    self.state.state = mapped
        elif msg_type == "multiColorBox" and isinstance(data, dict):
            merge_ace(self._ace, data)
            apply_units_to_state(self.state, ace_units(self._ace))
        else:
            return
        if mapped:
            self._transitions(mapped)
        self._signal()
        self._emit()

    def _transitions(self, mapped: str) -> None:
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

    def _signal(self) -> None:
        if self.state.layer_num != self._prev_layer:
            self._prev_layer = self.state.layer_num
            if self._on_layer_change and self.state.layer_num > 0:
                self._on_layer_change(self.state.layer_num)
        bed = self.state.temperatures.get("bed")
        if bed is not None and bed != self._prev_bed:
            self._prev_bed = bed
            if self._on_bed_temp_update:
                self._on_bed_temp_update(float(bed))

    def _emit(self) -> None:
        if self._on_state_change:
            self._on_state_change(self.state)

    # -- outbound -----------------------------------------------------------
    def build_message(self, msg_type: str, action: str, data) -> dict:
        return {
            "type": msg_type,
            "action": action,
            "timestamp": int(time.time() * 1000),
            "msgid": str(uuid.uuid4()),
            "data": data,
        }

    def _publish(self, kind: str, msg_type: str, action: str, data) -> bool:
        if self._mqtt is None or self._creds is None:
            logger.warning("[%s] Anycubic %s/%s dropped: not connected", self.serial_number, msg_type, action)
            return False
        try:
            info = self._mqtt.publish(
                self._topic(kind, msg_type), json.dumps(self.build_message(msg_type, action, data))
            )
            return info.rc == 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] Anycubic publish failed: %s", self.serial_number, exc)
            return False

    def _query(self, msg_type: str, action: str = "query") -> bool:
        return self._publish("web", msg_type, action, None)

    # -- PrinterClient control surface --------------------------------------
    def request_status_update(self) -> bool:
        return self._query("info")

    def start_print_data(self, filename: str, ams_mapping: list[int] | None) -> dict:
        data: dict = {"taskid": "-1", "filename": filename, "filetype": 1}
        entries = ams_box_mapping(ams_mapping or [], ace_units(self._ace)) if self.has_ace else []
        if entries:
            data["ams_settings"] = {"use_ams": True, "ams_box_mapping": entries}
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
        return self._publish("slicer", "print", "start", self.start_print_data(filename, ams_mapping))

    def stop_print(self) -> bool:
        return self._publish("web", "print", "stop", {"taskid": "-1"})

    def pause_print(self) -> bool:
        return self._publish("web", "print", "pause", {"taskid": "-1"})

    def resume_print(self) -> bool:
        return self._publish("web", "print", "resume", {"taskid": "-1"})

    def send_gcode(self, gcode: str) -> bool:
        return False

    def set_chamber_light(self, on: bool) -> bool:
        return self._publish(
            "web", "light", "control", {"type": LIGHT_CHAMBER, "status": 1 if on else 0, "brightness": 100 if on else 0}
        )

    def set_nozzle_temperature(self, target: int, nozzle: int = 0) -> bool:
        return self._publish(
            "web", "print", "update", {"taskid": "-1", "settings": {"target_nozzle_temp": int(target)}}
        )

    def set_bed_temperature(self, target: int) -> bool:
        return self._publish(
            "web", "print", "update", {"taskid": "-1", "settings": {"target_hotbed_temp": int(target)}}
        )
