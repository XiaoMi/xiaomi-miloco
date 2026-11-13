from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta

from miloco.iot_observation.catalog_policy import load_catalog_policy
from miloco.iot_observation.collector import ObservationCollector
from miloco.iot_observation.models import ObservationAttribute
from miloco.iot_observation.repository import ObservationRepository
from miloco.iot_observation.sidecar import ObservationSidecar

logger = logging.getLogger(__name__)


def monitored_specs(info, policy):
    result = {}
    for device in info.get("devices", []):
        did = device.get("did")
        if not isinstance(did, str) or not did or "/" in did:
            continue
        for iid, entry in (device.get("spec") or {}).items():
            if not isinstance(iid, str) or not iid.startswith("prop."):
                continue
            parts = iid.split(".")
            if len(parts) != 3 or not all(part.isdigit() for part in parts[1:]):
                continue
            if isinstance(entry, dict) and policy.is_monitored(entry):
                result[f"iot/device/{did}/prop/{parts[1]}.{parts[2]}"] = entry
    return result


class CoverageTracker:
    def __init__(self):
        self._previous = None

    def advance(self, at_ms, monotonic_ms, token, attributes):
        previous = self._previous
        self._previous = at_ms, monotonic_ms, token, frozenset(attributes)
        if previous is None:
            return []
        start, elapsed_start, old_token, old_attributes = previous
        elapsed = monotonic_ms - elapsed_start
        if old_token != token or not 0 < elapsed <= 120000 or abs(at_ms - start - elapsed) > 1000:
            return []
        return [(attribute, start, at_ms) for attribute in old_attributes & attributes]


