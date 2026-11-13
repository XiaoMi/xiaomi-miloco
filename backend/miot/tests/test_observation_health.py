from types import SimpleNamespace

from miot.client import MIoTClient
from miot.action_result import SOURCE_CLOUD, SOURCE_LOCAL_CENTRAL


def test_subscription_intent_is_not_health_and_reconnect_invalidates_proof():
    client = object.__new__(MIoTClient)
    client._mips_cloud = SimpleNamespace(is_connected=True)
    client._props_sub_sources = {"light": SOURCE_CLOUD, "hub": SOURCE_LOCAL_CENTRAL}
    client._callback_mips_connect = None
    epoch, ready = client.observation_health()
    assert not ready
    client._on_mips_subscribe_success("device/light/up/properties_changed/#")
    assert not client.observation_health()[1]
    client._on_mips_subscribe_success("device/light/state/#")
    assert client.observation_health()[1] == frozenset({"light"})
    client._on_mips_connect(False)
    assert client.observation_health()[0] > epoch
    assert not client.observation_health()[1]
    client._on_mips_connect(True)
    assert not client.observation_health()[1]


def test_property_subscription_failure_removes_proof():
    client = object.__new__(MIoTClient)
    client._mips_cloud = SimpleNamespace(is_connected=True)
    client._props_sub_sources = {"light": SOURCE_CLOUD}
    client._on_mips_subscribe_success("device/light/up/properties_changed/#")
    client._on_mips_subscribe_success("device/light/state/#")
    assert client.observation_health()[1]
    client._on_mips_subscribe_error(("device/light/up/properties_changed/#", 135, "rejected"))
    assert not client.observation_health()[1]
