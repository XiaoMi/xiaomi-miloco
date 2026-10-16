import type {
  GraphDatum,
  GraphDecodeResult,
  GraphDefinition,
  GraphDeviceSummary,
  GraphEdge,
  GraphEvidence,
  GraphFreshness,
  GraphGroup,
  GraphKind,
  GraphMedia,
  GraphMetric,
  GraphNode,
  GraphProtocolWarning,
  GraphResponse,
  GraphRole,
  GraphSeverity,
  GraphSource,
  GraphStatus,
  GraphValue,
  GraphWarning,
} from "./types";

const SOURCES = new Set<GraphSource>([
  "observed",
  "configured",
  "model_fixed",
  "unknown",
]);
const STATUSES = new Set<GraphStatus>([
  "ok",
  "warning",
  "skipped",
  "backpressure",
  "error",
  "inactive",
  "unknown",
]);
const FRESHNESS = new Set<GraphFreshness>(["fresh", "stale", "unknown"]);
const KINDS = new Set<GraphKind>([
  "source",
  "buffer",
  "transform",
  "filter",
  "inference",
  "encoder",
  "external",
  "sink",
  "generic",
]);
const ROLES = new Set<GraphRole>([
  "input",
  "output",
  "config",
  "flow",
  "state",
  "detail",
]);
const SEVERITIES = new Set<GraphSeverity>(["info", "warning", "error"]);
const SHORT_TEXT_LIMIT = 128;
const DISPLAY_TEXT_LIMIT = 512;

class ProtocolError extends Error {}

function asObject(value: unknown, message: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ProtocolError(message);
  }
  return value as Record<string, unknown>;
}

function asArray(value: unknown, path: string, limit?: number): unknown[] {
  if (!Array.isArray(value)) throw new ProtocolError(`${path} must be an array`);
  if (limit !== undefined && value.length > limit) {
    throw new ProtocolError(`${path} exceeds limit ${limit}`);
  }
  return value;
}

function asString(
  value: unknown,
  path: string,
  limit = DISPLAY_TEXT_LIMIT,
): string {
  if (typeof value !== "string") throw new ProtocolError(`${path} must be a string`);
  if (value.length > limit) {
    throw new ProtocolError(`${path} exceeds length limit ${limit}`);
  }
  return value;
}

function asShortString(value: unknown, path: string): string {
  return asString(value, path, SHORT_TEXT_LIMIT);
}

function asNullableString(
  value: unknown,
  path: string,
  limit = DISPLAY_TEXT_LIMIT,
): string | null {
  return value === null ? null : asString(value, path, limit);
}

function asBoolean(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") throw new ProtocolError(`${path} must be a boolean`);
  return value;
}

function asFiniteNumber(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new ProtocolError(`${path} must be a finite number`);
  }
  return value;
}

function asInteger(value: unknown, path: string): number {
  const decoded = asFiniteNumber(value, path);
  if (!Number.isInteger(decoded)) throw new ProtocolError(`${path} must be an integer`);
  return decoded;
}

function asNullableNumber(value: unknown, path: string): number | null {
  return value === null ? null : asFiniteNumber(value, path);
}

function asEnum<T extends string>(
  value: unknown,
  allowed: ReadonlySet<T>,
  path: string,
): T {
  const decoded = asString(value, path) as T;
  if (!allowed.has(decoded)) {
    throw new ProtocolError(`${path} has an unsupported value: ${decoded}`);
  }
  return decoded;
}

function normalizedStatus(
  value: unknown,
  path: string,
  warnings: GraphProtocolWarning[],
): GraphStatus {
  const decoded = asString(value, path);
  if (STATUSES.has(decoded as GraphStatus)) return decoded as GraphStatus;
  warnings.push({
    code: "unknown_status",
    message: `Unknown status "${decoded}" was rendered as unknown.`,
    path,
    value: decoded,
  });
  return "unknown";
}

function normalizedKind(
  value: unknown,
  path: string,
  warnings: GraphProtocolWarning[],
): GraphKind {
  const decoded = asString(value, path);
  if (KINDS.has(decoded as GraphKind)) return decoded as GraphKind;
  warnings.push({
    code: "unknown_kind",
    message: `Unknown node kind "${decoded}" was rendered as generic.`,
    path,
    value: decoded,
  });
  return "generic";
}