class ObservationService:
    def __init__(self, manager, settings, path):
        self.manager = manager
        self.settings = settings
        self.policy = load_catalog_policy()
        self.specs = {}
        self.family_id = ""
        self.repository = ObservationRepository(
            path, max_changes=settings.max_changes, max_coverage=settings.max_coverage,
            max_attributes=settings.max_attributes, hard_bytes=settings.hard_bytes,
            soft_bytes=settings.soft_bytes,
        )
        self.sidecar = ObservationSidecar(self.repository, settings)
        self.collector = ObservationCollector(
            manager.state_store, self.repository, self.policy,
            family_id=lambda: self.family_id, scope_getter=manager.current_scope,
            aligned_getter=manager.scope_is_aligned, spec_getter=self.specs.get,
            baseline_getter=self._baseline, queue_limit=settings.queue_size,
            health_token_getter=self._loss_token,
        )
        self.coverage = CoverageTracker()
        self._wake = asyncio.Event()
        self._task = None
        self._unsubscribe = None

    def _active_identity(self):
        family = self.manager.observation_family_id()
        if (not self.settings.enabled or not family or not self.manager.scope_is_aligned()
                or family != self.family_id or not self.collector.accepting):
            return None
        return family, self.manager.current_scope()

    def read_batch(self):
        identity = self._active_identity()
        if identity is None:
            return {"status": "disabled"}
        with self.repository.deadline(self.settings.timeout_seconds):
            result = self.sidecar.batch(identity[0], scope=identity[1])
        if identity != self._active_identity():
            raise ValueError("observation scope changed")
        return result

    def submit_batch(self, request):
        identity = self._active_identity()
        if identity is None:
            return {"status": "disabled"}
        with self.repository.deadline(self.settings.timeout_seconds):
            result = self.sidecar.submit(identity[0], request, scope=identity[1])
        if identity != self._active_identity():
            raise ValueError("observation scope changed")
        return result

    async def start(self):
        await asyncio.to_thread(self.repository.open)
        self._unsubscribe = self.manager.state_store.subscribe(
            "iot/device/*/status/online", lambda _: self.collector.mark_gap("online_transition"),
        )
        self._task = asyncio.create_task(self._maintain())

    def scope_switched(self):
        self.collector.switch_scope()
        self.specs.clear()
        self.coverage = CoverageTracker()

    def aligned(self):
        self._wake.set()

    async def stop(self):
        if self._unsubscribe is not None:
            self._unsubscribe()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self.collector.stop()
        await asyncio.to_thread(self.repository.close)

    def _baseline(self):
        store = self.manager.state_store
        return {path: store.get(path) for path in self.specs if store.get_entry(path) is not None}

    def _loss_token(self):
        stats = self.manager.state_store.stats()
        client = self.manager.miot_proxy.miot_client
        return (client.observation_generation, *(stats[key] for key in (
            "dropped", "discarded", "rejected_cascade", "rejected_leaf_limit",
        )))

    async def _refresh(self):
        scope = self.manager.current_scope()
        family = self.manager.observation_family_id()
        if not self.manager.scope_is_aligned() or family is None:
            return False
        policy = load_catalog_policy()
        info = await self.manager.miot_service.get_home_info()
        specs = monitored_specs(info, policy)
        if scope != self.manager.current_scope() or family != self.manager.observation_family_id():
            return False
        if len(specs) > self.settings.max_attributes:
            raise RuntimeError("observation metadata capacity exceeded")
        devices = {device["did"]: device for device in info.get("devices", [])}
        labels = {}
        for path, spec in specs.items():
            attribute = ObservationAttribute.from_spec(did=path.split("/")[2], path=path, spec_entry=spec, policy=policy)
            device = devices.get(attribute.did, {})
            values = spec.get("value_list") or []
            labels[attribute.identity] = {
                "device_name": str(device.get("name") or "")[:128],
                "room": str(device.get("room") or "")[:128],
                "values": [{"value": item["value"], "description": str(item.get("description") or "")[:128]} for item in values[:16]],
                "values_truncated": len(values) > 16,
            }
        self.sidecar.catalog = (family, labels)
        if specs != self.specs or family != self.family_id or policy != self.policy or not self.collector.accepting:
            self.collector.mark_alignment_started()
            attributes = [ObservationAttribute.from_spec(
                did=path.split("/")[2], path=path, spec_entry=spec, policy=policy,
            ) for path, spec in specs.items()]
            await asyncio.to_thread(self.repository.set_active_attributes, family, attributes, at_ms=int(time.time() * 1000))
            if scope != self.manager.current_scope():
                return False
            self.family_id = family
            self.policy = policy
            self.specs.clear()
            self.specs.update(specs)
            self.collector.policy = policy
            self.collector.start()
            self.collector.mark_alignment_completed()
        return True

    async def _sample(self):
        await self.collector.queue.join()
        self.collector.check_health()
        client = self.manager.miot_proxy.miot_client
        generation, devices = client.observation_health()
        store = self.manager.state_store
        attributes = set()
        for path, spec in self.specs.items():
            attribute = ObservationAttribute.from_spec(did=path.split("/")[2], path=path, spec_entry=spec, policy=self.policy)
            if (attribute.identity in self.collector.known_identities
                    and attribute.did in devices and spec.get("notify") is True
                    and store.get(f"iot/device/{attribute.did}/status/online") is True
                    and store.stats()["pending"] == 0):
                attributes.add(attribute)
        token = (self.manager.current_scope(), self.family_id, generation,
                 self._loss_token(), self.collector.continuity)
        intervals = self.coverage.advance(int(time.time() * 1000), int(time.monotonic() * 1000), token, attributes)
        self.collector.submit_coverage(intervals)

    async def _maintain(self):
        while True:
            self._wake.clear()
            try:
                zone = self.sidecar.zone
                first_day = datetime.now(zone).date() - timedelta(days=self.settings.retention_days - 1)
                cutoff = int(datetime.combine(first_day, datetime.min.time(), zone).timestamp() * 1000)
                await asyncio.to_thread(self.repository.prune, before_ms=cutoff)
                if await self._refresh():
                    await self._sample()
            except Exception:
                self.collector.mark_alignment_started()
                self.coverage = CoverageTracker()
                logger.warning("event=iot_observation_health reason=collection_unavailable mode=observe_only")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=60)
            except TimeoutError:
                pass

    async def forget_account(self, account_id):
        self.scope_switched()
        await self.collector.queue.join()
        await asyncio.to_thread(self.repository.forget_account, account_id)


def family_identity(account_id, homes):
    if not account_id or len(homes) != 1:
        return None
    return json.dumps([str(account_id), next(iter(homes))], separators=(",", ":"))
