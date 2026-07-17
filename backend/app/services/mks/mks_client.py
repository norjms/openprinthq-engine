"""MKS WiFi module client — plain-text TCP console (:8080, Marlin g-code).

Mirrors OrcaSlicer's ``TCPConsole``: one command per line, no prefix, no
auth, replies are read until a line that lowercases to ``ok``. Every
command/response pair is serialized through ``_io_lock`` since the wire has
no request framing to demultiplex. Satisfies ``PrinterClient``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.mks.status_map import apply_progress, apply_temps

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 3.0
_RECONNECT_BACKOFF_SECONDS = (2, 5, 10, 15)
_COMMAND_TIMEOUT_SECONDS = 5.0
_ACTIVE_STATES = frozenset({"RUNNING", "PAUSE"})
_COMPLETE_THRESHOLD = 99.0


class MKSClient:
    def __init__(
        self,
        ip_address: str,
        port: int = 8080,
        api_key: str | None = None,  # unused — no auth on this transport
        serial_number: str | None = None,
        use_https: bool = False,  # unused — raw TCP, no TLS variant
        on_state_change: Callable[[PrinterState], None] | None = None,
        on_print_start: Callable[[dict], None] | None = None,
        on_print_complete: Callable[[dict], None] | None = None,
        on_print_running_observed: Callable[[dict], None] | None = None,
        on_layer_change: Callable[[int], None] | None = None,
        on_bed_temp_update: Callable[[float], None] | None = None,
    ):
        self.ip_address = ip_address
        self.port = port
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

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._io_lock: asyncio.Lock | None = None

        self._prev_state: str | None = None
        self._prev_bed_temp: float | None = None

    # -- lifecycle ----------------------------------------------------------
    def connect(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop or asyncio.get_event_loop()
        self._io_lock = asyncio.Lock()
        self._stop = False
        self._task = self._loop.create_task(self._run_loop())

    def disconnect(self, timeout: float = 0) -> None:
        self._stop = True
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self._close_connection()
        self.state.connected = False

    def check_staleness(self) -> bool:
        return self.state.connected

    def _close_connection(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass
        self._reader = None
        self._writer = None

    # -- wire protocol --------------------------------------------------------
    async def _send_raw(self, command: str) -> str:
        """Send ``<command>\\n`` on the open connection and read the reply.

        Must be called while holding ``_io_lock``. Raises on I/O failure.
        """
        if not self._reader or not self._writer:
            raise ConnectionError("not connected")
        self._writer.write(f"{command}\n".encode())
        await self._writer.drain()

        async def _read_until_ok() -> list[str]:
            lines: list[str] = []
            while True:
                raw = await self._reader.readline()
                if not raw:
                    raise ConnectionError("connection closed by peer")
                line = raw.decode(errors="replace").strip()
                if line:
                    lines.append(line)
                if line.lower() == "ok":
                    return lines

        lines = await asyncio.wait_for(_read_until_ok(), timeout=_COMMAND_TIMEOUT_SECONDS)
        return "\n".join(lines)

    async def _send_command(self, command: str) -> str:
        assert self._io_lock is not None
        async with self._io_lock:
            return await self._send_raw(command)

    # -- poll loop ----------------------------------------------------------
    async def _run_loop(self) -> None:
        attempt = 0
        while not self._stop:
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.ip_address, self.port), timeout=10.0
                )
                self.state.connected = True
                attempt = 0
                await self._poll_once()
                self._emit_state_change()
                while not self._stop:
                    await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                    await self._poll_once()
                    self._emit_state_change()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] MKS poll error: %s", self.serial_number, exc)
                if self.state.connected:
                    self.state.connected = False
                    self.state.state = "unknown"
                    self._emit_state_change()
            finally:
                self._close_connection()
            if self._stop:
                break
            delay = _RECONNECT_BACKOFF_SECONDS[min(attempt, len(_RECONNECT_BACKOFF_SECONDS) - 1)]
            attempt += 1
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break

    async def _poll_once(self) -> None:
        temp_reply = await self._send_command("M105")
        apply_temps(self.state, temp_reply)
        progress_reply = await self._send_command("M27")
        mapped = apply_progress(self.state, progress_reply)
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

    # -- outbound commands (fire-and-forget) --------------------------------
    def _command(self, cmd: str) -> bool:
        if not self._loop:
            return False

        async def _do() -> None:
            try:
                await self._send_command(cmd)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] MKS command %r failed: %s", self.serial_number, cmd, exc)

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
        # File must already be uploaded (see mks_files.upload_gcode). OrcaSlicer's
        # MKS host sends "M23 <name>" then "M24" (select + resume/start) as a pair.
        ok = self._command(f"M23 {filename}")
        return self._command("M24") and ok

    def stop_print(self) -> bool:
        return self._command("M524")  # Marlin: abort SD print

    def pause_print(self) -> bool:
        return self._command("M25")

    def resume_print(self) -> bool:
        return self._command("M24")

    def send_gcode(self, gcode: str) -> bool:
        ok = True
        for line in gcode.split("\n"):
            line = line.strip()
            if line:
                ok = self._command(line) and ok
        return ok

    def set_chamber_light(self, on: bool) -> bool:
        return False

    def set_nozzle_temp(self, temp: float) -> bool:
        return self._command(f"M104 S{int(temp)}")

    def set_bed_temp(self, temp: float) -> bool:
        return self._command(f"M140 S{int(temp)}")

    def home(self, axes: list[str] | None = None) -> bool:
        return self._command("G28")
