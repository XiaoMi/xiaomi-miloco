import { useMemo } from "react";

import type {
  GraphDefinition,
  GraphEdge,
  GraphFreshness,
  GraphGroup,
  GraphMedia,
  GraphMetric,
  GraphNode,
  GraphResponse,
  GraphStatus,
} from "@/lib/types";

const NODE_WIDTH = 230;
const NODE_MIN_HEIGHT = 140;
const NODE_BOTTOM_PADDING = 18;
const MARGIN = 40;
const MAX_NODES_PER_ROW = 5;
const NODE_TEXT_WIDTH = 28;
const LABEL_LINE_HEIGHT = 18;
const SUMMARY_LINE_HEIGHT = 17;
const METRIC_LINE_HEIGHT = 15;

export type GraphAppearance = GraphStatus | "stale";

export interface PositionedGraphNode extends GraphNode {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface PositionedGraphEdge extends GraphEdge {
  path: string;
  labelX: number;
  labelY: number;
}

export interface PositionedGraphGroup extends GraphGroup {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface GraphLayoutResult {
  width: number;
  height: number;
  groups: PositionedGraphGroup[];
  nodes: PositionedGraphNode[];
  edges: PositionedGraphEdge[];
}

const APPEARANCE = {
  ok: {
    fill: "var(--color-success-bg)",
    stroke: "var(--color-success)",
    text: "var(--color-success)",
  },
  warning: {
    fill: "var(--color-warning-bg)",
    stroke: "var(--color-warning)",
    text: "var(--color-warning)",
  },
  skipped: {
    fill: "var(--color-bg-tertiary)",
    stroke: "var(--color-border-strong)",
    text: "var(--color-text-secondary)",
  },
  backpressure: {
    fill: "var(--color-warning-bg)",
    stroke: "var(--color-warning)",
    text: "var(--color-warning)",
  },
  error: {
    fill: "var(--color-error-bg)",
    stroke: "var(--color-error)",
    text: "var(--color-error)",
  },
  inactive: {
    fill: "var(--color-bg-tertiary)",
    stroke: "var(--color-border-strong)",
    text: "var(--color-text-secondary)",
  },
  unknown: {
    fill: "var(--color-bg-tertiary)",
    stroke: "var(--color-border-strong)",
    text: "var(--color-text-secondary)",
  },
  stale: {
    fill: "var(--color-bg-tertiary)",
    stroke: "var(--color-text-tertiary)",
    text: "var(--color-text-secondary)",
  },
} satisfies Record<GraphAppearance, { fill: string; stroke: string; text: string }>;

export function effectiveGraphAppearance(
  status: GraphStatus,
  freshness: GraphFreshness,
): GraphAppearance {
  if (freshness === "stale") return "stale";
  if (freshness === "unknown") return "unknown";
  return status;
}

function computedRanks(graph: GraphDefinition): Map<string, number> {
  const ranks = new Map<string, number>();
  const incoming = new Map(graph.nodes.map((node) => [node.id, [] as string[]]));
  for (const edge of graph.edges) incoming.get(edge.to)?.push(edge.from);

  const rankOf = (nodeId: string): number => {
    const known = ranks.get(nodeId);
    if (known !== undefined) return known;
    const node = graph.nodes.find((candidate) => candidate.id === nodeId);
    if (!node) return 0;
    const rank =
      node.rank ??
      Math.max(0, ...((incoming.get(nodeId) ?? []).map((source) => rankOf(source) + 1)));
    ranks.set(nodeId, rank);
    return rank;
  };

  for (const node of graph.nodes) rankOf(node.id);
  return ranks;
}

export function layoutGraph(graph: GraphDefinition): GraphLayoutResult {
  const ranks = computedRanks(graph);
  const nodeHeights = new Map(
    graph.nodes.map((node) => [node.id, nodeTextLayout(node).height]),
  );
  const rankSeparation = Math.max(56, graph.layout.rank_separation);
  const isHorizontal = graph.direction === "LR";
  const compareNodes = (left: GraphNode, right: GraphNode) =>
    (ranks.get(left.id) ?? 0) - (ranks.get(right.id) ?? 0) ||
    left.order - right.order ||
    left.id.localeCompare(right.id);
  const groupOrder = new Map(
    graph.groups.map((group) => [group.id, group.order]),
  );
  const units = [
    ...graph.groups.flatMap((group) => {
      const members = graph.nodes
        .filter((node) => node.group === group.id)
        .sort(compareNodes);
      return members.length > 0
        ? [{ id: `group:${group.id}`, order: group.order, nodes: members, grouped: true }]
        : [];
    }),
    ...graph.nodes
      .filter((node) => node.group === null)
      .map((node) => ({
        id: `node:${node.id}`,
        order: node.order,
        nodes: [node],
        grouped: false,
      })),
  ].sort((left, right) => {
    const leftRank = Math.min(...left.nodes.map((node) => ranks.get(node.id) ?? 0));
    const rightRank = Math.min(...right.nodes.map((node) => ranks.get(node.id) ?? 0));
    const leftGroupOrder = left.nodes[0].group === null
      ? left.order
      : groupOrder.get(left.nodes[0].group!) ?? left.order;
    const rightGroupOrder = right.nodes[0].group === null
      ? right.order
      : groupOrder.get(right.nodes[0].group!) ?? right.order;
    return leftRank - rightRank || leftGroupOrder - rightGroupOrder || left.id.localeCompare(right.id);
  });

  const rows: GraphNode[][] = [];
  if (isHorizontal) {
    let currentRow: GraphNode[] = [];
    for (const unit of units) {
      if (unit.grouped) {
        if (currentRow.length > 0) {
          rows.push(currentRow);
          currentRow = [];
        }
        rows.push(unit.nodes);
        continue;
      }
      if (
        currentRow.length > 0 &&
        currentRow.length + unit.nodes.length > MAX_NODES_PER_ROW
      ) {
        rows.push(currentRow);
        currentRow = [];
      }
      currentRow.push(...unit.nodes);
    }
    if (currentRow.length > 0) rows.push(currentRow);
  } else {
    rows.push(...units.flatMap((unit) => unit.nodes.map((node) => [node])));
  }

  const maxRowSize = Math.max(1, ...rows.map((row) => row.length));
  const maxNodeHeight = Math.max(...nodeHeights.values(), NODE_MIN_HEIGHT);
  const rowHeights = rows.map(() => maxNodeHeight);
  const rowOffsets = rowHeights.reduce<number[]>((offsets, _rowHeight, index) => {
    if (index === 0) {
      offsets.push(0);
    } else {
      offsets.push(offsets[index - 1] + rowHeights[index - 1] + rankSeparation);
    }
    return offsets;
  }, []);
  const width = isHorizontal
    ? MARGIN * 2 + maxRowSize * NODE_WIDTH + Math.max(0, maxRowSize - 1) * rankSeparation
    : MARGIN * 2 + NODE_WIDTH;
  const height =
    MARGIN * 2 +
    rowHeights.reduce((total, rowHeight) => total + rowHeight, 0) +
    Math.max(0, rows.length - 1) * rankSeparation;
  const nodes = rows.flatMap((row, rowIndex) =>
    row.map((node, columnIndex) => ({
      ...node,
      x: MARGIN + columnIndex * (NODE_WIDTH + (isHorizontal ? rankSeparation : 0)),
      y: MARGIN + rowOffsets[rowIndex],
      width: NODE_WIDTH,
      height: maxNodeHeight,
    })),
  );
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const groups = graph.groups
    .slice()
    .sort((left, right) => left.order - right.order || left.id.localeCompare(right.id))
    .flatMap((group) => {
      const members = nodes.filter((node) => node.group === group.id);
      if (members.length === 0) return [];
      const minX = Math.min(...members.map((node) => node.x));
      const minY = Math.min(...members.map((node) => node.y));
      const maxX = Math.max(...members.map((node) => node.x + node.width));
      const maxY = Math.max(...members.map((node) => node.y + node.height));
      return [
        {
          ...group,
          x: minX - 12,
          y: minY - 28,
          width: maxX - minX + 24,
          height: maxY - minY + 40,
        },
      ];
    });
  const edges = graph.edges.map((edge) => {
    const source = byId.get(edge.from)!;
    const target = byId.get(edge.to)!;
    const sourceCenterX = source.x + source.width / 2;
    const targetCenterX = target.x + target.width / 2;
    if (isHorizontal && source.y === target.y) {
      const flowsRight = targetCenterX >= sourceCenterX;
      const startX = flowsRight ? source.x + source.width : source.x;
      const startY = source.y + source.height / 2;
      const endX = flowsRight ? target.x : target.x + target.width;
      const endY = target.y + target.height / 2;
      const middleX = (startX + endX) / 2;
      const isNonAdjacent = Math.abs(target.x - source.x) > NODE_WIDTH + rankSeparation + 1;
      if (isNonAdjacent) {
        const routeY = source.y + source.height + 20;
        const startOuterX = startX + (flowsRight ? 20 : -20);
        const endOuterX = endX + (flowsRight ? -20 : 20);
        return {
          ...edge,
          path: `M ${startX} ${startY} H ${startOuterX} V ${routeY} H ${endOuterX} V ${endY} H ${endX}`,
          labelX: (startOuterX + endOuterX) / 2,
          labelY: routeY - 8,
        };
      }
      return {
        ...edge,
        path: `M ${startX} ${startY} C ${middleX} ${startY}, ${middleX} ${endY}, ${endX} ${endY}`,
        labelX: middleX,
        labelY: (startY + endY) / 2 - 18,
      };
    }
    const flowsDown = target.y > source.y;
    const startX = sourceCenterX;
    const startY = flowsDown ? source.y + source.height : source.y;
    const endX = targetCenterX;
    const endY = flowsDown ? target.y : target.y + target.height;
    const availableGap = Math.abs(endY - startY);
    const routeOffset = Math.min(20, availableGap / 2);
    const routeY = startY + (flowsDown ? routeOffset : -routeOffset);
    return {
      ...edge,
      path: `M ${startX} ${startY} V ${routeY} H ${endX} V ${endY}`,
      labelX: (startX + endX) / 2,
      labelY: routeY - 8,
    };
  });

  return { width, height, groups, nodes, edges };
}

function titleCase(value: string): string {
  return value
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function wrapGraphText(
  value: string,
  maxChars: number,
  maxLines = 3,
  truncate = true,
): string[] {
  const normalized = value.trim();
  if (!normalized) return [];
  const words = normalized
    .split(/\s+/)
    .flatMap((word) => {
      if (word.length <= maxChars) return [word];
      const chunks: string[] = [];
      for (let index = 0; index < word.length; index += maxChars) {
        chunks.push(word.slice(index, index + maxChars));
      }
      return chunks;
    });
  const lines: string[] = [];
  let current = "";
  for (const word of words) {
    const next = current ? `${current} ${word}` : word;
    if (next.length <= maxChars) {
      current = next;
      continue;
    }
    if (current) lines.push(current);
    current = word;
    if (truncate && lines.length === maxLines - 1) break;
  }
  if (current && (!truncate || lines.length < maxLines)) lines.push(current);
  const consumed = lines.join(" ").length;
  if (truncate && consumed < normalized.length && lines.length > 0) {
    const last = lines.length - 1;
    lines[last] = `${lines[last].slice(0, Math.max(1, maxChars - 1))}…`;
  }
  return lines;
}

function SvgTextLines({
  lines,
  x,
  y,
  lineHeight,
  textAnchor = "start",
  fontSize,
  fontWeight,
  fill,
}: {
  lines: string[];
  x: number;
  y: number;
  lineHeight: number;
  textAnchor?: "start" | "middle";
  fontSize: number;
  fontWeight?: number;
  fill: string;
}) {
  if (lines.length === 0) return null;
  return (
    <text
      x={x}
      y={y}
      textAnchor={textAnchor}
      fontSize={fontSize}
      fontWeight={fontWeight}
      fill={fill}
    >
      {lines.map((line, index) => (
        <tspan key={`${line}:${index}`} x={x} dy={index === 0 ? 0 : lineHeight}>
          {line}
        </tspan>
      ))}
    </text>
  );
}

function statusLabel(
  status: GraphStatus,
  freshness: GraphFreshness,
  lastObservedStatus: GraphStatus | null,
): string {
  return freshness === "stale"
    ? `Stale · last ${titleCase(lastObservedStatus ?? status)}`
    : titleCase(status);
}

function datumValue(value: number | null, suffix: string): string {
  return value === null ? "Unknown" : `${value.toLocaleString()}${suffix}`;
}

function mediaSummary(media: GraphMedia | null): string | null {
  if (!media) return null;
  const parts: string[] = [];
  if (media.width?.value != null && media.height?.value != null) {
    parts.push(`${media.width.value}×${media.height.value}`);
  }
  if (media.fps) parts.push(datumValue(media.fps.value, " FPS"));
  if (media.frame_count) parts.push(datumValue(media.frame_count.value, " frames"));
  return parts.length > 0 ? parts.join(" · ") : "Unknown media";
}

function mediaLines(media: GraphMedia | null): string[] {
  if (!media) return [];
  const dimensions =
    media.width?.value != null && media.height?.value != null
      ? `${media.width.value}×${media.height.value}`
      : null;
  const flow = [
    media.fps ? datumValue(media.fps.value, " FPS") : null,
    media.frame_count ? datumValue(media.frame_count.value, " frames") : null,
  ].filter((value): value is string => value !== null);
  const lines = [dimensions, flow.length > 0 ? flow.join(" · ") : null].filter(
    (value): value is string => value !== null,
  );
  return lines.length > 0 ? lines : ["Unknown media"];
}

function metricValue(metric: GraphMetric): string {
  if (metric.value === null) return "Unknown";
  return `${String(metric.value)}${metric.unit ? ` ${metric.unit}` : ""}`;
}

function edgeLabelLines(edge: GraphEdge): string[] {
  if (!edge.label || edge.label.toLowerCase() === "frames") return [];
  return wrapGraphText(edge.label, 16, 2, false);
}

function nodeMediaLines(node: GraphNode): string[] {
  const inputSummary = mediaSummary(node.input);
  const outputSummary = mediaSummary(node.output);
  if (inputSummary && outputSummary && inputSummary === outputSummary) {
    return mediaLines(node.input).map(
      (line, index) => `${index === 0 ? "In/Out " : ""}${line}`,
    );
  }
  return [
    ...mediaLines(node.input).map(
      (line, index) => `${index === 0 ? "In " : ""}${line}`,
    ),
    ...mediaLines(node.output).map(
      (line, index) => `${index === 0 ? "Out " : ""}${line}`,
    ),
  ].slice(0, 4);
}

interface NodeTextLayout {
  labelLines: string[];
  summaryLines: string[];
  metricLines: Array<{ metric: GraphMetric; lines: string[] }>;
  kindY: number;
  summaryY: number;
  metricY: number;
  height: number;
}

function nodeTextLayout(node: GraphNode): NodeTextLayout {
  const labelLines = wrapGraphText(node.label, NODE_TEXT_WIDTH, 3, false);
  const summaryLines = nodeMediaLines(node);
  const metricLines = node.metrics.slice(0, 2).map((metric) => ({
    metric,
    lines: wrapGraphText(
      `${metric.label || metric.key}: ${metricValue(metric)}`,
      NODE_TEXT_WIDTH,
      3,
      false,
    ),
  }));
  const kindY = 29 + Math.max(1, labelLines.length) * LABEL_LINE_HEIGHT;
  const summaryY = kindY + 22;
  const metricY = summaryY + Math.max(1, summaryLines.length) * SUMMARY_LINE_HEIGHT + 10;
  const metricsHeight = metricLines.reduce(
    (height, metric) => height + metric.lines.length * METRIC_LINE_HEIGHT + 4,
    0,
  );
  const contentBottom = metricLines.length > 0
    ? metricY + metricsHeight - 4
    : summaryY + Math.max(1, summaryLines.length) * SUMMARY_LINE_HEIGHT;

  return {
    labelLines,
    summaryLines,
    metricLines,
    kindY,
    summaryY,
    metricY,
    height: Math.max(NODE_MIN_HEIGHT, contentBottom + NODE_BOTTOM_PADDING),
  };
}

export function GenericGraphViewer({ graph }: { graph: GraphResponse }) {
  const layout = useMemo(() => layoutGraph(graph.graph), [graph.graph]);

  return (
    <div>
      <div className="rounded-xl border border-border bg-bg-primary">
        <svg
          viewBox={`0 0 ${layout.width} ${layout.height}`}
          width="100%"
          role="img"
          aria-label={`${graph.graph.label} graph`}
          className="block h-auto w-full"
        >
          <defs>
            <marker id="graph-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
              <path
                d="M 0 0 L 10 5 L 0 10 z"
                fill="context-stroke"
              />
            </marker>
          </defs>
          {layout.groups.map((group) => (
            <g key={group.id} aria-label={`${group.label || group.id} group`}>
              <rect
                x={group.x}
                y={group.y}
                width={group.width}
                height={group.height}
                rx={18}
                fill="transparent"
                stroke="var(--color-border-strong)"
                strokeWidth={1}
                strokeDasharray="4 4"
              />
              <text
                x={group.x + 12}
                y={group.y + 17}
                fontSize="13"
                fontWeight="600"
                fill="var(--color-text-secondary)"
              >
                {group.label || group.id}
              </text>
            </g>
          ))}
          {layout.edges.map((edge) => {
            const appearance = effectiveGraphAppearance(edge.status, edge.freshness);
            const palette = APPEARANCE[appearance];
            const labelLines = edgeLabelLines(edge);
            return (
              <g
                key={edge.id}
                aria-label={`${edge.label ?? edge.id}: ${statusLabel(edge.status, edge.freshness, edge.last_observed_status)}${edge.active ? "" : ", inactive"}`}
              >
                <path
                  d={edge.path}
                  fill="none"
                  stroke={palette.stroke}
                  strokeWidth={2}
                  strokeDasharray={!edge.active || edge.status === "inactive" || edge.freshness === "stale" ? "7 5" : undefined}
                  markerEnd="url(#graph-arrow)"
                />
                <SvgTextLines
                  lines={labelLines}
                  x={edge.labelX}
                  y={edge.labelY}
                  lineHeight={14}
                  textAnchor="middle"
                  fontSize={12}
                  fill={palette.text}
                />
              </g>
            );
          })}
          {layout.nodes.map((node) => {
            const appearance = effectiveGraphAppearance(node.status, node.freshness);
            const palette = APPEARANCE[appearance];
            const textLayout = nodeTextLayout(node);
            return (
              <g
                key={node.id}
                aria-label={`${node.label}: ${statusLabel(node.status, node.freshness, node.last_observed_status)}`}
              >
                <rect
                  x={node.x}
                  y={node.y}
                  width={node.width}
                  height={node.height}
                  rx={14}
                  fill={palette.fill}
                  stroke={palette.stroke}
                  strokeWidth={node.freshness === "stale" ? 3 : 2}
                  strokeDasharray={node.status === "inactive" || node.freshness === "stale" ? "7 5" : undefined}
                />
                <SvgTextLines
                  lines={textLayout.labelLines}
                  x={node.x + 14}
                  y={node.y + 27}
                  lineHeight={LABEL_LINE_HEIGHT}
                  fontSize={16}
                  fontWeight={600}
                  fill={palette.text}
                />
                <text x={node.x + 14} y={node.y + textLayout.kindY} fontSize="13" fill={palette.text}>
                  {titleCase(node.kind)} · {statusLabel(node.status, node.freshness, node.last_observed_status)}
                </text>
                <SvgTextLines
                  lines={textLayout.summaryLines}
                  x={node.x + 14}
                  y={node.y + textLayout.summaryY}
                  lineHeight={SUMMARY_LINE_HEIGHT}
                  fontSize={14}
                  fill={palette.text}
                />
                {textLayout.metricLines.map(({ metric, lines }, index) => (
                  <SvgTextLines
                    key={metric.key}
                    lines={lines}
                    x={node.x + 14}
                    y={
                      node.y +
                      textLayout.metricY +
                      textLayout.metricLines
                        .slice(0, index)
                        .reduce((offset, previous) => offset + previous.lines.length * METRIC_LINE_HEIGHT + 4, 0)
                    }
                    lineHeight={METRIC_LINE_HEIGHT}
                    fontSize={13}
                    fill={palette.text}
                  />
                ))}
              </g>
            );
          })}
        </svg>
      </div>
    </div>
  );
}
