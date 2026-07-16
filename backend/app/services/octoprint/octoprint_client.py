"""OctoPrint REST client — the OctoPrint/PrusaLink equivalent of the Moonraker
and Bambu clients.

OctoPrint has no status push we rely on here; we poll ``/api/printer`` +
``/api/job`` on an interval and map into the shared ``PrinterState``. Control
actions are synchronous wrappers (like the other clients) that fire-and-forget
an HTTP request on the client's event loop. Satisfies ``PrinterClient``.

``PrusaLinkClient`` (below) subclasses this for PrusaLink's ``/api/v1`` shape.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import httpx

from backend.app.services.bambu_mqtt import PrinterState
from backend.app.services.octoprint.status_map import apply_octoprint_status

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 3.0
_RECONNECT_BACKOFF_SECONDS = (2, 5, 10, 15)
# When a print ends, OctoPrint returns to Operational (IDLE) with no explicit
# success/fail. Treat >= this completion as a successful finish, else cancelled.
_COMPLETE_THRESHOLD = 99.0

# Bambu-style state vocabulary used for transition detection.
_ACTIVE_STATES = frozenset({"RUNNING", "PAUSE"})


class OctoPrintClient:
    flavor = "octoprint"

    def __init__(
        self,
        ip_address: str,
        port: int = 80,
        api_key: str | None = None,
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
        self.api_key = api_key or None
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

        self._prev_state: str | None = None
        self._prev_bed_temp: float | None = None

    # -- urls / http --------------------------------------------------------
    @property
    def base_url(self) -> str:
        return f"{self._scheme}://{self.ip_address}:{self.port}"

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.api_key} if self.api_key else {}

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

    # -- poll loop ----------------------------------------------------------
    async def _run_loop(self) -> None:
        attempt = 0
        while not self._stop:
            try:
                async with httpx.AsyncClient(
                    timeout=10.0, headers=self._headers(), verify=False
                ) as client:
                    await self._poll_once(client)  # initial — raises if unreachable
                    self.state.connected = True
                    attempt = 0
                    self._emit_state_change()
                    while not self._stop:
                        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                        await self._poll_once(client)
                        self._emit_state_change()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 — log + reconnect
                logger.warning("[%s] OctoPrint poll error: %s", self.serial_number, exc)
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

    async def _fetch_status(self, client: httpx.AsyncClient) -> tuple[dict, dict]:
        """Return (printer_json, job_json). Overridden by PrusaLink."""
        # /api/printer 409s when the printer is disconnected from OctoPrint but
        # the OctoPrint server itself is up — treat as connected-but-idle.
        pr = await client.get(f"{self.base_url}/api/printer")
        printer_json: dict = {}
        if pr.status_code == 200:
            printer_json = pr.json()
        elif pr.status_code == 409:
            printer_json = {"state": {"flags": {"operational": False}}}
        else:
            pr.raise_for_status()
        job = await client.get(f"{self.base_url}/api/job")
        job_json = job.json() if job.status_code == 200 else {}
        return printer_json, job_json

    async def _poll_once(self, client: httpx.AsyncClient) -> None:
        printer_json, job_json = await self._fetch_status(client)
        mapped = apply_octoprint_status(self.state, printer_json, job_json)
        self._detect_transitions(mapped)
        self._signal_bed()

    # -- transitions / callbacks -------------------------------------------
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
                # No explicit success flag — infer from last progress.
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
    def _post(self, path: str, json: dict | None = None) -> bool:
        if not self._loop:
            return False

        async def _do() -> None:
            try:
                async with httpx.AsyncClient(timeout=10.0, headers=self._headers(), verify=False) as client:
                    resp = await client.post(f"{self.base_url}{path}", json=json)
                    if resp.status_code >= 400:
                        logger.warning("[%s] OctoPrint POST %s -> %s", self.serial_number, path, resp.status_code)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] OctoPrint POST %s failed: %s", self.serial_number, path, exc)

        try:
            asyncio.run_coroutine_threadsafe(_do(), self._loop)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[%s] OctoPrint schedule %s failed: %s", self.serial_number, path, exc)
            return False

    # -- PrinterClient control surface --------------------------------------
    def request_status_update(self) -> bool:
        return True  # polling refreshes on its own interval

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
        # File must already be uploaded (see octoprint_files.upload_gcode).
        return self._post(f"/api/files/local/{filename}", {"command": "select", "print": True})

    def stop_print(self) -> bool:
        return self._post("/api/job", {"command": "cancel"})

    def pause_print(self) -> bool:
        return self._post("/api/job", {"command": "pause", "action": "pause"})

    def resume_print(self) -> bool:
        return self._post("/api/job", {"command": "pause", "action": "resume"})

    def send_gcode(self, gcode: str) -> bool:
        cmds = [c for c in gcode.split("\n") if c.strip()]
        return self._post("/api/printer/command", {"commands": cmds})

    def set_chamber_light(self, on: bool) -> bool:
        return False  # not modelled for OctoPrint

    # -- convenience controls ----------------------------------------------
    def set_nozzle_temp(self, temp: float) -> bool:
        return self._post("/api/printer/tool", {"command": "target", "targets": {"tool0": int(temp)}})

    def set_bed_temp(self, temp: float) -> bool:
        return self._post("/api/printer/bed", {"command": "target", "target": int(temp)})

    def home(self, axes: list[str] | None = None) -> bool:
        return self._post("/api/printer/printhead", {"command": "home", "axes": axes or ["x", "y", "z"]})


class PrusaLinkClient(OctoPrintClient):
    """PrusaLink (Prusa MK4/MINI/XL/MK3.9). OctoPrint-compatible for uploads and
    core control, but live status is richer via ``/api/v1/status``. Auth is the
    printer's API key (Settings → Network → PrusaLink) via ``X-Api-Key``."""

    flavor = "prusalink"

    async def _fetch_status(self, client: httpx.AsyncClient) -> tuple[dict, dict]:
        # Translate PrusaLink /api/v1/status into the OctoPrint shape our mapper
        # expects, so the same status_map applies.
        r = await client.get(f"{self.base_url}/api/v1/status")
        r.raise_for_status()
        s = r.json() or {}
        pr = s.get("printer") or {}
        job = s.get("job") or {}
        state_text = str(pr.get("state") or "").upper()
        flags = {
            "operational": state_text in ("IDLE", "READY", "FINISHED", "STOPPED"),
            "printing": state_text == "PRINTING",
            "paused": state_text == "PAUSED",
            "error": state_text in ("ERROR", "ATTENTION"),
            "ready": state_text in ("IDLE", "READY"),
        }
        printer_json = {
            "state": {"flags": flags},
            "temperature": {
                "tool0": {"actual": pr.get("temp_nozzle"), "target": pr.get("target_nozzle")},
                "bed": {"actual": pr.get("temp_bed"), "target": pr.get("target_bed")},
            },
        }
        job_json = {
            "job": {"file": {"name": (job.get("file") or {}).get("display_name") or (job.get("file") or {}).get("name")}},
            "progress": {
                "completion": job.get("progress"),
                "printTimeLeft": job.get("time_remaining"),
                "printTime": job.get("time_printing"),
            },
        }
        return printer_json, job_json
