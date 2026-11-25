"""MQTT client ids are unique to this process, not only to its process id.

In a container the process id is 1, so two Bambuddy-based farms watching one
printer named their first client the same ("bambuddy_<serial>_1_1"). A printer
drops a session when another connects with its id, so they took the printer
from each other every few seconds and it showed offline.
"""

from unittest.mock import MagicMock, patch

from backend.app.services.bambu_mqtt import BambuMQTTClient


def _ids(tag: str, pid: int) -> list[str]:
    made = []

    def fake_client(*a, **kw):
        made.append(kw["client_id"])
        return MagicMock()

    with (
        patch("backend.app.services.bambu_mqtt.mqtt.Client", side_effect=fake_client),
        patch("backend.app.services.bambu_mqtt.os.getpid", return_value=pid),
        patch.object(BambuMQTTClient, "_client_tag", tag),
        patch.object(BambuMQTTClient, "_client_instance_counter", 0),
    ):
        c = BambuMQTTClient(ip_address="192.0.2.4", serial_number="0948AD5C0204357", access_code="12345678")
        try:
            c.connect()
        except Exception:  # noqa: BLE001 - only the id handed to paho matters here
            pass
    return made


def test_two_farms_in_containers_get_different_ids_for_the_same_printer():
    a = _ids("1a2b3c4d", 1)
    b = _ids("9f8e7d6c", 1)
    assert a and b
    assert a[0] != b[0]
    assert a[0].startswith("bambuddy_0948AD5C0204357_1a2b3c4d_1_")


def test_the_tag_is_made_once_per_process():
    assert len(BambuMQTTClient._client_tag) == 8
    assert BambuMQTTClient._client_tag == BambuMQTTClient._client_tag
