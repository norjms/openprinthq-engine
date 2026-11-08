"""TP-Link Kasa plugs: the service against a fake python-kasa device, and the API wiring."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.services import kasa_plug
from backend.app.services.kasa_plug import KasaService


class FakeEnergy:
    current_consumption = 87.5
    consumption_today = 0.42
    consumption_total = 12.3
    voltage = 120.1
    current = 0.73


class FakeDevice:
    def __init__(self, on=False, energy=True, alias="MK3S plug"):
        self.is_on = on
        self.alias = alias
        self.model = "KP115"
        self.modules = {"Energy": FakeEnergy()} if energy else {}
        self.config = SimpleNamespace(to_dict=lambda: {"host": "192.0.2.9"})
        self.calls = []
        self.fail_update = False

    async def update(self):
        if self.fail_update:
            raise OSError("no route")

    async def turn_on(self):
        self.calls.append("on")
        self.is_on = True

    async def turn_off(self):
        self.calls.append("off")
        self.is_on = False

    async def disconnect(self):
        self.calls.append("disconnect")


@pytest.fixture
def fake(monkeypatch):
    import kasa

    state = {"dev": FakeDevice(), "discovered": 0, "creds": None}

    async def discover_single(host, **kw):
        state["discovered"] += 1
        state["creds"] = kw.get("credentials")
        return state["dev"]

    async def connect(*, host=None, config=None):
        return state["dev"]

    monkeypatch.setattr(kasa.Discover, "discover_single", discover_single)
    monkeypatch.setattr(kasa.Device, "connect", connect)
    monkeypatch.setattr(kasa.DeviceConfig, "from_dict", classmethod(lambda cls, d: SimpleNamespace(credentials=None)))
    monkeypatch.setattr(kasa, "Module", SimpleNamespace(Energy="Energy"))
    return state


def plug(**kw):
    base = {"name": "MK3S", "ip_address": "192.0.2.9", "username": None, "password": None}
    base.update(kw)
    return SimpleNamespace(**base)


class TestService:
    async def test_status_and_switching(self, fake):
        s = KasaService()
        assert await s.get_status(plug()) == {"state": "OFF", "reachable": True, "device_name": "MK3S plug"}
        assert await s.turn_on(plug()) is True and fake["dev"].is_on is True
        assert await s.turn_off(plug()) is True and fake["dev"].is_on is False
        assert await s.toggle(plug()) is True and fake["dev"].is_on is True
        assert "disconnect" in fake["dev"].calls

    async def test_known_plug_skips_discovery(self, fake):
        s = KasaService()
        await s.get_status(plug())
        await s.get_status(plug())
        assert fake["discovered"] == 1

    async def test_energy(self, fake):
        e = await KasaService().get_energy(plug())
        assert e["power"] == 87.5 and e["today"] == 0.42 and e["total"] == 12.3 and e["voltage"] == 120.1

    async def test_no_meter(self, fake):
        fake["dev"] = FakeDevice(energy=False)
        assert await KasaService().get_energy(plug()) is None

    async def test_unreachable(self, fake):
        fake["dev"].fail_update = True
        s = KasaService()
        assert (await s.get_status(plug()))["reachable"] is False
        assert await s.turn_on(plug()) is False
        r = await s.test_connection("192.0.2.9")
        assert r["success"] is False and "TP-Link account" in r["error"]

    async def test_discovery_finds_nothing(self, fake):
        fake["dev"] = None
        assert (await KasaService().get_status(plug()))["reachable"] is False

    async def test_credentials_are_passed_for_klap(self, fake):
        await KasaService().get_status(plug(username="me@example.com", password="pw"))
        assert fake["creds"] is not None and fake["creds"].username == "me@example.com"

    async def test_bad_ips_are_refused(self, fake):
        s = KasaService()
        for ip in ("127.0.0.1", "169.254.1.1", "not-an-ip", None):
            assert (await s.get_status(plug(ip_address=ip)))["reachable"] is False
        assert fake["discovered"] == 0

    async def test_test_connection(self, fake):
        r = await KasaService().test_connection("192.0.2.9")
        assert r == {
            "success": True,
            "state": "OFF",
            "device_name": "MK3S plug",
            "model": "KP115",
            "has_energy": True,
            "error": None,
        }


class TestApi:
    async def test_create_and_switch_a_kasa_plug(self, async_client, fake, monkeypatch):
        monkeypatch.setattr(kasa_plug, "kasa_service", KasaService())
        from backend.app.api.routes import smart_plugs

        monkeypatch.setattr(smart_plugs, "kasa_service", kasa_plug.kasa_service)
        r = await async_client.post(
            "/api/v1/smart-plugs/", json={"name": "MK3S power", "plug_type": "kasa", "ip_address": "192.0.2.9"}
        )
        assert r.status_code in (200, 201), r.text
        pid = r.json()["id"]
        assert r.json()["plug_type"] == "kasa"
        r = await async_client.get(f"/api/v1/smart-plugs/{pid}/status")
        assert r.status_code == 200, r.text
        assert r.json()["reachable"] is True

    async def test_kasa_needs_an_ip(self, async_client):
        r = await async_client.post("/api/v1/smart-plugs/", json={"name": "x", "plug_type": "kasa"})
        assert r.status_code == 422

    async def test_test_endpoint(self, async_client, fake, monkeypatch):
        from backend.app.api.routes import smart_plugs

        monkeypatch.setattr(smart_plugs, "kasa_service", KasaService())
        r = await async_client.post("/api/v1/smart-plugs/test-kasa", json={"ip_address": "192.0.2.9"})
        assert r.status_code == 200, r.text
        assert r.json()["has_energy"] is True and r.json()["device_name"] == "MK3S plug"
