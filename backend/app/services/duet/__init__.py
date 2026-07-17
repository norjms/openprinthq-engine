"""Duet / RepRapFirmware printer support (Duet Web Control transport).

Controls Duet-board printers over HTTP: RRF standalone (``rr_*`` endpoints on
the Duet board) and DSF (Duet on an SBC, ``/machine/*`` endpoints). Both expose
the same RRF object model for status, so the mapping is shared and only the
transport verbs differ (auto-detected on connect).

See ``ORCA_PRINTER_SUPPORT_PLAN.md`` (Chunk 2).
"""