function asGraphValue(value: unknown, path: string): GraphValue {
  if (typeof value === "string") return asString(value, path);
  if (typeof value === "boolean") return value;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  throw new ProtocolError(`${path} must be a string, boolean, or finite number`);
}

function decodeDatum(
  value: unknown,
  path: string,
  integerValue: boolean,
): GraphDatum {
  const decoded = asObject(value, `${path} must be an object`);
  const datumSource = asEnum(decoded.source, SOURCES, `${path}.source`);
  const datumValue = asNullableNumber(decoded.value, `${path}.value`);
  if (datumValue !== null && integerValue && !Number.isInteger(datumValue)) {
    throw new ProtocolError(`${path}.value must be an integer`);
  }
  if (datumValue !== null && datumValue < 0) {
    throw new ProtocolError(`${path}.value must be non-negative`);
  }
  if ((datumValue === null) !== (datumSource === "unknown")) {
    throw new ProtocolError(`${path} has inconsistent value and source`);
  }
  return { value: datumValue, source: datumSource };
}

function decodeNullableDatum(
  value: unknown,
  path: string,
  integerValue: boolean,
): GraphDatum | null {
  return value === null ? null : decodeDatum(value, path, integerValue);
}

function decodeMedia(value: unknown, path: string): GraphMedia {
  const decoded = asObject(value, `${path} must be an object`);
  return {
    width: decodeNullableDatum(decoded.width, `${path}.width`, true),
    height: decodeNullableDatum(decoded.height, `${path}.height`, true),
    fps: decodeNullableDatum(decoded.fps, `${path}.fps`, false),
    frame_count: decodeNullableDatum(
      decoded.frame_count,
      `${path}.frame_count`,
      true,
    ),
    duration_ms: decodeNullableDatum(
      decoded.duration_ms,
      `${path}.duration_ms`,
      false,
    ),
  };
}

function decodeMetric(value: unknown, path: string): GraphMetric {
  const decoded = asObject(value, `${path} must be an object`);
  const metricSource = asEnum(decoded.source, SOURCES, `${path}.source`);
  const metricValue =
    decoded.value === null ? null : asGraphValue(decoded.value, `${path}.value`);
  if ((metricValue === null) !== (metricSource === "unknown")) {
    throw new ProtocolError(`${path} has inconsistent value and source`);
  }
  return {
    key: asShortString(decoded.key, `${path}.key`),
    label: asString(decoded.label, `${path}.label`),
    value: metricValue,
    unit: asNullableString(decoded.unit, `${path}.unit`, SHORT_TEXT_LIMIT),
    source: metricSource,
    role: asEnum(decoded.role, ROLES, `${path}.role`),
    severity:
      decoded.severity === null
        ? null
        : asEnum(decoded.severity, SEVERITIES, `${path}.severity`),
  };
}

function decodeWarning(value: unknown, path: string): GraphWarning {
  const decoded = asObject(value, `${path} must be an object`);
  const paramsObject = asObject(decoded.params, `${path}.params must be an object`);
  const params: Record<string, GraphValue> = {};
  for (const [key, item] of Object.entries(paramsObject)) {
    asShortString(key, `${path}.params key`);
    params[key] = asGraphValue(item, `${path}.params.${key}`);
  }
  return {
    code: asShortString(decoded.code, `${path}.code`),
    message: asString(decoded.message, `${path}.message`),
    severity: asEnum(decoded.severity, SEVERITIES, `${path}.severity`),
    params,
  };
}

function decodeEvidence(value: unknown, path: string): GraphEvidence {
  const decoded = asObject(value, `${path} must be an object`);
  const ageMs = asNullableNumber(decoded.age_ms, `${path}.age_ms`);
  if (ageMs !== null && ageMs < 0) {
    throw new ProtocolError(`${path}.age_ms must be non-negative`);
  }
  return {
    trace_id: asNullableString(
      decoded.trace_id,
      `${path}.trace_id`,
      SHORT_TEXT_LIMIT,
    ),
    device_trace_id: asNullableString(
      decoded.device_trace_id,
      `${path}.device_trace_id`,
      SHORT_TEXT_LIMIT,
    ),
    observed_at:
      decoded.observed_at === null
        ? null
        : asInteger(decoded.observed_at, `${path}.observed_at`),
    age_ms: ageMs,
  };
}

