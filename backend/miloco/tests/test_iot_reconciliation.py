from __future__ import annotations

import asyncio

import pytest
from miloco.rule.iot_source import IotRef, IotSource
from miloco.state import StateStore


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_reconcile_rechecks_state_store_without_change_callback():
    store = StateStore()
    store.start()
    store.set("iot/device/d1/status/online", True, source="test")
    store.set("iot/device/d1/prop/5.1", False, source="test")

    refs = [IotRef("r1", "d1", "5.1", "eq", True)]
    fed: list[tuple[str, bool]] = []
    unknown: list[tuple[str, str]] = []
    source = IotSource(
        store=store,
        feed=lambda rule_id, value: _feed(fed, rule_id, value),
        mark_unknown=lambda rule_id, did: unknown.append((rule_id, did)),
        iot_refs=lambda: refs,
        ref_of_rule=lambda rule_id: next(
            (ref for ref in refs if ref.rule_id == rule_id), None
        ),
        reconcile_interval=60,
    )
    source.start()
    await _settle()
    fed.clear()

    source._unsubscribes[0]()
    store.set("iot/device/d1/prop/5.1", True, source="test")
    source._reconcile_once()
    await _settle()

    assert fed == [("r1", True)]
    assert unknown == []
    assert source.diagnostics()["reconcile_count"] == 1

    await source.stop()
    store.stop()


async def _feed(fed: list[tuple[str, bool]], rule_id: str, value: bool) -> None:
    fed.append((rule_id, value))
