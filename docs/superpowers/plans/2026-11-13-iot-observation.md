# IoT Observation Implementation Plan

## Observe 旁路修订（当前执行版本）

**Goal:** Agent 只总结变动，代码累积验证；IoT 附属于家庭知识 Observe，隔离正式档案和巡检。

**Spec:** `docs/superpowers/specs/2026-11-13-iot-observation-design.md`。

以下历史计划和验证记录仅描述旧实现，固定时间窗/巡检 hook 不再是目标。
在现有 `feat/iot-observation` 工作区继续，不覆盖无关改动，不提交。

- [x] 新增失败测试：有界批次、ack 水位、证据去重/回滚、跨天/缺口/反例、家庭隔离。
- [x] 实现 `iot_observation/sidecar.py` 与 schema v3：事实快照、原子提交、后台资格。
- [x] 接入生命周期、鉴权 HTTP 与 CLI batch/submit；停用旧 run、删除 Analyzer/Runner。
- [x] 移除两宿主巡检 IoT hook（Hermes 保留原 noop/trace）；共享 Observe Skill 添加独立旁路，更新 README/config。
- [x] 验证 backend 观察与家庭知识回归、CLI、Hermes、OpenClaw 和格式检查。

验证命令：`cd backend && uv run pytest miloco/tests/test_iot_observation_*.py -q`；
`cd cli && uv run pytest tests/test_observation.py -q`；
`cd plugins/hermes && uv run --project ../../backend pytest tests -q`；
`cd plugins/openclaw && pnpm test && pnpm check`。

### 本次验证结果

- 后端观察、home_profile、StateStore、IoT/MIoT、scope/top-up/restart/reconfigure 相关回归：840 passed。
  单独 IoT 观察套件：51 passed；MIoT 健康接口：2 passed。
- CLI observation/commands/client/output：202 passed。
- Hermes：240 passed、2 skipped；OpenClaw：192 passed，TypeScript 检查通过。
- 新增观察代码 Ruff、关键新文件格式检查及 `git diff --check` 通过。
- 20 万条变化、128 属性合成数据：SQLite+WAL 37,806,080 bytes，写入 4.754 秒，
  有界事实批次读取 0.003 秒。与旧版本 4.982 秒全历史机械分析不是同一工作负载，
  该数字不包含模型推理，不代表最坏组合性能。
- 容量测试定位并修复淘汰水位聚合的全索引扫描；证据迟到缺口重新校验、
  过期/跨 scope/跨家庭拒绝、磁盘预算拒绝不 ack、schema v2 迁移、并发重试均有回归。
- 尚未验证：真实宿主/设备端到端，Agent 语义误总结/硬凑率和真实 Token 账单；
  4,096 属性与 100,000 覆盖区间的组合极限、真实磁盘写满、白名单单一构建源仍未验收。
- 默认仍关闭。每日 Observe 一批可能积压；只支持 bool/有限枚举，不包括连续亮度。
  旁路资格只供内部检查，不进入正式知识或设备执行。未提交，保留当前分支。

---

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement bounded, log-only observation of writable boolean/finite-enum IoT changes, with continuous `StateStore` collection, SQLite retention, deterministic 30-minute analysis, and identical OpenClaw/Hermes patrol integration.

**Architecture:** Add an independent `miloco.iot_observation` package split into policy, repository, collector, analyzer, and runner components. The collector subscribes to `iot/device/*/prop/*`, validates the active family/scope and catalog policy, and writes through a bounded asynchronous worker; the analyzer reads immutable repository snapshots and records only its own watermark/run metadata. `Manager` owns lifecycle wiring only, while patrol adapters invoke a timeout-bounded runner before the existing agent workflow continues.

**Tech Stack:** Python 3.11, `asyncio`, `sqlite3`, FastAPI internal endpoint, existing `StateStore`, pytest, TypeScript OpenClaw scheduler, Hermes Python cron/plugin.

