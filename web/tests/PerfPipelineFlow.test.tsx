import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it } from "vitest";

import {
  PerfPipelineFlowView,
  firstPerceptionFlowDeviceId,
  perceptionFlowRefreshDelayMs,
  perceptionFlowRequestDeps,
  scopeMatchesSelection,
} from "@/components/PerfPipelineFlow";
import { decodeGraphResponse } from "@/lib/graphDecode";
import i18n from "@/i18n";
import { graphFixture } from "./graphFixture";

function decodedGraph() {
  const result = decodeGraphResponse(graphFixture());
  if (!result.ok) throw new Error(result.error);
  return result.graph;
}

describe("PerfPipelineFlow", () => {
  afterEach(async () => {
    await i18n.changeLanguage("zh");
  });

  it("localizes the card shell while keeping Graph JSON content unchanged", async () => {
    await i18n.changeLanguage("zh");
    const html = renderToStaticMarkup(
      <PerfPipelineFlowView
        graph={decodedGraph()}
        loading={false}
        error={undefined}
        protocolWarnings={[
          {
            code: "unknown_kind",
            message: "Unknown node kind was rendered as generic.",
            path: "graph.nodes[0].kind",
            value: "future-kind",
          },
        ]}
        devices={decodedGraph().summary.devices}
        selectedDeviceId="camera-1"
        onDeviceChange={() => {}}
        onRefresh={() => {}}
      />,
    );

    expect(html).toContain("感知流水线");
    expect(html).toContain("当前进程内的流向、缓冲、分辨率、帧率与状态。");
    expect(html).not.toContain("全部设备");
    expect(html).toContain("刷新流向图");
    expect(html).toContain("协议警告");
    expect(html).toContain("Living room");
    expect(html).toContain("Decoded Stream");
  });

  it("renders the card shell in English when English is selected", async () => {
    await i18n.changeLanguage("en");
    const html = renderToStaticMarkup(
      <PerfPipelineFlowView
        graph={decodedGraph()}
        loading={false}
        error={undefined}
        protocolWarnings={[]}
        devices={decodedGraph().summary.devices}
        selectedDeviceId="camera-1"
        onDeviceChange={() => {}}
        onRefresh={() => {}}
      />,
    );

    expect(html).toContain("Perception Pipeline");
    expect(html).toContain("Current in-memory flow, buffers, resolution, frame rate, and status.");
    expect(html).not.toContain("All devices");
  });

  it("selects the first runtime device by default", () => {
    expect(firstPerceptionFlowDeviceId(decodedGraph().summary.devices)).toBe("camera-1");
    expect(firstPerceptionFlowDeviceId([])).toBeUndefined();
  });

  it("matches global scope (null) against the unselected state (undefined)", () => {
    // 无设备时后端返回全局图 scope.device_id=null、选中态保持 undefined,
    // 两者必须视为匹配,否则全局图永不显示。
    expect(scopeMatchesSelection(null, undefined)).toBe(true);
    expect(scopeMatchesSelection(undefined, undefined)).toBe(true);
    expect(scopeMatchesSelection(null, "camera-1")).toBe(false);
    expect(scopeMatchesSelection("camera-1", undefined)).toBe(false);
    expect(scopeMatchesSelection("camera-1", "camera-1")).toBe(true);
  });

  it("keeps runtime flow refresh independent from the historical window", () => {
    expect(perceptionFlowRequestDeps(undefined, 3, 30)).toEqual([
      "all",
      3,
      30,
    ]);
    expect(perceptionFlowRequestDeps("camera-1", 4, 5)).toEqual([
      "camera-1",
      4,
      5,
    ]);
    expect(perceptionFlowRefreshDelayMs(30)).toBe(30_000);
  });

});
