# Perf Perception Pipeline Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved runtime-only perception pipeline Graph JSON and render it on the existing Perf page.

**Architecture:** Realtime perception produces optional per-device diagnostics only when Perf is enabled. A process-local snapshot store retains the latest observation per active device, and a backend adapter validates and serializes a complete Graph JSON. The Web client treats the response as unknown input, decodes it defensively, and renders a generic read-only SVG graph without knowing perception topology.

**Tech Stack:** Python 3.11+, dataclasses, Pydantic/FastAPI, pytest, React 19, TypeScript, Vitest, native SVG.

**Spec:** `docs/superpowers/specs/2026-09-17-perf-perception-pipeline-flow-design.md`

## Global Constraints

- Graph labels and messages are English only; the surrounding Perf card chrome uses existing frontend i18n.
- Node and edge media dimensions come only from observed frames, current configuration, or active model sessions; unavailable values remain unknown.
- New diagnostics are process-local runtime data and never enter SQLite, event payloads, `OmniEventArtifacts`, `omni_trace.json.gz`, or other files.
- On-demand perception is unchanged and excluded from flow diagnostics.
- `perf.enabled=false` creates no snapshot store, collects no flow-only diagnostics, and exposes no new endpoint.
- Backend producer enums are closed; frontend decoding alone tolerates unexpected `kind/status` values.
- The Graph evidence and `traces_device` record for a device reuse the same explicit `device_trace_id`.

---

### Task 1: Runtime Snapshot Store and Graph Contract

**Files:**
- Create: `backend/miloco/src/miloco/observability/perception_flow.py`
- Create: `backend/miloco/tests/observability/test_perception_flow.py`

**Interfaces:**
- Produces: `PerDeviceFlowDiagnostics`, `PerceptionFlowCycleDiagnostics`, `PerceptionFlowSnapshotStore`.
- Produces: `build_perception_flow_graph(store, device_id, settings, process_started_at)` returning the strict response model.
- Produces: module accessors that bind or clear the store only while Perf is enabled.

- [ ] **Step 1: Write failing tests for atomic merge, retention, clear, stale snapshots, audio-only media, global discrete aggregation, enum rejection, invalid graph references, duplicate IDs, cycles, and default empty topology.**
- [ ] **Step 2: Run `uv run pytest miloco/tests/observability/test_perception_flow.py -q` from `backend/` and verify failures are caused by the missing module.**
- [ ] **Step 3: Implement immutable diagnostic records, locked latest-per-device storage, strict Pydantic Graph models, topology validation, default semantic graph, device projection, and global summary projection.**
- [ ] **Step 4: Re-run the focused test file and keep it green.**

### Task 2: Collection and Pipeline Diagnostics

**Files:**
- Modify: `backend/miloco/src/miloco/perception/collect/stream_buffer.py`
- Modify: `backend/miloco/src/miloco/perception/schema.py`
- Modify: `backend/miloco/src/miloco/perception/collect/camera_adapter.py`
- Modify: `backend/miloco/src/miloco/perception/engine/types.py`
- Create: `backend/miloco/src/miloco/perception/flow_context.py`
- Modify: `backend/miloco/src/miloco/perception/engine/pipeline.py`
- Modify: `backend/miloco/src/miloco/perception/engine/omni/prompt_builder.py`
- Test: `backend/miloco/tests/perception/test_stream_buffer_overflow.py`
- Create: `backend/miloco/tests/perception/engine/test_flow_diagnostics.py`

**Interfaces:**
- `ReadyWindow` carries `last_drain_observed_at`, `last_drain_ready_depth_before`, and `last_drain_ready_depth_after` sampled under the buffer lock.
- `run_batch_pipeline(..., trace_id, device_trace_ids, collect_flow_diagnostics)` returns per-device partial diagnostics on success, Gate skip, and isolated device failure.
- `flow_diagnostics_scope()` exposes media transform, encoded media, and Smart Crop recorders as runtime-only no-ops without an active scope.

- [ ] **Step 1: Add failing buffer tests proving last-drain depth is sampled atomically.**
- [ ] **Step 2: Add failing pipeline tests for explicit trace IDs, source/pipeline/Gate/Omni counts, Gate skip, partial failure, encoded media, Smart Crop, and audio-only semantics.**
- [ ] **Step 3: Implement the minimal collection metadata and task-local diagnostic scope without changing on-demand behavior.**
- [ ] **Step 4: Thread diagnostics through the realtime batch pipeline and reuse supplied `device_trace_id` values.**
- [ ] **Step 5: Run the focused collection and pipeline tests.**

