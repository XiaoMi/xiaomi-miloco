import json
import time
from datetime import datetime, timezone

from miloco.config.settings import IoTObservationSettings
from miloco.iot_observation.models import ObservationAttribute, ObservationChange
from miloco.iot_observation.repository import ObservationRepository
from miloco.iot_observation.sidecar import ObservationSidecar


def test_full_event_capacity_remains_bounded(tmp_path):
    repo = ObservationRepository(tmp_path / "capacity.sqlite3").open()
    now = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
    start_ms = int(now.timestamp() * 1000) - 27 * 86400000
    attributes = [
        ObservationAttribute(f"device-{index}", f"iot/device/device-{index}/prop/2.1", "light", "on", "v1")
        for index in range(128)
    ]
    started = time.perf_counter()
    try:
        for batch in range(0, 200001, 256):
            with repo.transaction():
                for index in range(batch, min(batch + 256, 200001)):
                    value = bool(index // len(attributes) % 2)
                    repo.record_change("home", ObservationChange(
                        attributes[index % len(attributes)], not value, value,
                        "iot_push", start_ms + index * 10000, 1,
                    ))
        write_seconds = time.perf_counter() - started
        assert repo.change_count() == 200000
        assert repo.attribute_count() == 128
        disk_bytes = repo._size_bytes()
        assert disk_bytes < repo.hard_bytes
        started = time.perf_counter()
        result = ObservationSidecar(repo, IoTObservationSettings()).batch("home", scope=1, now_ms=int(now.timestamp() * 1000))
        batch_seconds = time.perf_counter() - started
        assert result["has_more"]
        assert result["lost_before_id"] == 1
        assert len(result["events"]) <= 48
        assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()) <= 12288
        assert repo.last_success_id("home", source="observe") is None
        print(f"capacity: events=200000 attributes=128 disk_bytes={disk_bytes} write_seconds={write_seconds:.3f} batch_seconds={batch_seconds:.3f}")
    finally:
        repo.close()
