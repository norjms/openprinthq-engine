"""Shared guards for printer-type-specific routes.

Bambu-only features (AMS, k-profiles, drying, calibration, …) must fail cleanly
on external printers (Klipper/OctoPrint/PrusaLink) rather than 500 with an
AttributeError. The frontend already hides these controls for external
printers; these guards are defense-in-depth at the API boundary.
"""

from __future__ import annotations

from fastapi import HTTPException

from backend.app.services.printer_capabilities import is_external


def reject_klipper_feature(printer, feature: str) -> None:
    """Raise 400 for any non-Bambu printer. ``printer`` is the ORM row.

    (Named for history; blocks the feature on every external transport.)
    """
    if is_external(printer):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "unsupported_for_printer_type",
                "message": f"{feature} is not supported for this printer type.",
            },
        )
