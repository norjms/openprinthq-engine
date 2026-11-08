"""Anycubic LAN-mode handshake: broker credentials and the client certificate.

Added by OpenPrintHQ, AGPL-3.0. Untested on hardware.

The printer (Kobra 3 / S1 generation with LAN mode switched on in its
settings) answers ``GET http://<ip>:18910/info`` with a token, its
``ctrlInfoUrl`` and ``modelId``. A signed ``POST`` to that URL returns an
AES-CBC encrypted blob with the local broker URL (``mqtts://<ip>:9883``), the
MQTT user name and password, the device id used in topics and a client
certificate and key for mutual TLS.

    sign = md5(md5(token[:16]) + ts + nonce)
    key  = token[16:32], iv = the reply's token (first 16 bytes, zero padded)

The algorithm is the one documented and implemented by kobra-connect
(github.com/rvanderp3/kobra-connect, Apache-2.0) and anycubic_ha_local
(github.com/chrisfore/anycubic_ha_local, AGPL-3.0), which validated it on a
Kobra S1 Max (firmware 2.6.9.6). Nothing here is stored: the credentials live
in memory for the session and the certificate in a private temporary
directory that is removed on disconnect.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import re
import string
import time
import urllib.parse
from dataclasses import dataclass

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

INFO_PORT = 18910


class AnycubicHandshakeError(Exception):
    pass


class AnycubicCloudModeError(AnycubicHandshakeError):
    """The printer is in cloud mode; LAN mode must be switched on."""


@dataclass(frozen=True)
class LanCredentials:
    broker_host: str
    broker_port: int
    username: str
    password: str = ""
    device_id: str = ""
    model_id: str = ""
    serial: str = ""
    device_cert: str = ""
    device_key: str = ""
    model_name: str = ""

    def __repr__(self) -> str:  # never print the secrets
        return f"LanCredentials(broker={self.broker_host}:{self.broker_port}, model_id={self.model_id})"


def sign(token: str, ts: int, nonce: str) -> str:
    first = hashlib.md5(token[:16].encode()).hexdigest()  # noqa: S324 - the printer's scheme
    return hashlib.md5((first + str(ts) + nonce).encode()).hexdigest()  # noqa: S324


def decrypt_ctrl(info_b64: str, token: str, reply_token: str) -> dict:
    key = token[16:32].encode()
    iv = reply_token.encode()[:16].ljust(16, b"\0")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(base64.b64decode(info_b64)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return json.loads((unpadder.update(padded) + unpadder.finalize()).decode())


def credentials_from(info: dict, ctrl_plain: dict) -> LanCredentials:
    m = re.match(r"mqtts?://([^:/]+):(\d+)", str(ctrl_plain.get("broker") or ""))
    if not m:
        raise AnycubicHandshakeError("handshake reply has no broker URL")
    return LanCredentials(
        broker_host=m.group(1),
        broker_port=int(m.group(2)),
        username=str(ctrl_plain.get("username") or ""),
        password=str(ctrl_plain.get("password") or ""),
        device_id=str(ctrl_plain.get("deviceId") or ""),
        model_id=str(info.get("modelId") or ""),
        serial=str(info.get("cn") or ""),
        device_cert=str(ctrl_plain.get("devicecrt") or ""),
        device_key=str(ctrl_plain.get("devicepk") or ""),
        model_name=str(info.get("modelName") or ""),
    )


async def handshake(host: str, timeout: float = 10.0) -> LanCredentials:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(f"http://{host}:{INFO_PORT}/info")
        resp.raise_for_status()
        info = resp.json()
        if info.get("ctrlType") == "cloud":
            raise AnycubicCloudModeError("the printer is in cloud mode; switch LAN mode on at the printer")
        token, ctrl_url = info.get("token"), info.get("ctrlInfoUrl")
        if not token or not ctrl_url or not info.get("modelId"):
            raise AnycubicHandshakeError("the printer does not offer the signed LAN handshake (Kobra 3 / S1 needed)")
        ts = int(time.time() * 1000)
        nonce = "".join(random.choices(string.ascii_letters + string.digits, k=6))  # noqa: S311
        did = "".join(random.choices(string.ascii_uppercase + string.digits, k=32))  # noqa: S311
        query = urllib.parse.urlencode({"ts": ts, "nonce": nonce, "sign": sign(token, ts, nonce), "did": did})
        resp = await client.post(f"{ctrl_url}?{query}", headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        ctrl = resp.json()
    if ctrl.get("code") != 200:
        raise AnycubicHandshakeError(f"handshake refused: {ctrl.get('message')}")
    data = ctrl.get("data") or {}
    return credentials_from(info, decrypt_ctrl(data["info"], token, data["token"]))
