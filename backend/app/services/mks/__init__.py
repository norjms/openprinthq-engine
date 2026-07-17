"""MKS WiFi module support (Marlin-based boards with the MKS TFT WiFi add-on).

Upload is HTTP (``POST /upload?X-Filename=``, port 80); control/status is a
plain-text line-oriented TCP console on port 8080 (no auth, ``\\n``-terminated,
replies read until a bare ``ok`` line) — this is OrcaSlicer's ``MKS`` host
type (``src/slic3r/Utils/MKS.cpp`` + ``TCPConsole.cpp``).

See ``ORCA_PRINTER_SUPPORT_PLAN.md`` (Chunk 4).
"""