**Spec:** Attachment `2026-11-13-iot-observation-design.md` supplied in this conversation.

## Verification Follow-up (2026-11-13)

- Prepared backend and CLI environments using `uv sync --all-packages --all-groups --locked`
  (CLI uses `uv sync --all-groups --locked`). `uv` is available at `/usr/bin/uv`;
  the earlier missing-tool diagnosis was incorrect.
- Fixed missing OpenClaw/Hermes observation hooks. Both registered patrol paths
  now invoke the internal CLI before inference, with bounded per-run deduplication,
  a 6-second process timeout, failure isolation, and no candidate injection.
  Prompts describe the hook instead of asking the Agent to invoke it again.
- Added adapter-generated correlation IDs through CLI, authenticated endpoint, and
  runner; endpoint validation rejects malformed/log-injecting IDs. Disabled
  responses also include a run ID. Added tests through actual plugin registration
  and managed cron definitions (host services and subprocesses are mocked).
- Updated old Manager test doubles for the new observation lifecycle fields and
  account-cleanup method. State/IoT/MIoT/top-up/restart/reconfigure regression:
  757 passed, including the catalog consistency assertion. MIoT health:
  2 passed. Hermes suite: 244 passed, 2 skipped. CLI command/client subset:
  194 passed. OpenClaw suite: 194 passed; TypeScript checking passed.
- Synthetic capacity run: 200,001 insertions retain 200,000 changes across 128
  attributes; SQLite plus WAL measured 37,822,464 bytes, insertion 4.461 seconds,
  analysis 4.982 seconds. This is close to the default budget, not a worst-case
  performance guarantee. The test does not impose a hardware-dependent timing
  assertion and uses no live device data.
- Remaining acceptance work: real host/device end-to-end execution; worst-case
  combined 4,096-attribute / 100,000-coverage performance and disk-pressure tests;
  consolidate catalog resource generation into a single build-time source
  (current packaged copies match, with version/hash regression protection).
- Do not treat this verification update as completion of every checklist item
  below. In particular, mocked scheduler tests do not establish live-host E2E.

## Global Constraints

- Use an independent `iot-observation.sqlite3` in the existing local workspace directory.
- Monitor only catalog-defined writable boolean/finite-enum properties; never fall back to all IoT properties.
- Collect `Change.old`, `Change.new`, `Change.source`, and `Change.at` directly; never replace event values by reading the current state.
- Treat first value, alignment, top-up, recovery, unknown old value, and deletion as baseline/gap handling, not user/device changes.
- Keep collection continuous and make patrol analysis the only 30-minute analysis trigger.
- Use 30-minute half-open windows with 15-minute steps, local dates, and separate weekday/weekend groups.
- Require at least 7 effective dates, 7-day first/last span, 70% hit rate, and 5 hit dates for long-term candidates; require 3 hit dates for `emerging`.
- Bound storage at 200,000 changes, 100,000 coverage intervals, 4,096 metadata/state entries, and a 128 MiB soft / 160 MiB hard budget.
- Log one summary, at most 10 change samples, and at most 20 candidate details per run; always report truncation.
- Observation failures must be isolated and must not affect state dispatch, rules, patrol actions, notifications, profiles, or habit suggestions.
- Do not add a user-facing history/query CLI, UI, debug notification, rule, recommendation, or device control.

## File Map