### Task 3: Processor Lifecycle and Perf Endpoint

**Files:**
- Modify: `backend/miloco/src/miloco/perception/processor.py`
- Modify: `backend/miloco/src/miloco/perception/client.py`
- Modify: `backend/miloco/src/miloco/perception/engine/api.py`
- Modify: `backend/miloco/src/miloco/perception/runner.py`
- Modify: `backend/miloco/src/miloco/observability/router.py`
- Modify: `backend/miloco/src/miloco/main.py`
- Modify: `backend/miloco/tests/observability/test_router.py`
- Create: `backend/miloco/tests/observability/test_perception_flow_lifecycle.py`

**Interfaces:**
- `PipelineProcessor` owns an optional store and exposes `retain_flow_devices()`, `clear_flow_snapshots()`, and `perception_flow_store`.
- Realtime proxy/API methods accept explicit `trace_id` and `device_trace_ids` while preserving the existing public result tuple.
- `GET /api/perf/perception-flow?device_id=...` reads the bound runtime store and returns the strict Graph JSON.

- [ ] **Step 1: Write failing tests for endpoint filtering, missing devices, Perf-disabled mounting, successful sync cleanup, final-device cleanup without another cycle, restart/stop/close/reinit clearing, system-failure partial snapshots, and trace ID equality.**
- [ ] **Step 2: Pass explicit IDs through processor/client/API, merge completed or partial diagnostics, and keep diagnostics failures isolated from perception results.**
- [ ] **Step 3: Wire store binding, active-device retention, lifecycle clears, and the new authenticated endpoint.**
- [ ] **Step 4: Add negative persistence assertions for SQLite/event artifacts/file payloads and run focused backend tests.**

### Task 4: Frontend Decoder, Generic SVG Viewer, and Perf Integration

**Files:**
- Create: `web/src/lib/graphDecode.ts`
- Modify: `web/src/lib/types.ts`
- Modify: `web/src/api/real.ts`
- Modify: `web/src/api/index.ts`
- Create: `web/src/components/GenericGraphViewer.tsx`
- Create: `web/src/components/PerfPipelineFlow.tsx`
- Modify: `web/src/components/PerfPage.tsx`
- Create: `web/tests/graphDecode.test.ts`
- Create: `web/tests/GenericGraphViewer.test.tsx`
- Create: `web/tests/PerfPipelineFlow.test.tsx`

**Interfaces:**
- `decodeGraphResponse(value: unknown): GraphDecodeResult` validates schema references and DAG structure, normalizes unexpected `kind/status`, and returns protocol warnings.
- `getPerceptionFlow(deviceId?: string)` fetches unknown JSON and decodes it before exposing data to components.
- `GenericGraphViewer` consumes only Graph JSON node, edge, group, metric, media, status, freshness, and layout fields; it wraps the complete graph instead of requiring horizontal scrolling.

- [ ] **Step 1: Write failing decoder tests for valid, malformed, unknown-enum, duplicate, dangling-edge, cyclic, stale, inactive-edge, and audio-only fixtures.**
- [ ] **Step 2: Implement types and runtime decoding with local protocol errors.**
- [ ] **Step 3: Write failing component tests for dynamic topology, read-only rendering, stale status override, English labels/messages, scope selection, manual/automatic refresh, and independence from the historical window selector.**
- [ ] **Step 4: Implement the generic SVG viewer and Perf section, then integrate it after KPI cards.**
- [ ] **Step 5: Run focused Vitest and TypeScript checks.**

### Task 5: Verification and Self-Review

**Files:**
- Review all changed files and the approved spec.

- [ ] **Step 1: Run focused backend and frontend suites for all changed behavior.**
- [ ] **Step 2: Run backend Ruff, backend type checks where practical, Web TypeScript, Web tests, and Web build.**
- [ ] **Step 3: Inspect `git diff --stat` and `git diff` for accidental persistence fields, on-demand changes, Graph i18n, unknown producer enums, or unrelated formatting.**
- [ ] **Step 4: Compare every acceptance criterion in the spec with implemented tests and document any real limitation instead of claiming unsupported coverage.**
