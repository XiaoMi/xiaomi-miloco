import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { habitSuggestionsPath } from "../src/home-profile/helpers.js";
import {
  buildPendingSuggestionBlock,
  loadOpenQuestions,
} from "../src/home-profile/injection.js";
import { writeJsonFileSync } from "../src/utils/io.js";

/**
 * 习惯建议状态机已移入 miloco-cli；openclaw 侧只剩 prompt 注入的只读 reader。
 * 这里只验证 reader 口径：读同一个 task-suggestions.json，仅未过期的 asked 条目入注入块。
 */

let tmpHome: string;
const prevHomeEnv = process.env.MILOCO_HOME;
const prevTzEnv = process.env.MILOCO_TIMEZONE;

// asked_at 用 +08:00 后缀，过期口径只在 Asia/Shanghai 稳定。
const D6_10 = "2026-06-06T10:00:00+08:00";

beforeEach(() => {
  tmpHome = mkdtempSync(path.join(tmpdir(), "miloco-injection-"));
  process.env.MILOCO_HOME = tmpHome;
  process.env.MILOCO_TIMEZONE = "Asia/Shanghai";
});

afterEach(() => {
  if (prevHomeEnv === undefined) delete process.env.MILOCO_HOME;
  else process.env.MILOCO_HOME = prevHomeEnv;
  if (prevTzEnv === undefined) delete process.env.MILOCO_TIMEZONE;
  else process.env.MILOCO_TIMEZONE = prevTzEnv;
  rmSync(tmpHome, { recursive: true, force: true });
});

function seed(entries: unknown[]): void {
  writeJsonFileSync(
    habitSuggestionsPath(),
    { version: 1, entries },
    { pretty: true },
  );
}

describe("buildPendingSuggestionBlock", () => {
  it("无 store 文件 → 空串（正常日子静默）", () => {
    expect(buildPendingSuggestionBlock()).toBe("");
  });

  it("只有 pending / 已 resolve 的条目 → 空串（只认 asked）", () => {
    seed([
      { key: "a", title: "A", suggestion: "sa", status: "pending" },
      { key: "b", title: "B", suggestion: "sb", status: "created" },
      { key: "c", title: "C", suggestion: "sc", status: "rejected" },
    ]);
    expect(buildPendingSuggestionBlock()).toBe("");
  });

  it("未过期的 asked 条目 → 注入块含其 key/title/suggestion 与 resolve 引导", () => {
    // asked_at 取当下（buildPendingSuggestionBlock 用真实 now 判龄），确保未过期。
    const fresh = new Date().toISOString();
    seed([{ key: "wl_gym", title: "傍晚健身", suggestion: "健身放歌单", status: "asked", asked_at: fresh }]);
    const block = buildPendingSuggestionBlock();
    expect(block).toContain("等用户回应的习惯建议");
    expect(block).toContain("[wl_gym] 傍晚健身：健身放歌单");
    // 引导用户走 CLI resolve，而非旧 tool
    expect(block).toContain("miloco-cli habit resolve");
    expect(block).not.toContain("miloco_habit_suggest");
  });

  it("asked 超 7 天 → 视为过期，不入注入块", () => {
    seed([{ key: "old", title: "旧", suggestion: "s", status: "asked", asked_at: D6_10 }]);
    // asked_at 距今已远超 7 天（测试运行时刻）→ 空串
    expect(buildPendingSuggestionBlock()).toBe("");
  });
});

describe("loadOpenQuestions 7 天边界（注入 now 精确验证）", () => {
  // asked_at 固定 D6_10；用注入的 now 精确卡在 7 天两侧，防 <= / < off-by-one 回归。
  const exactly7 = "2026-06-13T10:00:00+08:00"; // 恰好 7*86_400_000 ms → 含
  const justOver = "2026-06-13T10:00:00.001+08:00"; // 超 1ms → 排除

  beforeEach(() => {
    seed([
      { key: "wl_gym", title: "健身", suggestion: "放歌单", status: "asked", asked_at: D6_10 },
    ]);
  });

  it("恰好 7 天（== STALE_MS）仍算未过期 → 含", () => {
    expect(loadOpenQuestions(exactly7)).toHaveLength(1);
  });

  it("超过 7 天 1ms → 排除", () => {
    expect(loadOpenQuestions(justOver)).toHaveLength(0);
  });

  it("非 asked 状态（pending/created）永不入 open questions", () => {
    seed([
      { key: "p", title: "P", suggestion: "s", status: "pending", asked_at: D6_10 },
      { key: "c", title: "C", suggestion: "s", status: "created", asked_at: D6_10 },
    ]);
    expect(loadOpenQuestions(exactly7)).toHaveLength(0);
  });
});
