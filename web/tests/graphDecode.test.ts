import { describe, expect, it } from "vitest";

import { decodeGraphResponse } from "@/lib/graphDecode";
import { graphFixture } from "./graphFixture";

describe("decodeGraphResponse", () => {
  it("decodes dynamic topology, inactive edges, stale evidence, and audio-only media", () => {
    const fixture = graphFixture();
    fixture.graph.nodes[0].freshness = "stale";
    fixture.graph.nodes[0].last_observed_status = "error";
    const result = decodeGraphResponse(fixture);

    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.graph.graph.nodes.map((node) => node.id)).toEqual([
      "dynamic-a",
      "dynamic-b",
      "audio-only",
    ]);
    expect(result.graph.graph.edges[0].active).toBe(false);
    expect(result.graph.graph.edges[0].media?.width?.value).toBe(1920);
    expect(result.graph.graph.nodes[0].last_observed_status).toBe("error");
    expect(result.graph.graph.nodes[2].output).toBeNull();
    expect(result.graph.graph.nodes[2].metrics[0].value).toBe("m4a");
  });

  it("normalizes unknown kind and status with protocol warnings", () => {
    const fixture = graphFixture();
    fixture.graph.nodes[0].kind = "future-transform";
    fixture.graph.nodes[0].status = "degraded";
    fixture.graph.edges[0].status = "paused";

    const result = decodeGraphResponse(fixture);

    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.graph.graph.nodes[0]).toMatchObject({
      kind: "generic",
      status: "unknown",
    });
    expect(result.graph.graph.edges[0].status).toBe("unknown");
    expect(result.warnings.map((warning) => warning.code)).toEqual([
      "unknown_kind",
      "unknown_status",
      "unknown_status",
    ]);
  });

  it.each([
    ["malformed response", null, "Graph response must be an object"],
    [
      "unsupported schema",
      { ...graphFixture(), schema_version: 2 },
      "Unsupported graph schema version: 2",
    ],
    [
      "duplicate node id",
      (() => {
        const fixture = graphFixture();
        fixture.graph.nodes[1].id = fixture.graph.nodes[0].id;
        return fixture;
      })(),
      "Duplicate node id: dynamic-a",
    ],
    [
      "dangling edge",
      (() => {
        const fixture = graphFixture();
        fixture.graph.edges[0].to = "missing";
        return fixture;
      })(),
      "Edge dynamic-edge references an unknown node",
    ],
    [
      "cycle",
      (() => {
        const fixture = graphFixture();
        fixture.graph.edges.push({
          ...fixture.graph.edges[0],
          id: "back-edge",
          from: "dynamic-b",
          to: "dynamic-a",
          active: true,
        });
        return fixture;
      })(),
      "Graph contains a cycle",
    ],
    [
      "disconnected active node",
      (() => {
        const fixture = graphFixture();
        fixture.graph.nodes.push({
          ...fixture.graph.nodes[0],
          id: "disconnected",
          group: fixture.graph.nodes[0].group,
          standalone: false,
        });
        return fixture;
      })(),
      "Active node is not on a source-to-sink path: disconnected",
    ],
  ])("returns a local protocol error for %s", (_name, value, message) => {
    expect(decodeGraphResponse(value)).toEqual({ ok: false, error: message });
  });

  it("rejects graphs above the node limit", () => {
    const fixture = graphFixture();
    fixture.graph.nodes = Array.from({ length: 65 }, (_, index) => ({
      ...fixture.graph.nodes[1],
      id: `node-${index}`,
      order: index,
    }));
    fixture.graph.edges = [];

    expect(decodeGraphResponse(fixture)).toEqual({
      ok: false,
      error: "graph.nodes exceeds limit 64",
    });
  });

  it("rejects fractional integer media data", () => {
    const fixture = graphFixture();
    fixture.graph.nodes[0].output!.frame_count.value = 1.5;

    expect(decodeGraphResponse(fixture)).toEqual({
      ok: false,
      error: "graph.nodes[0].output.frame_count.value must be an integer",
    });
  });
});
