"""Common printer-client interface.

Historically ``PrinterManager`` held a concrete ``BambuMQTTClient`` per printer.
To support Klipper/Moonraker printers alongside Bambu ones, the manager now
holds a ``PrinterClient`` — the protocol below — and instantiates the right
implementation based on ``Printer.connection_type``.

This is the *common* surface only: connection lifecycle, live ``state``, and
the basic control + print actions every printer supports. Bambu-only
capabilities (AMS, drying, k-profiles, calibration, airduct, skip-objects, …)
are NOT part of this protocol; callers reach those through the concrete
``BambuMQTTClient`` and must gate on ``printer_capabilities`` so they are never
invoked on a non-Bambu client.

``BambuMQTTClient`` already satisfies this protocol structurally; no changes to
it are required. The Klipper ``MoonrakerClient`` (Phase 2) implements the same
surface, mapping Moonraker state into the shared ``PrinterState``.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from backend.app.services.bambu_mqtt import PrinterState


@runtime_checkable
class PrinterClient(Protocol):
    """The minimal interface PrinterManager and generic callers rely on.

    Implementations: ``BambuMQTTClient`` (MQTT), ``MoonrakerClient`` (Klipper).
    """

    #: Live printer state. Implementations keep this updated from their
    #: transport and fill at least the transport-agnostic core fields
    #: (connected, state, progress, remaining_time, layer_num, total_layers,
    #: temperatures, current_print/gcode_file). Bambu-only fields stay at their
    #: defaults for non-Bambu printers.
    state: PrinterState

    def connect(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Open the connection (non-blocking; state updates arrive async)."""
        ...

    def disconnect(self, timeout: float = 0) -> None:
        """Close the connection and stop background tasks."""
        ...

    def check_staleness(self) -> bool:
        """Re-evaluate liveness; return current connected state."""
        ...

    def request_status_update(self) -> bool:
        """Ask the printer to push a full status snapshot."""
        ...

    # --- control -----------------------------------------------------------
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
        """Start a job. The file must already be uploaded to the printer.

        Bambu-specific arguments (plate_id, ams_mapping, *_cali, use_ams) are
        accepted for signature compatibility; non-Bambu implementations ignore
        the ones that don't apply.
        """
        ...

    def stop_print(self) -> bool: ...

    def pause_print(self) -> bool: ...

    def resume_print(self) -> bool: ...

    def set_chamber_light(self, on: bool) -> bool: ...

    def send_gcode(self, gcode: str) -> bool: ...
