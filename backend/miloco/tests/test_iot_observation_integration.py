from miloco.iot_observation.models import ObservationAttribute


def test_spec_iids_are_mapped_to_state_paths_without_display_limits():
    from miloco.iot_observation.integration import monitored_specs
    from miloco.iot_observation.policy import ObservationPolicy

    entry = {"writeable": True, "format": "bool", "service_type_name": "light", "type_name": "on"}
    info = {"devices": [{"did": "light", "spec": {f"prop.2.{number}": entry for number in range(1, 21)}}]}
    specs = monitored_specs(info, ObservationPolicy("v1", frozenset({("light", "prop", "on")})))
    assert len(specs) == 20
    assert "iot/device/light/prop/2.1" in specs


def test_coverage_requires_continuous_proof_and_never_bridges_restart_or_loss():
    from miloco.iot_observation.integration import CoverageTracker

    attribute = ObservationAttribute("light", "iot/device/light/prop/2.1", "light", "on", "v1")
    tracker = CoverageTracker()
    assert tracker.advance(1000, 1000, (1, 0), {attribute}) == []
    assert tracker.advance(61000, 61000, (1, 0), {attribute}) == [(attribute, 1000, 61000)]
    assert tracker.advance(121000, 121000, (1, 1), {attribute}) == []
    assert tracker.advance(181000, 181000, (1, 1), set()) == []
    assert tracker.advance(241000, 241000, (1, 1), {attribute}) == []
    assert tracker.advance(1000000, 301000, (1, 1), {attribute}) == []


def test_service_sidecar_rejects_unaligned_and_changed_scope(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock

    import pytest
    from miloco.config.settings import IoTObservationSettings
    from miloco.iot_observation.integration import ObservationService

    manager = SimpleNamespace(
        state_store=Mock(), current_scope=Mock(return_value=1), scope_is_aligned=Mock(return_value=False),
        observation_family_id=Mock(return_value="home"),
    )
    service = ObservationService(manager, IoTObservationSettings(enabled=True), tmp_path / "db")
    service.repository.open()
    try:
        assert service.read_batch() == {"status": "disabled"}
        manager.scope_is_aligned.return_value = True
        assert service.read_batch() == {"status": "disabled"}
        service.family_id = "home"
        service.collector._accepting = True
        service.sidecar.batch = Mock(return_value={"status": "empty"})
        assert service.read_batch() == {"status": "empty"}
        manager.current_scope.side_effect = [1, 2]
        with pytest.raises(ValueError, match="scope changed"):
            service.read_batch()
    finally:
        service.repository.close()