function decodeList<T>(
  value: unknown,
  path: string,
  decode: (item: unknown, path: string) => T,
  limit?: number,
): T[] {
  return asArray(value, path, limit).map((item, index) =>
    decode(item, `${path}[${index}]`),
  );
}

function decodeNode(
  value: unknown,
  path: string,
  warnings: GraphProtocolWarning[],
): GraphNode {
  const decoded = asObject(value, `${path} must be an object`);
  return {
    id: asShortString(decoded.id, `${path}.id`),
    kind: normalizedKind(decoded.kind, `${path}.kind`, warnings),
    label: asString(decoded.label, `${path}.label`),
    group: asNullableString(decoded.group, `${path}.group`, SHORT_TEXT_LIMIT),
    rank: decoded.rank === null ? null : asInteger(decoded.rank, `${path}.rank`),
    order: asInteger(decoded.order, `${path}.order`),
    status: normalizedStatus(decoded.status, `${path}.status`, warnings),
    freshness: asEnum(decoded.freshness, FRESHNESS, `${path}.freshness`),
    last_observed_status:
      decoded.last_observed_status === null
        ? null
        : normalizedStatus(
            decoded.last_observed_status,
            `${path}.last_observed_status`,
            warnings,
          ),
    standalone: asBoolean(decoded.standalone, `${path}.standalone`),
    metrics: decodeList(decoded.metrics, `${path}.metrics`, decodeMetric, 32),
    input:
      decoded.input === null ? null : decodeMedia(decoded.input, `${path}.input`),
    output:
      decoded.output === null
        ? null
        : decodeMedia(decoded.output, `${path}.output`),
    warnings: decodeList(
      decoded.warnings,
      `${path}.warnings`,
      decodeWarning,
      16,
    ),
    evidence:
      decoded.evidence === null
        ? null
        : decodeEvidence(decoded.evidence, `${path}.evidence`),
  };
}

function decodeEdge(
  value: unknown,
  path: string,
  warnings: GraphProtocolWarning[],
): GraphEdge {
  const decoded = asObject(value, `${path} must be an object`);
  return {
    id: asShortString(decoded.id, `${path}.id`),
    from: asShortString(decoded.from, `${path}.from`),
    to: asShortString(decoded.to, `${path}.to`),
    label: asNullableString(decoded.label, `${path}.label`),
    active: asBoolean(decoded.active, `${path}.active`),
    status: normalizedStatus(decoded.status, `${path}.status`, warnings),
    freshness: asEnum(decoded.freshness, FRESHNESS, `${path}.freshness`),
    last_observed_status:
      decoded.last_observed_status === null
        ? null
        : normalizedStatus(
            decoded.last_observed_status,
            `${path}.last_observed_status`,
            warnings,
          ),
    media:
      decoded.media === null
        ? null
        : decodeMedia(decoded.media, `${path}.media`),
    metrics: decodeList(decoded.metrics, `${path}.metrics`, decodeMetric, 32),
    warnings: decodeList(
      decoded.warnings,
      `${path}.warnings`,
      decodeWarning,
      16,
    ),
    evidence:
      decoded.evidence === null
        ? null
        : decodeEvidence(decoded.evidence, `${path}.evidence`),
  };
}

function decodeGroup(value: unknown, path: string): GraphGroup {
  const decoded = asObject(value, `${path} must be an object`);
  return {
    id: asShortString(decoded.id, `${path}.id`),
    label: asString(decoded.label, `${path}.label`),
    order: asInteger(decoded.order, `${path}.order`),
  };
}

