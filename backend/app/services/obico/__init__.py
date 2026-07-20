"""Obico cloud-relay support (upload-only — see module docstrings for why).

Obico is self-hosted (each user points at their own server + API key) and its
slicer-facing API (``src/slic3r/Utils/Obico.cpp`` in OrcaSlicer) is
upload-only: ``POST {host}/api/v1/g_code_files/``. Live status for the
printer already lives on the Obico server itself, fed by a companion
(moonraker-obico / OctoPrint plugin) running on the printer's own host — not
by anything the slicer (or Bambuddy) can poll. So unlike every other
connection type, Obico printers in Bambuddy have no live monitoring: adding
one lets you dispatch a queued print to Obico, nothing more.

See ``ORCA_PRINTER_SUPPORT_PLAN.md`` (Chunk 5).
"""
