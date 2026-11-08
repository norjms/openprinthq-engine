"""TP-Link Kasa smart plugs over the local network (python-kasa).

Kasa plugs speak one of two local protocols: the original XOR-obfuscated JSON on
TCP 9999, or KLAP (newer firmware on HS100/HS103/HS110/KP115/KP125/EP25 and
others), which needs the TP-Link account email and password. python-kasa
detects which one a plug uses (``Discover.discover_single``) and handles both.
The plug's ``username`` / ``password`` fields hold those account credentials
when a KLAP plug needs them; legacy plugs ignore them.

The engine must reach the plug directly on the LAN: discovery is UDP (ports
9999 and 20002) and control is TCP 9999 or HTTP 80/4433, none of which the
connector's HTTP plug relay (``OPHQ_LAN_PROXY``) carries.

Multi-outlet strips (HS300, KP303) switch as one device here; per-outlet control
is not modelled.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.app.models.smart_plug import SmartPlug

logger = logging.getLogger(__name__)


def _valid_ip(ip: str | None) -> bool:
    try:
        addr = ipaddress.ip_address(ip or "")
    except ValueError:
        return False
    return not addr.is_loopback and not addr.is_link_local and not addr.is_multicast


class KasaService:
    def __init__(self, timeout: float = 6.0):
        self.timeout = timeout
        # ip -> python-kasa DeviceConfig dict, so a known plug skips discovery next time.
        self._configs: dict[str, dict] = {}

    async def _device(self, ip: str, username: str | None, password: str | None):
        """A connected, updated python-kasa Device, or None when the plug cannot be reached."""
        if not _valid_ip(ip):
            logger.warning("Blocked Kasa request to invalid IP: %s", ip)
            return None
        from kasa import Credentials, Device, DeviceConfig, Discover

        creds = Credentials(username, password) if username and password else None
        dev = None
        cached = self._configs.get(ip)
        if cached:
            try:
                config = DeviceConfig.from_dict(cached)
                if creds is not None:
                    config.credentials = creds
                dev = await asyncio.wait_for(Device.connect(config=config), self.timeout)
            except Exception as e:  # noqa: BLE001 - fall back to discovery
                logger.debug("Kasa %s: cached connection failed (%s), rediscovering", ip, e)
                self._configs.pop(ip, None)
                dev = None
        if dev is None:
            try:
                dev = await Discover.discover_single(
                    ip, credentials=creds, discovery_timeout=int(self.timeout), timeout=int(self.timeout)
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("Kasa plug at %s not reachable: %s", ip, e)
                return None
            if dev is None:
                logger.warning("No Kasa plug answered discovery at %s", ip)
                return None
        try:
            await asyncio.wait_for(dev.update(), self.timeout)
        except Exception as e:  # noqa: BLE001
            logger.warning("Kasa plug at %s did not answer: %s", ip, e)
            await self._close(dev)
            return None
        try:
            self._configs[ip] = dev.config.to_dict()
        except Exception:  # noqa: BLE001 - caching is only an optimisation
            pass
        return dev

    @staticmethod
    async def _close(dev) -> None:
        try:
            await dev.disconnect()
        except Exception:  # noqa: BLE001
            pass

    async def get_status(self, plug: SmartPlug) -> dict:
        dev = await self._device(plug.ip_address, plug.username, plug.password)
        if dev is None:
            return {"state": None, "reachable": False, "device_name": None}
        try:
            return {"state": "ON" if dev.is_on else "OFF", "reachable": True, "device_name": dev.alias}
        finally:
            await self._close(dev)

    async def _switch(self, plug: SmartPlug, on: bool | None) -> bool:
        dev = await self._device(plug.ip_address, plug.username, plug.password)
        if dev is None:
            return False
        try:
            target = (not dev.is_on) if on is None else on
            await asyncio.wait_for(dev.turn_on() if target else dev.turn_off(), self.timeout)
            await asyncio.wait_for(dev.update(), self.timeout)
            ok = dev.is_on == target
            log = logger.info if ok else logger.warning
            log(
                "Kasa plug '%s' at %s %s",
                plug.name,
                plug.ip_address,
                ("on" if target else "off") if ok else "did not switch",
            )
            return ok
        except Exception as e:  # noqa: BLE001
            logger.warning("Kasa plug '%s' at %s: switch failed: %s", plug.name, plug.ip_address, e)
            return False
        finally:
            await self._close(dev)

    async def turn_on(self, plug: SmartPlug) -> bool:
        return await self._switch(plug, True)

    async def turn_off(self, plug: SmartPlug) -> bool:
        return await self._switch(plug, False)

    async def toggle(self, plug: SmartPlug) -> bool:
        return await self._switch(plug, None)

    async def get_energy(self, plug: SmartPlug) -> dict | None:
        """Same shape as the Tasmota service; None when the plug has no energy meter (HS100, HS103, KP100)."""
        from kasa import Module

        dev = await self._device(plug.ip_address, plug.username, plug.password)
        if dev is None:
            return None
        try:
            energy = dev.modules.get(Module.Energy)
            if energy is None:
                return None
            return {
                "power": energy.current_consumption,
                "voltage": getattr(energy, "voltage", None),
                "current": getattr(energy, "current", None),
                "today": energy.consumption_today,
                "yesterday": None,
                "total": energy.consumption_total,
                "factor": None,
                "apparent_power": None,
                "reactive_power": None,
            }
        except Exception as e:  # noqa: BLE001
            logger.debug("Kasa plug at %s: energy read failed: %s", plug.ip_address, e)
            return None
        finally:
            await self._close(dev)

    async def test_connection(self, ip: str, username: str | None = None, password: str | None = None) -> dict:
        dev = await self._device(ip, username, password)
        if dev is None:
            return {
                "success": False,
                "state": None,
                "device_name": None,
                "error": "Could not connect to the Kasa plug (if it is on newer firmware, add the TP-Link account email and password)",
            }
        try:
            return {
                "success": True,
                "state": "ON" if dev.is_on else "OFF",
                "device_name": dev.alias,
                "model": getattr(dev, "model", None),
                "has_energy": _has_energy(dev),
                "error": None,
            }
        finally:
            await self._close(dev)


def _has_energy(dev) -> bool:
    try:
        from kasa import Module

        return dev.modules.get(Module.Energy) is not None
    except Exception:  # noqa: BLE001
        return False


kasa_service = KasaService()