function decodeDeviceSummary(
  value: unknown,
  path: string,
  warnings: GraphProtocolWarning[],
): GraphDeviceSummary {
  const decoded = asObject(value, `${path} must be an object`);
  return {
    device_id: asShortString(decoded.device_id, `${path}.device_id`),
    room_name: asNullableString(decoded.room_name, `${path}.room_name`),
    trace_id: asNullableString(
      decoded.trace_id,
      `${path}.trace_id`,
      SHORT_TEXT_LIMIT,
    ),
    device_trace_id: asNullableString(
      decoded.device_trace_id,
      `${path}.device_trace_id`,
      SHORT_TEXT_LIMIT,
    ),
    observed_at:
      decoded.observed_at === null
        ? null
        : asInteger(decoded.observed_at, `${path}.observed_at`),
    freshness: asEnum(decoded.freshness, FRESHNESS, `${path}.freshness`),
    status: normalizedStatus(decoded.status, `${path}.status`, warnings),
  };
}

function decodeDefinition(
  value: unknown,
  warnings: GraphProtocolWarning[],
): GraphDefinition {
  const decoded = asObject(value, "graph must be an object");
  const layout = asObject(decoded.layout, "graph.layout must be an object");
  const direction = asString(decoded.direction, "graph.direction");
  if (direction !== "LR" && direction !== "TB") {
    throw new ProtocolError(
      `graph.direction has an unsupported value: ${direction}`,
    );
  }
  return {
    id: asShortString(decoded.id, "graph.id"),
    label: asString(decoded.label, "graph.label"),
    direction,
    layout: {
      rank_separation: asFiniteNumber(
        layout.rank_separation,
        "graph.layout.rank_separation",
      ),
      node_separation: asFiniteNumber(
        layout.node_separation,
        "graph.layout.node_separation",
      ),
    },
    groups: decodeList(decoded.groups, "graph.groups", decodeGroup),
    nodes: asArray(decoded.nodes, "graph.nodes", 64).map((item, index) =>
      decodeNode(item, `graph.nodes[${index}]`, warnings),
    ),
    edges: asArray(decoded.edges, "graph.edges", 128).map((item, index) =>
      decodeEdge(item, `graph.edges[${index}]`, warnings),
    ),
  };
}

function validateTopology(graph: GraphDefinition): void {
  const nodeIds = new Set<string>();
  for (const node of graph.nodes) {
    if (nodeIds.has(node.id)) throw new ProtocolError(`Duplicate node id: ${node.id}`);
    nodeIds.add(node.id);
  }

  const edgeIds = new Set<string>();
  const adjacency = new Map(graph.nodes.map((node) => [node.id, [] as string[]]));
  for (const edge of graph.edges) {
    if (edgeIds.has(edge.id)) throw new ProtocolError(`Duplicate edge id: ${edge.id}`);
    edgeIds.add(edge.id);
    if (!nodeIds.has(edge.from) || !nodeIds.has(edge.to)) {
      throw new ProtocolError(`Edge ${edge.id} references an unknown node`);
    }
    adjacency.get(edge.from)?.push(edge.to);
  }

  const groupIds = new Set<string>();
  for (const group of graph.groups) {
    if (groupIds.has(group.id)) {
      throw new ProtocolError(`Duplicate group id: ${group.id}`);
    }
    groupIds.add(group.id);
  }
  for (const node of graph.nodes) {
    if (node.group !== null && !groupIds.has(node.group)) {
      throw new ProtocolError(`Node ${node.id} references an unknown group`);
    }
  }

  const state = new Map<string, 0 | 1 | 2>();
  const visit = (nodeId: string) => {
    if (state.get(nodeId) === 1) throw new ProtocolError("Graph contains a cycle");
    if (state.get(nodeId) === 2) return;
    state.set(nodeId, 1);
    for (const target of adjacency.get(nodeId) ?? []) visit(target);
    state.set(nodeId, 2);
  };
  for (const node of graph.nodes) visit(node.id);

  const incomingCounts = new Map(graph.nodes.map((node) => [node.id, 0]));
  for (const edge of graph.edges) {
    incomingCounts.set(edge.to, (incomingCounts.get(edge.to) ?? 0) + 1);
  }
  const declaredSources = graph.nodes.filter((node) => node.kind === "source");
  const declaredSinks = graph.nodes.filter((node) => node.kind === "sink");
  const sources = declaredSources.length > 0
    ? declaredSources
    : graph.nodes.filter(
        (node) => !node.standalone && incomingCounts.get(node.id) === 0,
      );
  const sinks = declaredSinks.length > 0
    ? declaredSinks
    : graph.nodes.filter(
        (node) => !node.standalone && (adjacency.get(node.id)?.length ?? 0) === 0,
      );
  if (sources.length === 0 || sinks.length === 0) {
    throw new ProtocolError("Graph requires source and sink nodes");
  }

  const reachableFromSource = new Set<string>();
  const pendingFromSource = sources.map((node) => node.id);
  while (pendingFromSource.length > 0) {
    const nodeId = pendingFromSource.pop()!;
    if (reachableFromSource.has(nodeId)) continue;
    reachableFromSource.add(nodeId);
    pendingFromSource.push(...(adjacency.get(nodeId) ?? []));
  }

  const reverseAdjacency = new Map(
    graph.nodes.map((node) => [node.id, [] as string[]]),
  );
  for (const edge of graph.edges) {
    reverseAdjacency.get(edge.to)?.push(edge.from);
  }
  const canReachSink = new Set<string>();
  const pendingToSink = sinks.map((node) => node.id);
  while (pendingToSink.length > 0) {
    const nodeId = pendingToSink.pop()!;
    if (canReachSink.has(nodeId)) continue;
    canReachSink.add(nodeId);
    pendingToSink.push(...(reverseAdjacency.get(nodeId) ?? []));
  }

  const disconnected = graph.nodes.find(
    (node) =>
      !node.standalone &&
      node.status !== "inactive" &&
      (!reachableFromSource.has(node.id) || !canReachSink.has(node.id)),
  );
  if (disconnected) {
    throw new ProtocolError(
      `Active node is not on a source-to-sink path: ${disconnected.id}`,
    );
  }
}

