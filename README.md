# OpenPrintHQ Engine

The print engine behind the [OpenPrintHQ](https://github.com/norjms/openprinthq) farm.
It is the backend that talks to the printers: it connects to them, monitors them, queues and
dispatches jobs, tracks filament and raises alarms.

This repository is a fork of [Bambuddy](https://github.com/maziggy/bambuddy) by maziggy and the
Bambuddy contributors, licensed AGPL-3.0. OpenPrintHQ is not affiliated with or endorsed by the
Bambuddy project.

> **Looking for a standalone Bambu Lab manager?** Use [upstream Bambuddy](https://github.com/maziggy/bambuddy).
> This fork is built to run inside an OpenPrintHQ farm and is not supported as a standalone app.

## Where it fits

| Repository | Role |
|---|---|
| [openprinthq](https://github.com/norjms/openprinthq) | The hub: products, model library, inventory and storefronts |
| [openprinthq-farm](https://github.com/norjms/openprinthq-farm) | The farm, served under the hub at `/apps/farm` |
| **openprinthq-engine** (this repository) | The farm's print engine |
| [openprinthq-link](https://github.com/norjms/openprinthq-link) | Connects a hub to printers on other networks |

## Branches

| Branch | What it holds |
|---|---|
| `main` | The fork point: Bambuddy 1.2.5 plus the attribution notice |
| `feature/klipper-extended` | Active development. Pull requests target this branch, and the engine image is built from it. |

## What this fork adds to Bambuddy

**Printers beyond Bambu Lab**
- Klipper / Moonraker (extended), Snapmaker, PrusaLink, OctoPrint, Duet / RepRapFirmware, FlashForge and MKS
- Elegoo SDCP printers, including the Centauri Carbon 2
- Anycubic Kobra printers in LAN mode

**Multi-material units**
- Creality CFS, QIDI Box, Anycubic ACE Pro and Elegoo CANVAS slots, with slot-by-slot colour remapping

**Farm operation**
- Farm dispatch, with printer access scoped per user
- Filament matching: fill levels, colour difference, unmatched jobs and mappings
- Filament reconciliation against inventory
- Alarm feed and alarm page: dismiss, per-printer suppression, readable text for Bambu HMS codes, and reopening when a code returns
- Print failure detection and failure events
- Bed ejection profiles and plate hold hooks
- Farm analytics and an audit log
- Queue cancel reasons

**Cameras, power and notifications**
- WebRTC (WHEP) camera streams and snapshots from the farm's go2rtc
- Kasa smart plug control
- Web push notifications, and a chat agent for Discord and Slack

**Platform**
- OrcaSlicer slicing through a sidecar service
- A published OpenAPI specification
- Backend tests on PostgreSQL as well as SQLite
- OpenPrintHQ naming in user-facing text (API metadata, notification sender, MFA issuer)

Identifiers that form part of an external contract, such as MQTT topic prefixes, environment variable
names and the backup archive layout, keep their upstream names so the engine stays compatible.

## Container image

Built by the `engine-build` workflow on every push to `feature/klipper-extended`, for `linux/amd64`
and `linux/arm64`:

```
ghcr.io/norjms/openprinthq-engine:<tag>
```

| Tag | Meaning |
|---|---|
| `1.2.5` (and later versions) | The upstream Bambuddy version the build is based on |
| `sha-<commit>` | One exact build. Pin this in production. |
| `test` | The latest build from the development branch |

The OpenPrintHQ installer pulls and runs this image as part of a farm. For running from source,
the upstream instructions in [docs/UPSTREAM-README.md](docs/UPSTREAM-README.md) apply.

## Staying in step with upstream

Bambuddy releases are merged into this fork by pull request. Upstream authorship and commit history
are kept unchanged. Bugs that also occur in upstream Bambuddy are best reported
[there](https://github.com/maziggy/bambuddy/issues), so every user benefits.

## Contributing

Open issues and pull requests in this repository for engine changes specific to OpenPrintHQ.
Pull requests target `feature/klipper-extended`. See [CONTRIBUTING.md](CONTRIBUTING.md) for code
style and testing.

## License and attribution

AGPL-3.0, the same as upstream. The upstream license is kept verbatim in [LICENSE](LICENSE). See
[ATTRIBUTION-OpenPrintHQ.md](ATTRIBUTION-OpenPrintHQ.md) for the fork notice. Under AGPL-3.0 section 13,
anyone using a running engine over a network is entitled to its source, which is this repository.

The upstream project's README is kept at [docs/UPSTREAM-README.md](docs/UPSTREAM-README.md).

## Thanks

To maziggy and every Bambuddy contributor, whose work this engine is built on. If Bambuddy is useful
to you, consider [supporting the project](https://github.com/sponsors/maziggy).
