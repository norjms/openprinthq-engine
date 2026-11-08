# Elegoo payloads

Test fixtures for the Elegoo SDCP and Centauri Carbon 2 clients. OpenPrintHQ
owns no Elegoo printer; every payload is quoted from published sources.

| File | Source | Licence |
|------|--------|---------|
| `cc2_canvas_status.json` | "Real-world example from CC2 printer with Canvas" (method 2005) in `docs/CC2_PROTOCOL.md` of github.com/danielcherubini/elegoo-homeassistant | MIT |
| `cc2_full_status.json` | "Full Status Response" (method 6000) in the same document | MIT |
| `cc2_delta_status.json` | "Delta Update Example" in the same document | MIT |
| `sdcp_status_printing.json` | `Status` frame layout from the SDCP v3.0.0 specification (github.com/cbd-tech/SDCP-Smart-Device-Control-Protocol-V3.0.0) with the FDM fields (`TempOfNozzle`, `TempOfHotbed`) and values from elegoo-homeassistant `sdcp/tests/test_status.py` | spec: published by the vendor; tests: MIT |
| `sdcp_canvas_response.json` | Cmd 324 response: `sdcp/response` framing from the SDCP spec, `canvas_info` fields from Elegoo's elegoo-link SDK (`handleCanvasStatus`, Apache-2.0) and the Centauri Carbon 1 placeholders (`"?"`, dashes) from elegoo-homeassistant `websocket/tests/test_canvas_handler.py` | Apache-2.0 / MIT |
