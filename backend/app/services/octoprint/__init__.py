"""OctoPrint / PrusaLink printer support (OctoPrint REST transport).

Controls printers exposed through OctoPrint's REST API (and PrusaLink, which is
OctoPrint-compatible) — upload g-code, start/pause/resume/cancel, set temps,
home, and poll live status into the shared ``PrinterState``.

See ``ORCA_PRINTER_SUPPORT_PLAN.md`` (Chunk 1) for the design.
"""
