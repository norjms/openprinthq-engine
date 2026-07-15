"""Shared guards for printer-type-specific routes.

Bambu-only features (AMS, k-profiles, drying, calibration, …) must fail cleanly
on Klipper/Moonraker printers rather than 500 with an AttributeError. The
frontend already hides these controls for Klipper printers; these guards are
defense-in-depth at the API boundary.
"""

from __future__ import annotations

from fastapi import HTTPException

from backend.app.services.printer_capabilities import is_klipper


def reject_klipper_feature(printer, feature: str) -> None:
    """Raise 400 if ``printer`` is a Klipper printer. ``printer`` is the ORM row."""
    if is_klipper(printer):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "unsupported_for_printer_type",
                "message": f"{feature} is not supported for this printer type.",
            },
        )