- Create `backend/miloco/src/miloco/iot_observation/__init__.py`: package exports.
- Create `backend/miloco/src/miloco/iot_observation/models.py`: immutable event, coverage, candidate, and run result types.
- Create `backend/miloco/src/miloco/iot_observation/policy.py`: shared policy loading and writable boolean/enum validation.
- Create `backend/miloco/src/miloco/iot_observation/repository.py`: isolated SQLite schema, short transactions, watermarks, retention, capacity, and snapshots.
- Create `backend/miloco/src/miloco/iot_observation/collector.py`: subscription callback, scope/epoch checks, bounded queue, worker, baseline and gap semantics.
- Create `backend/miloco/src/miloco/iot_observation/analyzer.py`: deterministic recent-change and 28-day window analysis.
- Create `backend/miloco/src/miloco/iot_observation/runner.py`: enable flag, per-family mutex, 5-second budget, structured summary logging, and failure isolation.
- Create `backend/miloco/src/miloco/iot_observation/router.py`: authenticated internal patrol execution endpoint returning only `run_id/status/reason`.
- Modify `backend/miloco/src/miloco/manager.py`: construct/start/stop observation lifecycle and pass current scope/family callbacks only.
- Modify `backend/miloco/src/miloco/main.py`: initialize/close the observation service with the application lifecycle and include its router.
- Modify `backend/miloco/src/miloco/config/settings.py` and `settings.yaml`: default-off observation settings and bounded thresholds.
- Modify the smallest MIoT catalog/spec boundary: expose stable writable boolean/finite-enum metadata without changing existing catalog behavior.
- Modify `plugins/skills/miloco-home-patrol/SKILL.md`, `plugins/openclaw/src/home-profile/scheduler.ts`, and `plugins/hermes/miloco-plugin/cron_setup.py`: describe and trigger the deterministic observation step without returning candidates to the agent.
- Create focused tests in `backend/miloco/tests/test_iot_observation_*.py`, plus scheduler/adapter tests where existing patterns support them.

### Task 1: Policy and domain contracts

**Files:**
- Create: `backend/miloco/src/miloco/iot_observation/__init__.py`
- Create: `backend/miloco/src/miloco/iot_observation/models.py`
- Create: `backend/miloco/src/miloco/iot_observation/policy.py`
- Test: `backend/miloco/tests/test_iot_observation_policy.py`

**Interfaces:** `ObservationPolicy.is_monitored(spec_entry) -> bool`, `ObservationPolicy.accepts_value(value) -> bool`, and typed immutable records for accepted changes, baselines, coverage gaps, and analysis results.

- [ ] Write tests for writable bool, finite enum, read-only, continuous numeric, missing catalog, invalid enum value, and policy version changes.
- [ ] Run `cd backend/miloco && uv run pytest tests/test_iot_observation_policy.py -q` and verify the new tests fail for missing contracts.
- [ ] Implement the smallest policy/model layer, with policy version included in every monitored attribute identity/epoch.
- [ ] Re-run the focused tests and verify they pass.

### Task 2: SQLite repository and retention

**Files:**
- Create: `backend/miloco/src/miloco/iot_observation/repository.py`
- Test: `backend/miloco/tests/test_iot_observation_repository.py`

**Interfaces:** `ObservationRepository.open()`, `record_baseline`, `record_change`, `record_gap`, `begin_run`, `finish_run`, `recent_changes`, `window_snapshot`, `prune`, and `close`.

- [ ] Write tests for schema creation, isolated database path, baseline/change distinction, duplicate same-value suppression, old/new preservation, watermark reads, family/epoch isolation, retention, row caps, and replay exclusion.
- [ ] Run the focused repository tests and verify they fail before implementation.
- [ ] Implement WAL-safe short transactions, indexes for path/time/window reads, schema versioning, bounded pruning, and explicit disk-budget refusal/gap reporting.
- [ ] Re-run repository tests and verify they pass.

### Task 3: Deterministic analyzer

**Files:**
- Create: `backend/miloco/src/miloco/iot_observation/analyzer.py`
- Test: `backend/miloco/tests/test_iot_observation_analyzer.py`

**Interfaces:** `ObservationAnalyzer.analyze(now, run_id) -> AnalysisResult`; analyzer receives repository snapshots and never invokes business services.

- [ ] Write tests for first-run two-hour lookback, successful-analysis watermark, rapid off/on/off events, same-value reports, three different `emerging` dates, long-term weekday/weekend thresholds, unknown coverage days, cross-midnight windows, adjacent half-open windows, sparse data, and candidate/detail truncation.
- [ ] Run the focused analyzer tests and verify they fail.
- [ ] Implement local-date grouping, 30-minute/15-minute sliding windows, effective-date denominators, hit-date deduplication, and evidence fields needed for logs.
- [ ] Re-run the focused tests and verify they pass.

