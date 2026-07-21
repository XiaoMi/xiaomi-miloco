import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { readFileSafe } from "../src/home-profile/helpers.js";
import { buildPendingSuggestionBlock } from "../src/home-profile/injection.js";

// 家庭档案逻辑已下沉 backend（miloco-cli home-profile）；plugin 侧不再注入 profile.md，
// 只保留 readFileSafe（今日感知日志注入等只读场景复用）与待回应习惯建议块。

let tmpHome: string;
const prevEnv = process.env.MILOCO_HOME;

beforeEach(() => {
  tmpHome = mkdtempSync(path.join(tmpdir(), "miloco-home-"));
  process.env.MILOCO_HOME = tmpHome;
});

afterEach(() => {
  if (prevEnv === undefined) delete process.env.MILOCO_HOME;
  else process.env.MILOCO_HOME = prevEnv;
  rmSync(tmpHome, { recursive: true, force: true });
});

describe("readFileSafe", () => {
  it("存在文件返回内容", () => {
    const p = path.join(tmpHome, "some.md");
    writeFileSync(p, "hello world", "utf8");
    expect(readFileSafe(p)).toBe("hello world");
  });

  it("缺失文件返回空串（不抛错）", () => {
    expect(readFileSafe(path.join(tmpHome, "nope.md"))).toBe("");
  });
});

describe("buildPendingSuggestionBlock", () => {
  it("无未决习惯建议时返回空串（正常日子静默）", () => {
    expect(buildPendingSuggestionBlock()).toBe("");
  });
});
