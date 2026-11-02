/**
 * 模型档案列表只展示高频、非敏感信息。
 *
 * Base URL 与 API Key 仍保留在新增/编辑表单中，但不能重新出现在列表；调用顺位与
 * 操作必须排在状态前，保证窄屏优先看到可操作信息。这里直接钉住表格源码结构，避免
 * 纯样式调整悄悄把敏感列带回来。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const source = readFileSync(
  fileURLToPath(new URL("../src/components/UsageOmniConfig.tsx", import.meta.url)),
  "utf8",
);
const table = source.match(/<table[\s\S]*?<\/table>/)?.[0] ?? "";
const header = table.match(/<thead>[\s\S]*?<\/thead>/)?.[0] ?? "";

describe("Omni 模型档案列表列布局", () => {
  it("按名称、模型、调用顺位、操作、状态的顺序展示", () => {
    const keys = [...header.matchAll(/t\("(usage\.[^"]+)"\)/g)].map((match) => match[1]);
    expect(keys).toEqual([
      "usage.colName",
      "usage.colModel",
      "usage.fallbackColumn",
      "usage.colAction",
      "usage.colStatus",
    ]);
    expect(table).toContain("colSpan={5}");
  });

  it("列表不直接渲染 Base URL 或打码后的 API Key", () => {
    expect(table).not.toContain("usage.baseUrlLabel");
    expect(table).not.toContain("usage.colApiKey");
    expect(table).not.toContain("p.base_url");
    expect(table).not.toContain("p.api_key_masked");
  });
});