function decode(value: unknown): {
  graph: GraphResponse;
  warnings: GraphProtocolWarning[];
} {
  const decoded = asObject(value, "Graph response must be an object");
  if (decoded.schema_version !== 1) {
    throw new ProtocolError(
      `Unsupported graph schema version: ${String(decoded.schema_version)}`,
    );
  }

  const warnings: GraphProtocolWarning[] = [];
  const graph = decodeDefinition(decoded.graph, warnings);
  validateTopology(graph);
  const scope = asObject(decoded.scope, "scope must be an object");
  const summary = asObject(decoded.summary, "summary must be an object");
  const staleAfterSec = asFiniteNumber(decoded.stale_after_sec, "stale_after_sec");
  if (staleAfterSec < 0) {
    throw new ProtocolError("stale_after_sec must be non-negative");
  }

  return {
    graph: {
      schema_version: 1,
      generated_at: asInteger(decoded.generated_at, "generated_at"),
      process_started_at: asInteger(
        decoded.process_started_at,
        "process_started_at",
      ),
      stale_after_sec: staleAfterSec,
      scope: {
        device_id: asNullableString(
          scope.device_id,
          "scope.device_id",
          SHORT_TEXT_LIMIT,
        ),
        room_name: asNullableString(scope.room_name, "scope.room_name"),
      },
      graph,
      summary: {
        latest_trace_id: asNullableString(
          summary.latest_trace_id,
          "summary.latest_trace_id",
          SHORT_TEXT_LIMIT,
        ),
        latest_device_trace_id: asNullableString(
          summary.latest_device_trace_id,
          "summary.latest_device_trace_id",
          SHORT_TEXT_LIMIT,
        ),
        observed_at:
          summary.observed_at === null
            ? null
            : asInteger(summary.observed_at, "summary.observed_at"),
        freshness: asEnum(summary.freshness, FRESHNESS, "summary.freshness"),
        devices: asArray(summary.devices, "summary.devices").map(
          (item, index) =>
            decodeDeviceSummary(item, `summary.devices[${index}]`, warnings),
        ),
      },
    },
    warnings,
  };
}

export function decodeGraphResponse(value: unknown): GraphDecodeResult {
  try {
    const result = decode(value);
    return { ok: true, ...result };
  } catch (error) {
    if (error instanceof ProtocolError) return { ok: false, error: error.message };
    throw error;
  }
}
