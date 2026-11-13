from __future__ import annotations

import asyncio

from miloco.iot_observation.collector import OBSERVATION_PATTERN, ObservationCollector
from miloco.iot_observation.policy import ObservationPolicy
from miloco.iot_observation.repository import ObservationRepository
from miloco.state.types import MISSING, Change


class FakeStore:
    def __init__(self) -> None:
        self.pattern = None
        self.callback = None

    def subscribe(self, pattern, callback):
        self.pattern = pattern
        self.callback = callback
        return lambda: setattr(self, "callback", None)


def make_spec() -> dict:
    return {
        "service_type_name": "light",
        "type_name": "on",
        "kind": "prop",
        "writeable": True,
        "format": "bool",
    }


def test_collector_uses_state_store_pattern_and_preserves_fast_changes(tmp_path) -> None:
    async def scenario() -> None:
        store = FakeStore()
        repo = ObservationRepository(tmp_path / "db.sqlite3").open()
        scope = {"value": 1, "aligned": True}
        collector = ObservationCollector(
            store,
            repo,
            ObservationPolicy("v1", frozenset({("light", "prop", "on")})),
            family_id="home-1",
            scope_getter=lambda: scope["value"],
            aligned_getter=lambda: scope["aligned"],
            spec_getter=lambda _path: make_spec(),
        )
        collector.start()
        assert store.pattern == OBSERVATION_PATTERN
        store.callback(Change("iot/device/did-1/prop/2.1", MISSING, False, "iot_push", 1000))
        store.callback(Change("iot/device/did-1/prop/2.1", False, True, "iot_push", 1100))
        store.callback(Change("iot/device/did-1/prop/2.1", True, False, "iot_push", 1200))
        await collector.queue.join()
        rows = repo.recent_changes("home-1", since_ms=0, until_ms=2000)
        assert [(row.old, row.new) for row in rows] == [(False, True), (True, False)]
        await collector.stop()
        repo.close()

    asyncio.run(scenario())


def test_overflow_invalidates_baseline_outside_full_queue(tmp_path):
    async def scenario():
        store = FakeStore()
        repo = ObservationRepository(tmp_path / "db.sqlite3").open()
        collector = ObservationCollector(
            store, repo, ObservationPolicy("v1", frozenset({("light", "prop", "on")})),
            family_id="home-1", scope_getter=lambda: 1, aligned_getter=lambda: True,
            spec_getter=lambda _: make_spec(), queue_limit=1,
        )
        collector.start()
        path = "iot/device/did-1/prop/2.1"
        store.callback(Change(path, MISSING, False, "iot_align", 1000))
        await collector.queue.join()
        store.callback(Change(path, False, True, "iot_push", 1100))
        store.callback(Change(path, True, False, "iot_push", 1200))
        assert collector.dropped_count == 1
        await collector.queue.join()
        store.callback(Change(path, False, True, "iot_push", 1300))
        await collector.queue.join()
        assert repo.change_count("home-1") == 0
        store.callback(Change(path, True, False, "iot_push", 1400))
        await collector.queue.join()
        assert repo.change_count("home-1") == 1
        await collector.stop()
        repo.close()

    asyncio.run(scenario())


def test_collector_drops_events_from_old_scope(tmp_path) -> None:
    async def scenario() -> None:
        store = FakeStore()
        repo = ObservationRepository(tmp_path / "db.sqlite3").open()
        scope = {"value": 1, "aligned": True}
        collector = ObservationCollector(
            store,
            repo,
            ObservationPolicy("v1", frozenset({("light", "prop", "on")})),
            family_id="home-1",
            scope_getter=lambda: scope["value"],
            aligned_getter=lambda: scope["aligned"],
            spec_getter=lambda _path: make_spec(),
        )
        collector.start()
        old_callback = store.callback
        old_callback(Change("iot/device/did-1/prop/2.1", MISSING, False, "iot_push", 900))
        await collector.queue.join()
        collector.switch_scope()
        scope["value"] = 2
        collector.mark_alignment_completed()
        old_callback(Change("iot/device/did-1/prop/2.1", False, True, "iot_push", 1000))
        old_callback(Change("iot/device/did-1/prop/2.1", True, False, "iot_push", 1100))
        await collector.queue.join()
        assert repo.change_count("home-1") == 0
        await collector.stop()
        repo.close()

    asyncio.run(scenario())


def test_unknown_source_only_repairs_baseline(tmp_path):
    async def scenario():
        store = FakeStore()
        repo = ObservationRepository(tmp_path / "db.sqlite3").open()
        collector = ObservationCollector(
            store, repo, ObservationPolicy("v1", frozenset({("light", "prop", "on")})),
            family_id="home-1", scope_getter=lambda: 1,
            aligned_getter=lambda: True, spec_getter=lambda _: make_spec(),
        )
        collector.start()
        for old, new, source, at in [
            (MISSING, False, "iot_align", 1000),
            (False, True, "unknown", 1100),
            (True, False, "iot_push", 1200),
        ]:
            store.callback(Change("iot/device/did-1/prop/2.1", old, new, source, at))
        await collector.queue.join()
        rows = repo.recent_changes("home-1", since_ms=0, until_ms=2000)
        assert [(row.old, row.new) for row in rows] == [(True, False)]
        await collector.stop()
        repo.close()

    asyncio.run(scenario())
