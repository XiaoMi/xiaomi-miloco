import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import {
  GenericGraphViewer,
  effectiveGraphAppearance,
  layoutGraph,
  wrapGraphText,
} from "@/components/GenericGraphViewer";
import { decodeGraphResponse } from "@/lib/graphDecode";
import { graphFixture } from "./graphFixture";

function decodedGraph() {
  const result = decodeGraphResponse(graphFixture());
  if (!result.ok) throw new Error(result.error);
  return result.graph;
}

describe("GenericGraphViewer", () => {
  it("lays out arbitrary node IDs from Graph JSON without business mappings", () => {
    const graph = decodedGraph();
    const layout = layoutGraph(graph.graph);

    expect(layout.nodes.map((node) => node.id)).toEqual([
      "dynamic-a",
      "dynamic-b",
      "audio-only",
    ]);
    expect(layout.edges).toHaveLength(1);
    expect(layout.width).toBeGreaterThan(0);
    expect(layout.height).toBeGreaterThan(0);
  });

  it("never renders edge label text on connection lines", () => {
    // 连接线不带文字:即使协议带 label(供屏幕阅读器的 aria-label),SVG 画布上
    // 也不能出现 <text> 文本。fixture 的边本就带 label="frames",再显式注入
    // 一个真实 label 值钉住行为。
    const graph = decodedGraph();
    graph.graph.edges[0].label = "Audio bypass";
    const html = renderToStaticMarkup(<GenericGraphViewer graph={graph} />);
    expect(html).not.toContain(">Audio bypass<");
    expect(html).not.toContain(">frames<");
  });

  it("renders each complete group on its own left-to-right row", () => {
    const graph = decodedGraph();
    const groupSizes = [2, 2, 4, 5];
    const groups = groupSizes.map((_, index) => ({
      id: `group-${index}`,
      label: `Group ${index}`,
      order: index,
    }));
    const nodes = Array.from({ length: 13 }, (_, index) => ({
      ...graph.graph.nodes[0],
      id: `node-${index}`,
      rank: index,
      order: index,
      group: index < 2
        ? groups[0].id
        : index < 4
        ? groups[1].id
        : index < 8
        ? groups[2].id
        : groups[3].id,
    }));
    const edges = nodes.slice(1).map((node, index) => ({
      ...graph.graph.edges[0],
      id: `edge-${index}`,
      from: nodes[index].id,
      to: node.id,
    }));

    const layout = layoutGraph({ ...graph.graph, nodes, edges, groups });
    const rows = Array.from(new Set(layout.nodes.map((node) => node.y))).sort((a, b) => a - b);
    const rowNodes = rows.map((y) => layout.nodes.filter((node) => node.y === y));

    expect(rowNodes.map((row) => row.length)).toEqual([2, 2, 4, 5]);
    for (const row of rowNodes) {
      expect(row.map((node) => node.x)).toEqual(
        row.map((node) => node.x).slice().sort((left, right) => left - right),
      );
    }
    for (const group of groups) {
      expect(new Set(layout.nodes.filter((node) => node.group === group.id).map((node) => node.y)).size).toBe(1);
    }
  });

  it("wraps long labels instead of rendering one overflowing SVG line", () => {
    expect(wrapGraphText("Frames with tracking metadata", 18, 3)).toEqual([
      "Frames with",
      "tracking metadata",
    ]);
    expect(wrapGraphText("abcdefghijklmnopqrstuvwxyz", 8, 2)).toEqual([
      "abcdefgh",
      "ijklmno…",
    ]);

    const graph = decodedGraph();
    graph.graph.nodes[0].label = "A very long node label that must wrap";
    graph.graph.nodes[0].metrics = [
      {
        ...graph.graph.nodes[0].metrics[0],
        label: "Configured Max Windows",
        value: 3,
        unit: "windows",
      },
    ];
    const html = renderToStaticMarkup(<GenericGraphViewer graph={graph} />);

    expect(html).toContain("<tspan");
    expect(html).toContain("A very long node label");
    expect(html).not.toContain("…");
  });

  it("renders an accessible read-only SVG without interactive details", () => {
    const html = renderToStaticMarkup(<GenericGraphViewer graph={decodedGraph()} />);

    expect(html).toContain("<svg");
    expect(html).toContain("aria-label=\"Perception Flow graph\"");
    expect(html).not.toContain("tabindex=\"0\"");
    expect(html).not.toContain("cursor-pointer");
    expect(html).not.toContain("Trace ID:");
    expect(html).toContain("Decoded Stream");
    expect(html).toContain("Audio Encoder");
    expect(html).toContain("Inactive");
    expect(html).toContain("aria-label=\"Source group\"");
    expect(html).toContain("aria-label=\"Output group\"");
    expect(html).not.toContain("overflow-x-auto");
    expect(html).toContain('width="100%"');
    expect(html).not.toContain("max-w-md");
    expect(html).not.toContain('class="block min-w-full"');
    expect(html).toContain('fill="context-stroke"');
  });

  it("renders edge media text exactly once on the canvas", () => {
    const html = renderToStaticMarkup(<GenericGraphViewer graph={decodedGraph()} />);
    expect(decodedGraph().graph.edges[0].media?.width?.value).toBe(1920);
    expect(html.match(/1920×1080/g)).toHaveLength(1);
    expect(html).not.toContain(">frames<");
  });

  it("keeps wider five-node cards within the responsive SVG viewBox", () => {
    const graph = decodedGraph();
    const group = { id: "output", label: "Output", order: 10 };
    const nodes = Array.from({ length: 5 }, (_, index) => ({
      ...graph.graph.nodes[0],
      id: `node-${index}`,
      group: group.id,
      rank: index,
      order: index,
    }));
    const edges = nodes.slice(1).map((node, index) => ({
      ...graph.graph.edges[0],
      id: `edge-${index}`,
      from: nodes[index].id,
      to: node.id,
    }));

    const layout = layoutGraph({
      ...graph.graph,
      nodes,
      edges,
      groups: [group],
    });

    expect(layout.nodes[0].width).toBe(230);
    expect(layout.width).toBeLessThanOrEqual(1500);
  });

  it("uses the maximum content height for every card", () => {
    const graph = decodedGraph();
    const layout = layoutGraph(graph.graph);
    const heights = new Set(layout.nodes.map((node) => node.height));

    expect(heights.size).toBe(1);
    expect(layout.nodes[0].height).toBeGreaterThan(140);
  });

  it("keeps same-row arrows horizontal when card content differs", () => {
    const graph = decodedGraph();
    const group = { id: "output", label: "Output", order: 10 };
    const source = { ...graph.graph.nodes[0], id: "source", group: group.id, rank: 0 };
    const target = { ...graph.graph.nodes[1], id: "target", group: group.id, rank: 1 };
    const edge = { ...graph.graph.edges[0], id: "source-target", from: source.id, to: target.id };
    const layout = layoutGraph({ ...graph.graph, nodes: [source, target], edges: [edge], groups: [group] });
    const positionedSource = layout.nodes.find((node) => node.id === source.id)!;

    expect(layout.edges[0].path.startsWith(
      `M ${positionedSource.x + positionedSource.width} ${positionedSource.y + positionedSource.height / 2}`,
    )).toBe(true);
  });

  it("routes cross-row arrows orthogonally through the row gap", () => {
    const graph = decodedGraph();
    const groups = [
      { id: "first", label: "First", order: 10 },
      { id: "second", label: "Second", order: 20 },
    ];
    const nodes = Array.from({ length: 6 }, (_, index) => ({
      ...graph.graph.nodes[0],
      id: `node-${index}`,
      group: index < 3 ? groups[0].id : groups[1].id,
      rank: index,
      order: index,
    }));
    const edge = {
      ...graph.graph.edges[0],
      id: "cross-row",
      from: nodes[2].id,
      to: nodes[3].id,
    };

    const layout = layoutGraph({ ...graph.graph, nodes, edges: [edge], groups });
    const source = layout.nodes.find((node) => node.id === edge.from)!;
    const target = layout.nodes.find((node) => node.id === edge.to)!;
    const positionedEdge = layout.edges[0];

    expect(source.y).toBeLessThan(target.y);
    expect(positionedEdge.path).toContain(" V ");
    expect(positionedEdge.path).toContain(" H ");
    expect(positionedEdge.path).not.toContain(" C ");
  });

  it("routes non-adjacent same-row bypass edges below intermediate cards", () => {
    const graph = decodedGraph();
    const group = { id: "output", label: "Output", order: 10 };
    const nodes = Array.from({ length: 3 }, (_, index) => ({
      ...graph.graph.nodes[0],
      id: `node-${index}`,
      group: group.id,
      rank: index,
      order: index,
    }));
    const edge = {
      ...graph.graph.edges[0],
      id: "bypass",
      from: nodes[0].id,
      to: nodes[2].id,
      label: "Audio bypass",
    };

    const layout = layoutGraph({ ...graph.graph, nodes, edges: [edge], groups: [group] });
    const source = layout.nodes.find((node) => node.id === edge.from)!;
    const positionedEdge = layout.edges[0];

    expect(positionedEdge.path).toContain(" V ");
    expect(positionedEdge.path).toContain(" H ");
    expect(positionedEdge.path).toContain(String(source.y + source.height + 20));
    expect(positionedEdge.path).not.toContain(" C ");
  });

  it("distinguishes node input and output media", () => {
    const graph = decodedGraph();
    graph.graph.nodes[0].input = graph.graph.nodes[0].output;
    const html = renderToStaticMarkup(<GenericGraphViewer graph={graph} />);

    expect(html).toContain(">In/Out 1920×1080<");
    expect(html).toContain(">15 FPS · 60 frames<");
  });

  it("uses stale appearance instead of current error or backpressure colors", () => {
    expect(effectiveGraphAppearance("error", "stale")).toBe("stale");
    expect(effectiveGraphAppearance("backpressure", "stale")).toBe("stale");
    expect(effectiveGraphAppearance("error", "fresh")).toBe("error");
  });
});
