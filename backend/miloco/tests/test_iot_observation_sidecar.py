import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from miloco.config.settings import IoTObservationSettings
from miloco.iot_observation.models import (
    ObservationAttribute,
    ObservationChange,
    ObservationGap,
)
from miloco.iot_observation.repository import ObservationRepository
from miloco.iot_observation.sidecar import ObservationSidecar, Submission


@pytest.fixture
def sidecar(tmp_path):
    repo = ObservationRepository(tmp_path / "observe.db").open()
    yield ObservationSidecar(repo, IoTObservationSettings())
    repo.close()


def record(sidecar, day=0, *, family="home", healthy=True, scope=1, replay=False):
    start = int(
        (
            datetime(2026, 11, 1, 21, tzinfo=ZoneInfo("Asia/Shanghai"))
            + timedelta(days=day)
        ).timestamp()
        * 1000
    )
    attributes = [
        ObservationAttribute(name, f"iot/device/{name}/prop/2.1", service, "on", "v1")
        for name, service in [("tv", "television"), ("lamp", "light")]
    ]
    for index, attribute in enumerate(attributes):
        sidecar.repository.record_change(
            family,
            ObservationChange(
                attribute, False, True, "cloud", start + index * 1000, scope
            ),
            replay=replay,
        )
        if healthy:
            sidecar.repository.record_coverage(
                family,
                attribute=attribute,
                scope=scope,
                start_ms=start - 1000,
                end_ms=start + 5000,
                status="healthy",
                replay=replay,
            )
    return start + 6000


def submission(batch, *, observation_id=None, kind="support"):
    return Submission.model_validate(
        {
            "batch_id": batch["batch_id"],
            "observations": [
                {
                    "observation_id": observation_id,
                    "description": "电视开启后落地灯开启",
                    "attributes": sorted(batch["attributes"]),
                    "episodes": [
                        {
                            "event_ids": [event["id"] for event in batch["events"]],
                            "kind": kind,
                        }
                    ],
                    "uncertainty": "操作来源未知",
                }
            ],
        }
    )


def test_batch_is_bounded_stable_and_only_commit_advances(sidecar):
    now = record(sidecar)
    batch = sidecar.batch("home", scope=1, now_ms=now)
    assert len(batch["events"]) == 2
    assert "maturity" not in json.dumps(batch)
    assert sidecar.batch("home", scope=1, now_ms=now + 1) == batch
    assert sidecar.repository.last_success_id("home", source="observe") is None
    request = Submission(batch_id=batch["batch_id"], observations=[])
    receipt = sidecar.submit("home", request, scope=1, now_ms=now)
    assert sidecar.submit("home", request, scope=1, now_ms=now) == receipt
    assert sidecar.batch("home", scope=1, now_ms=now)["status"] == "empty"


def test_late_arrivals_and_pagination_do_not_skip(sidecar):
    sidecar.settings.batch_events = 2
    now = record(sidecar, day=1)
    record(sidecar, day=0)
    batch = sidecar.batch("home", scope=1, now_ms=now)
    assert batch["has_more"]
    sidecar.submit(
        "home",
        Submission(batch_id=batch["batch_id"], observations=[]),
        scope=1,
        now_ms=now,
    )
    next_batch = sidecar.batch("home", scope=1, now_ms=now)
    assert next_batch["events"][0]["at_ms"] < batch["events"][0]["at_ms"]
    assert len(next_batch["context"]) == 2
    assert (
        len(json.dumps(next_batch, ensure_ascii=False, separators=(",", ":")).encode())
        <= 12288
    )


def test_three_and_seven_days_are_server_derived_not_agent_targets(sidecar):
    observation_id = None
    for day in range(7):
        now = record(sidecar, day)
        batch = sidecar.batch("home", scope=1, now_ms=now)
        assert not {"maturity", "support_days", "reviewable"}.intersection(
            json.dumps(batch).split()
        )
        receipt = sidecar.submit(
            "home",
            submission(batch, observation_id=observation_id),
            scope=1,
            now_ms=now,
        )
        observation_id = receipt["observation_ids"][0]
        state = sidecar.state("home", observation_id)
        assert state["support_days"] == day + 1
        with sidecar.repository.transaction() as conn:
            stored = conn.execute(
                "SELECT qualification FROM observe_commonality WHERE id=?",
                (observation_id,),
            ).fetchone()[0]
        assert json.loads(stored) == state
        assert state["maturity"] == (
            "reviewable" if day == 6 else "accumulated" if day >= 2 else "collecting"
        )
    assert "ready_to_promote" not in receipt
    assert state["action"] == "none"


