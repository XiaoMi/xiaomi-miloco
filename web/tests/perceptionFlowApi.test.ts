import { afterEach, describe, expect, it, vi } from "vitest";

import { getPerceptionFlow } from "@/api";
import { graphFixture } from "./graphFixture";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("getPerceptionFlow", () => {
  it("fetches unknown JSON, encodes device scope, and returns decoded graph data", async () => {
    const calls: string[] = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      calls.push(typeof input === "string" ? input : input.toString());
      return new Response(JSON.stringify(graphFixture()), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    globalThis.fetch = fetchMock as unknown as typeof fetch;

    const result = await getPerceptionFlow("camera / 1");

    expect(calls[0]).toBe(
      "/api/perf/perception-flow?device_id=camera%20%2F%201",
    );
    expect(result.graph.graph.nodes[0].id).toBe("dynamic-a");
  });

  it("rejects a local protocol error without exposing malformed data", async () => {
    globalThis.fetch = vi.fn(async () =>
      new Response(JSON.stringify({ schema_version: 2 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ) as unknown as typeof fetch;

    await expect(getPerceptionFlow()).rejects.toThrow(
      "Unsupported graph schema version: 2",
    );
  });
});