### Task 4: Collector lifecycle and scope isolation

**Files:**
- Create: `backend/miloco/src/miloco/iot_observation/collector.py`
- Test: `backend/miloco/tests/test_iot_observation_collector.py`

**Interfaces:** `ObservationCollector.start()`, `handle_change(change)`, `mark_alignment_started/completed`, `mark_gap`, `switch_scope`, `stop`; callback work must be non-blocking and bounded.

- [ ] Write tests for subscription pattern, baseline on alignment/recovery/top-up, valid event ordering from `Change.old/new`, deletion, unknown source, queue overflow, late old-scope events, duplicate start/stop, and unsubscribe behavior.
- [ ] Run the focused collector tests and verify they fail.
- [ ] Implement the bounded queue and single writer, with all scope/epoch checks repeated at write time and gap records for overflow/distribution loss.
- [ ] Re-run the focused tests and verify they pass.

### Task 5: Runner, config, endpoint, and Manager wiring

**Files:**
- Create: `backend/miloco/src/miloco/iot_observation/runner.py`
- Create: `backend/miloco/src/miloco/iot_observation/router.py`
- Modify: `backend/miloco/src/miloco/config/settings.py`
- Modify: `backend/miloco/src/miloco/config/settings.yaml`
- Modify: `backend/miloco/src/miloco/manager.py`
- Modify: `backend/miloco/src/miloco/main.py`
- Test: `backend/miloco/tests/test_iot_observation_runner.py`, `backend/miloco/tests/test_iot_observation_integration.py`

**Interfaces:** `ObservationRunner.run(family_id, source="live|replay") -> RunResult`; endpoint returns only `run_id`, `status`, and `reason`.

- [ ] Write tests for default-off behavior, 5-second timeout, single-family mutex, `already_running`, failure isolation, startup/shutdown, scope switching, and no business-side-effect calls.
- [ ] Run focused runner/integration tests and verify they fail.
- [ ] Implement settings, service construction, router auth, lifecycle ordering (subscribe after store start and before alignment; unsubscribe before store stop), and structured logs with run id and truncation counts.
- [ ] Re-run focused tests and verify they pass.

### Task 6: OpenClaw/Hermes patrol integration

**Files:**
- Modify: `plugins/skills/miloco-home-patrol/SKILL.md`
- Modify: `plugins/openclaw/src/home-profile/scheduler.ts`
- Modify: `plugins/hermes/miloco-plugin/cron_setup.py`
- Test: existing scheduler/plugin tests or focused new tests where available.

**Interfaces:** both patrol entry points make exactly one deterministic backend observation call before Agent early exits; candidates never enter the Agent prompt/result.

- [ ] Write tests or fixtures proving both scheduler prompts include the same observation step and preserve existing schedule/delivery settings.
- [ ] Run the focused plugin tests and verify the new assertions fail.
- [ ] Update both prompts and the skill to state that the backend call is best-effort, log-only, non-repeating, and not an Agent tool/query workflow.
- [ ] Re-run plugin tests and verify they pass.

### Task 7: Verification and operational documentation

**Files:**
- Modify: `backend/miloco/README.md` and only the relevant deployment/config docs.
- Test: complete focused backend suite and relevant CLI/plugin suites.

- [ ] Run replay tests covering all fixed acceptance cases from the design, including 200,000-event/space-budget measurements where the environment supports them.
- [ ] Run `cd backend/miloco && uv run pytest tests/test_iot_observation_*.py -q`.
- [ ] Run the smallest relevant existing suites for Manager, MIoT, patrol scheduler, and Hermes cron reconciliation.
- [ ] Review `git diff --check`, confirm no user-facing CLI/UI or business side effects were added, and report any unmeasured performance claims explicitly.