def test_same_day_and_overlapping_evidence_do_not_inflate_days(sidecar):
    now = record(sidecar)
    batch = sidecar.batch("home", scope=1, now_ms=now)
    request = submission(batch)
    request.observations[0].episodes *= 3
    result = sidecar.submit("home", request, scope=1, now_ms=now)
    assert sidecar.state("home", result["observation_ids"][0])["support_days"] == 1
    assert sidecar.submit("home", request, scope=1, now_ms=now) == result


def test_unknown_coverage_and_difference_do_not_mature(sidecar):
    observation_id = None
    for day in range(8):
        now = record(sidecar, day, healthy=day != 0)
        batch = sidecar.batch("home", scope=1, now_ms=now)
        result = sidecar.submit(
            "home",
            submission(
                batch,
                observation_id=observation_id,
                kind="difference" if day == 7 else "support",
            ),
            scope=1,
            now_ms=now,
        )
        observation_id = result["observation_ids"][0]
    state = sidecar.state("home", observation_id)
    assert state["support_days"] == 6
    assert state["unknown_days"] == 1
    assert state["difference_days"] == 1
    assert state["maturity"] == "contested"


def test_gap_overrides_healthy_coverage(sidecar):
    now = record(sidecar)
    sidecar.repository.record_gap("home", ObservationGap(1, "loss", now - 5500))
    batch = sidecar.batch("home", scope=1, now_ms=now)
    receipt = sidecar.submit("home", submission(batch), scope=1, now_ms=now)
    assert sidecar.state("home", receipt["observation_ids"][0])["support_days"] == 0


def test_invalid_evidence_rolls_back_whole_batch(sidecar):
    now = record(sidecar)
    batch = sidecar.batch("home", scope=1, now_ms=now)
    request = submission(batch)
    invalid = request.observations[0].model_copy(deep=True)
    invalid.episodes[0].event_ids = [999999]
    request.observations.append(invalid)
    with pytest.raises(ValueError, match="evidence"):
        sidecar.submit("home", request, scope=1, now_ms=now)
    assert sidecar.repository.last_success_id("home", source="observe") is None
    assert sidecar.batch("home", scope=1, now_ms=now) == batch


def test_scope_family_replay_and_expiry_are_isolated(sidecar):
    now = record(sidecar)
    record(sidecar, family="other")
    record(sidecar, replay=True)
    batch = sidecar.batch("home", scope=1, now_ms=now)
    assert len(batch["events"]) == 2
    request = submission(batch)
    for family, scope, at_ms in [
        ("other", 1, now),
        ("home", 2, now),
        ("home", 1, now + 86400001),
    ]:
        with pytest.raises(ValueError):
            sidecar.submit(family, request, scope=scope, now_ms=at_ms)


def test_no_counters_or_confidence_accepted(sidecar):
    from pydantic import ValidationError

    now = record(sidecar)
    payload = submission(sidecar.batch("home", scope=1, now_ms=now)).model_dump()
    payload["observations"][0]["confidence"] = 1.0
    with pytest.raises(ValidationError):
        Submission.model_validate(payload)


def test_pruned_evidence_cannot_be_submitted(sidecar):
    now = record(sidecar)
    batch = sidecar.batch("home", scope=1, now_ms=now)
    sidecar.repository.prune(before_ms=now)
    with pytest.raises(ValueError, match="evidence"):
        sidecar.submit("home", submission(batch), scope=1, now_ms=now)


def test_definition_change_cannot_inherit_evidence(sidecar):
    now = record(sidecar)
    batch = sidecar.batch("home", scope=1, now_ms=now)
    receipt = sidecar.submit("home", submission(batch), scope=1, now_ms=now)
    now = record(sidecar, day=1)
    batch = sidecar.batch("home", scope=1, now_ms=now)
    request = submission(batch, observation_id=receipt["observation_ids"][0])
    request.observations[0].description = "晚上落地灯开启"
    with pytest.raises(ValueError, match="immutable"):
        sidecar.submit("home", request, scope=1, now_ms=now)
    request.observations[0].observation_id = None
    new = sidecar.submit("home", request, scope=1, now_ms=now)
    assert new["observation_ids"] != receipt["observation_ids"]
    assert sidecar.state("home", new["observation_ids"][0])["support_days"] == 1


