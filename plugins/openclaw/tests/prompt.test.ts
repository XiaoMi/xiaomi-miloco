import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { homeProfilePath } from "../src/home-profile/helpers.js";
import { registerBeforePromptBuildHook, resolveProfile } from "../src/hooks/prompt.js";

// catalog 走 miloco-cli，测试里 mock 掉，单独控制空/非空两条路径。
const getCatalog = vi.fn<() => Promise<string>>();
vi.mock("../src/services/catalog.js", () => ({
  getCatalog: () => getCatalog(),
}));

type HookResult = {
  prependSystemContext: string;
  appendSystemContext?: string;
};

function makeApi() {
  let handler:
    | ((
        evt: { prompt?: string } | null,
        ctx?: { sessionKey?: string; trigger?: string },
      ) => Promise<HookResult>)
    | undefined;
  const api = {
    on(_event: string, h: typeof handler) {
      handler = h;
    },
  } as any;
  return {
    api,
    run: (sessionKey?: string, opts?: { prompt?: string; trigger?: string }) =>
      handler!({ prompt: opts?.prompt }, { sessionKey, trigger: opts?.trigger }),
  };
}

describe("resolveProfile", () => {
  it.each([
    ["agent:main:miloco", "full"],
    ["agent:main:miloco-rule", "rule"],
    ["agent:main:miloco-suggest", "suggestion"],
    ["agent:main:cron:[t1]:run:abc", "minimal"],
    ["agent:main", "full"],
    ["agent:main:telegram:dm:123", "full"],
    [undefined, "full"],
  ])("%s → %s", (key, expected) => {
    expect(resolveProfile(key as string | undefined)).toBe(expected);
  });

  // isolated cron 的 sessionKey 不含 :cron:，必须靠消息前缀 / trigger 兜住，否则漏判成 full。
  it("消息带 [cron: 前缀 → minimal（即便 sessionKey 像交互式）", () => {
    expect(
      resolveProfile("agent:main:miloco", {
        prompt: "[cron:job1 miloco-perception-digest] 执行感知日志摘要。",
      }),
    ).toBe("minimal");
  });

  it("trigger=cron → minimal", () => {
    expect(resolveProfile("agent:main:miloco", { trigger: "cron" })).toBe("minimal");
  });
});

describe("before_prompt_build 组装", () => {
  let tmpHome: string;
  const prevEnv = process.env.MILOCO_HOME;

  beforeEach(() => {
    tmpHome = mkdtempSync(path.join(tmpdir(), "miloco-prompt-"));
    process.env.MILOCO_HOME = tmpHome;
    const p = homeProfilePath();
    mkdirSync(path.dirname(p), { recursive: true });
    writeFileSync(p, "# 家庭档案\n\n### 妈妈\n- 对花粉过敏", "utf8");
    getCatalog.mockReset();
    getCatalog.mockResolvedValue("");
  });

  afterEach(() => {
    if (prevEnv === undefined) delete process.env.MILOCO_HOME;
    else process.env.MILOCO_HOME = prevEnv;
    rmSync(tmpHome, { recursive: true, force: true });
  });

  it("full：能力概览 + 语音指令格式 + 家庭记忆 + 通知 + 语言；档案进 append", async () => {
    const { api, run } = makeApi();
    registerBeforePromptBuildHook(api, {} as any);
    const r = await run("agent:main:miloco");
    expect(r.prependSystemContext).toContain("## 能力概览");
    // full 列全部三种感知格式
    expect(r.prependSystemContext).toContain("语音指令");
    expect(r.prependSystemContext).toContain("事件提醒");
    expect(r.prependSystemContext).toContain("规则触发");
    expect(r.prependSystemContext).toContain("## 家庭记忆");
    expect(r.prependSystemContext).toContain("miloco-notify");
    expect(r.prependSystemContext).toContain("## 输出语言");
    expect(r.appendSystemContext).toContain("对花粉过敏");
  });

  it("rule：无能力概览，感知用规则触发格式", async () => {
    const { api, run } = makeApi();
    registerBeforePromptBuildHook(api, {} as any);
    const r = await run("agent:main:miloco-rule");
    expect(r.prependSystemContext).not.toContain("## 能力概览");
    expect(r.prependSystemContext).toContain("规则触发");
    expect(r.prependSystemContext).not.toContain("语音指令");
    expect(r.prependSystemContext).toContain("## 家庭记忆");
  });

  it("suggestion：无能力概览，感知用事件提醒格式", async () => {
    const { api, run } = makeApi();
    registerBeforePromptBuildHook(api, {} as any);
    const r = await run("agent:main:miloco-suggest");
    expect(r.prependSystemContext).not.toContain("## 能力概览");
    expect(r.prependSystemContext).toContain("事件提醒");
    expect(r.prependSystemContext).not.toContain("语音指令");
  });

  it("minimal(cron)：仅身份+通知+语言，无感知/能力/记忆，append 为空", async () => {
    const { api, run } = makeApi();
    registerBeforePromptBuildHook(api, {} as any);
    const r = await run("agent:main:cron:[t1]:run:abc");
    expect(r.prependSystemContext).toContain("Miloco");
    expect(r.prependSystemContext).toContain("miloco-notify");
    expect(r.prependSystemContext).toContain("## 输出语言");
    expect(r.prependSystemContext).not.toContain("## 感知");
    expect(r.prependSystemContext).not.toContain("## 能力概览");
    expect(r.prependSystemContext).not.toContain("## 家庭记忆");
    expect(r.appendSystemContext).toBeUndefined();
  });

  it("isolated cron（sessionKey 像交互式，但消息带 [cron: 前缀）→ minimal", async () => {
    const { api, run } = makeApi();
    registerBeforePromptBuildHook(api, {} as any);
    const r = await run("agent:main:miloco", {
      prompt: "[cron:job1 miloco-perception-digest] 执行感知日志摘要。加载 miloco-perception-digest skill。",
    });
    expect(r.prependSystemContext).not.toContain("## 能力概览");
    expect(r.prependSystemContext).not.toContain("## 感知");
    expect(r.prependSystemContext).not.toContain("## 家庭记忆");
    expect(r.appendSystemContext).toBeUndefined();
  });

  it("所有 profile（含 minimal）都注入家庭时区块，取部署时区", async () => {
    const prevTz = process.env.MILOCO_TIMEZONE;
    process.env.MILOCO_TIMEZONE = "Asia/Shanghai"; // env 优先，结果确定
    try {
      const { api, run } = makeApi();
      registerBeforePromptBuildHook(api, {} as any);
      for (const key of ["agent:main:miloco", "agent:main:cron:[t1]:run:abc"]) {
        const r = await run(key);
        expect(r.prependSystemContext).toContain("## 时间与时区");
        expect(r.prependSystemContext).toContain("Asia/Shanghai");
      }
    } finally {
      if (prevTz === undefined) delete process.env.MILOCO_TIMEZONE;
      else process.env.MILOCO_TIMEZONE = prevTz;
    }
  });

  it("catalog 非空时进 append 末；为空时整段不出现", async () => {
    const { api, run } = makeApi();
    registerBeforePromptBuildHook(api, {} as any);

    getCatalog.mockResolvedValue("# devices catalog\n# 数据格式\n...");
    const withCat = await run("agent:main:miloco");
    expect(withCat.appendSystemContext).toContain("## 设备目录");
    expect(withCat.appendSystemContext).toContain("# devices catalog");

    getCatalog.mockResolvedValue("");
    const noCat = await run("agent:main:miloco");
    expect(noCat.appendSystemContext ?? "").not.toContain("## 设备目录");
  });
});
