"""Snapmaker (2.0 / Artisan / J1) LAN HTTP transport.

Added by OpenPrintHQ (2026-07-25), AGPL-3.0. Networked Snapmaker printers expose
an HTTP API on :8080 with token-based auth (the touchscreen authorizes the
controller once, via POST /api/v1/connect). Upload g-code, start/pause/resume/
stop, set temps, and poll live status into the shared PrinterState.
"""