def test_batch_budget_preserves_unread_events(sidecar):
    sidecar.settings.batch_bytes = 2048
    for day in range(5):
        now = record(sidecar, day)
    read = []
    while True:
        batch = sidecar.batch("home", scope=1, now_ms=now)
        if batch["status"] == "empty":
            break
        assert (
            len(json.dumps(batch, ensure_ascii=False, separators=(",", ":")).encode())
            <= 2048
        )
        read.extend(event["id"] for event in batch["events"])
        sidecar.submit(
            "home",
            Submission(batch_id=batch["batch_id"], observations=[]),
            scope=1,
            now_ms=now,
        )
    assert read == list(range(1, 11))


def test_late_gap_revokes_qualified_evidence(sidecar):
    now = record(sidecar)
    batch = sidecar.batch("home", scope=1, now_ms=now)
    result = sidecar.submit("home", submission(batch), scope=1, now_ms=now)
    observation_id = result["observation_ids"][0]
    assert sidecar.state("home", observation_id)["support_days"] == 1
    sidecar.repository.record_gap("home", ObservationGap(1, "late_loss", now - 5500))
    assert sidecar.state("home", observation_id)["support_days"] == 0


def test_capacity_rejection_is_atomic(sidecar):
    sidecar.settings.max_evidence = 1
    now = record(sidecar)
    batch = sidecar.batch("home", scope=1, now_ms=now)
    receipt = sidecar.submit("home", submission(batch), scope=1, now_ms=now)
    old_cursor = sidecar.repository.last_success_id("home", source="observe")
    now = record(sidecar, day=1)
    batch = sidecar.batch("home", scope=1, now_ms=now)
    with pytest.raises(ValueError, match="capacity"):
        sidecar.submit(
            "home",
            submission(batch, observation_id=receipt["observation_ids"][0]),
            scope=1,
            now_ms=now,
        )
    assert sidecar.repository.last_success_id("home", source="observe") == old_cursor
    assert sidecar.state("home", receipt["observation_ids"][0])["support_days"] == 1


def test_schema_v2_migrates_without_losing_raw_events(tmp_path):
    path = tmp_path / "migration.db"
    repo = ObservationRepository(path).open()
    service = ObservationSidecar(repo, IoTObservationSettings())
    now = record(service)
    with repo.transaction() as conn:
        for table in ("observe_batch", "observe_evidence", "observe_commonality"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("UPDATE observation_meta SET value='2' WHERE key='schema_version'")
    repo.close()
    repo.open()
    try:
        assert len(service.batch("home", scope=1, now_ms=now)["events"]) == 2
    finally:
        repo.close()


def test_forget_account_removes_only_its_ledger(sidecar):
    family = '["account-a","home"]'
    other = '["account-b","home"]'
    for identity in (family, other):
        now = record(sidecar, family=identity)
        batch = sidecar.batch(identity, scope=1, now_ms=now)
        sidecar.submit(identity, submission(batch), scope=1, now_ms=now)
    sidecar.repository.forget_account("account-a")
    with sidecar.repository.transaction() as conn:
        for table in (
            "observe_batch",
            "observe_commonality",
            "observe_evidence",
            "observation_cursor",
        ):
            assert not conn.execute(
                f"SELECT 1 FROM {table} WHERE family_id=?", (family,)
            ).fetchone()
            assert conn.execute(
                f"SELECT 1 FROM {table} WHERE family_id=?", (other,)
            ).fetchone()


def test_restart_retains_unacknowledged_packet_and_ledger(sidecar):
    now = record(sidecar)
    batch = sidecar.batch("home", scope=1, now_ms=now)
    sidecar.repository.close()
    sidecar.repository.open()
    assert sidecar.batch("home", scope=1, now_ms=now) == batch
    result = sidecar.submit("home", submission(batch), scope=1, now_ms=now)
    sidecar.repository.close()
    sidecar.repository.open()
    assert sidecar.state("home", result["observation_ids"][0])["support_days"] == 1


def test_concurrent_submissions_share_one_commit(sidecar):
    from concurrent.futures import ThreadPoolExecutor

    now = record(sidecar)
    request = submission(sidecar.batch("home", scope=1, now_ms=now))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: sidecar.submit("home", request, scope=1, now_ms=now), range(2)
            )
        )
    assert results[0] == results[1]
    assert sidecar.state("home", results[0]["observation_ids"][0])["support_days"] == 1


def test_unknown_and_cross_scope_intervals_never_qualify(sidecar):
    now = record(sidecar)
    with sidecar.repository.transaction() as conn:
        conn.execute("UPDATE observation_change SET scope=2 WHERE did='lamp'")
    batch = sidecar.batch("home", scope=2, now_ms=now)
    result = sidecar.submit("home", submission(batch), scope=2, now_ms=now)
    assert sidecar.state("home", result["observation_ids"][0])["support_days"] == 0


def test_local_dates_not_utc_dates_drive_accumulation(sidecar):
    now = record(sidecar)
    with sidecar.repository.transaction() as conn:
        conn.execute(
            "UPDATE observation_coverage SET start_ms=start_ms-7200000,end_ms=end_ms+7200000"
        )
        conn.execute("UPDATE observation_change SET at_ms=at_ms+7200000")
    batch = sidecar.batch("home", scope=1, now_ms=now + 7200000)
    receipt = sidecar.submit("home", submission(batch), scope=1, now_ms=now + 7200000)
    now = record(sidecar, day=1)
    with sidecar.repository.transaction() as conn:
        conn.execute("UPDATE observation_change SET at_ms=at_ms-72000000 WHERE id>2")
        conn.execute(
            "UPDATE observation_coverage SET start_ms=start_ms-72000000 WHERE start_ms>?",
            (now - 10000,),
        )
    batch = sidecar.batch("home", scope=1, now_ms=now)
    sidecar.submit(
        "home",
        submission(batch, observation_id=receipt["observation_ids"][0]),
        scope=1,
        now_ms=now,
    )
    assert sidecar.state("home", receipt["observation_ids"][0])["support_days"] == 2


def test_packet_includes_current_device_context_only_for_matching_family(sidecar):
    now = record(sidecar)
    sidecar.catalog = (
        "home",
        {
            ("tv", "iot/device/tv/prop/2.1", "v1"): {
                "device_name": "客厅电视",
                "room": "客厅",
            }
        },
    )
    batch = sidecar.batch("home", scope=1, now_ms=now)
    television = next(
        attribute
        for attribute in batch["attributes"].values()
        if attribute["did"] == "tv"
    )
    assert television["current_labels"]["device_name"] == "客厅电视"
    record(sidecar, family="other")
    other = sidecar.batch("other", scope=1, now_ms=now)
    assert all(
        "current_labels" not in attribute for attribute in other["attributes"].values()
    )


def test_disk_budget_refusal_does_not_acknowledge(sidecar):
    now = record(sidecar)
    batch = sidecar.batch("home", scope=1, now_ms=now)
    previous = sidecar.repository.hard_bytes
    sidecar.repository.hard_bytes = 1
    try:
        with pytest.raises(RuntimeError, match="disk budget"):
            sidecar.submit("home", submission(batch), scope=1, now_ms=now)
    finally:
        sidecar.repository.hard_bytes = previous
    assert sidecar.repository.last_success_id("home", source="observe") is None
    assert sidecar.batch("home", scope=1, now_ms=now) == batch


def test_evidence_ids_are_strict_and_attribute_keys_bounded(sidecar):
    from pydantic import ValidationError

    now = record(sidecar)
    payload = submission(sidecar.batch("home", scope=1, now_ms=now)).model_dump()
    for event_ids in ([True], ["1"], [-1]):
        payload["observations"][0]["episodes"][0]["event_ids"] = event_ids
        with pytest.raises(ValidationError):
            Submission.model_validate(payload)
    payload["observations"][0]["episodes"][0]["event_ids"] = [1, 2]
    payload["observations"][0]["attributes"] = ["a" * 1000]
    with pytest.raises(ValidationError):
        Submission.model_validate(payload)
