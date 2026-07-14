import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  readOnboardingState,
  writeOnboardingInviteState,
} from "../src/home-profile/onboarding_state.js";
import { registerBeforePromptBuildHook, resolveProfile } from "../src/hooks/prompt.js";
import { toLocalParts } from "../src/utils/time.js";

// 感知日志文件名日期取部署时区；测试固定 tz 后按同一逻辑算出某个偏移日的文件名。
function perceptionFile(workspaceDir: string, tz: string, dayOffset = 0): string {
  const iso = new Date(Date.now() + dayOffset * 24 * 60 * 60 * 1000).toISOString();
  const p = toLocalParts(iso, tz);
  if (!p) throw new Error("perceptionFile: bad parts");
  const pad2 = (n: number) => String(n).padStart(2, "0");
  const date = `${p.y}-${pad2(p.m)}-${pad2(p.d)}`;
  return path.join(workspaceDir, "memory", `${date}-miloco-perception.md`);
}

function writePerception(file: string, body: string): void {
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, body, "utf8");
}

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
        ctx?: { sessionKey?: string; trigger?: string; workspaceDir?: string },
      ) => Promise<HookResult>)
    | undefined;
  const api = {
    on(_event: string, h: typeof handler) {
      handler = h;
    },
  } as any;
  return {
    api,
    run: (
      sessionKey?: string,
      opts?: { prompt?: string; trigger?: string; workspaceDir?: string },
    ) =>
      handler!(
        { prompt: opts?.prompt },
        { sessionKey, trigger: opts?.trigger, workspaceDir: opts?.workspaceDir },
      ),
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
  let tmpWorkspace: string;
  const prevHome = process.env.MILOCO_HOME;
  const prevTz = process.env.MILOCO_TIMEZONE;

  beforeEach(() => {
    tmpHome = mkdtempSync(path.join(tmpdir(), "miloco-prompt-"));
    process.env.MILOCO_HOME = tmpHome;
    // 固定部署时区，使今日感知日志文件名可确定复现。
    process.env.MILOCO_TIMEZONE = "Asia/Shanghai";
    // 工作区：写入今日感知日志，供 append 注入。
    tmpWorkspace = mkdtempSync(path.join(tmpdir(), "miloco-ws-"));
    writePerception(
      perceptionFile(tmpWorkspace, "Asia/Shanghai"),
      "# 2026-01-01 感知记忆\n\n- 09:00–11:30 书房 · 戴眼镜男性：在电脑前工作",
    );
    getCatalog.mockReset();
    getCatalog.mockResolvedValue("");
  });

  afterEach(() => {
    if (prevHome === undefined) delete process.env.MILOCO_HOME;
    else process.env.MILOCO_HOME = prevHome;
    if (prevTz === undefined) delete process.env.MILOCO_TIMEZONE;
    else process.env.MILOCO_TIMEZONE = prevTz;
    rmSync(tmpHome, { recursive: true, force: true });
    rmSync(tmpWorkspace, { recursive: true, force: true });
  });

  it("full：能力概览 + 语音指令格式 + 家庭记忆 + 通知 + 语言；今日感知日志进 append", async () => {
    const { api, run } = makeApi();
    registerBeforePromptBuildHook(api, {} as any);
    const r = await run("agent:main:miloco", { workspaceDir: tmpWorkspace });
    expect(r.prependSystemContext).toContain("## 能力概览");
    // full 列全部三种感知格式
    expect(r.prependSystemContext).toContain("语音指令");
    expect(r.prependSystemContext).toContain("事件提醒");
    expect(r.prependSystemContext).toContain("规则触发");
    expect(r.prependSystemContext).toContain("## 家庭记忆");
    expect(r.prependSystemContext).toContain("miloco-notify");
    expect(r.prependSystemContext).toContain("## 输出语言");
    // 今日感知日志整段注入 append
    expect(r.appendSystemContext).toContain("## 今日感知日志");
    expect(r.appendSystemContext).toContain("戴眼镜男性：在电脑前工作");
    // 日志首行冗余 H1（`# 感知记忆`）被剥掉，不应作为与段头同级的 H2 兄弟节点出现
    expect(r.appendSystemContext).not.toContain("## 感知记忆");
  });

  it("拿不到 workspaceDir → 今日感知日志段不出现", async () => {
    const { api, run } = makeApi();
    registerBeforePromptBuildHook(api, {} as any);
    const r = await run("agent:main:miloco");
    expect(r.appendSystemContext ?? "").not.toContain("## 今日感知日志");
  });

  it("当天和昨天都没有感知日志文件 → 该段不出现", async () => {
    const emptyWs = mkdtempSync(path.join(tmpdir(), "miloco-ws-empty-"));
    try {
      const { api, run } = makeApi();
      registerBeforePromptBuildHook(api, {} as any);
      const r = await run("agent:main:miloco", { workspaceDir: emptyWs });
      expect(r.appendSystemContext ?? "").not.toContain("感知日志");
    } finally {
      rmSync(emptyWs, { recursive: true, force: true });
    }
  });

  it("当天无日志但昨天有 → 回退为「最近感知日志」，不谎称今日", async () => {
    const ws = mkdtempSync(path.join(tmpdir(), "miloco-ws-y-"));
    try {
      writePerception(
        perceptionFile(ws, "Asia/Shanghai", -1),
        "# 2025-12-31 感知记忆\n\n- 20:00–21:00 客厅 · 全家：一起看电视",
      );
      const { api, run } = makeApi();
      registerBeforePromptBuildHook(api, {} as any);
      const r = await run("agent:main:miloco", { workspaceDir: ws });
      expect(r.appendSystemContext).toContain("## 最近感知日志");
      expect(r.appendSystemContext).not.toContain("## 今日感知日志");
      expect(r.appendSystemContext).toContain("一起看电视");
    } finally {
      rmSync(ws, { recursive: true, force: true });
    }
  });

  it("当天文件只有 H1、无正文 → 回退到昨天，不注入空段", async () => {
    const ws = mkdtempSync(path.join(tmpdir(), "miloco-ws-h1-"));
    try {
      // digest 建了当天文件、写下 H1，却把这批日志全判为该丢弃 → 仅剩 H1。
      writePerception(perceptionFile(ws, "Asia/Shanghai"), "# 2026-01-02 感知记忆\n");
      writePerception(
        perceptionFile(ws, "Asia/Shanghai", -1),
        "# 2026-01-01 感知记忆\n\n- 20:00–21:00 客厅 · 全家：一起看电视",
      );
      const { api, run } = makeApi();
      registerBeforePromptBuildHook(api, {} as any);
      const r = await run("agent:main:miloco", { workspaceDir: ws });
      expect(r.appendSystemContext).toContain("## 最近感知日志");
      expect(r.appendSystemContext).not.toContain("## 今日感知日志");
      expect(r.appendSystemContext).toContain("一起看电视");
    } finally {
      rmSync(ws, { recursive: true, force: true });
    }
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

  it("被邀请会话中的普通设备指令不会抢锁 onboarding", async () => {
    writeOnboardingInviteState(["wechat:s1", "telegram:s2"]);
    const { api, run } = makeApi();
    registerBeforePromptBuildHook(api, {} as any);

    const r = await run("wechat:s1", {
      prompt: "帮我把空调关了",
      workspaceDir: tmpWorkspace,
    });

    expect(r.appendSystemContext ?? "").not.toContain("Onboarding 会话收敛");
    expect(readOnboardingState()?.lockedSessionKey).toBeUndefined();
  });

  it("被邀请会话明确回应 onboarding 邀请时才写入锁", async () => {
    writeOnboardingInviteState(["wechat:s1", "telegram:s2"]);
    const { api, run } = makeApi();
    registerBeforePromptBuildHook(api, {} as any);

    const r = await run("telegram:s2", {
      prompt: "好的，开始登记吧",
      workspaceDir: tmpWorkspace,
    });

    expect(r.appendSystemContext).toContain("当前会话已被锁定为正在继续的 onboarding 会话");
    expect(readOnboardingState()?.lockedSessionKey).toBe("telegram:s2");
  });

  it("已锁到另一会话时给柔性收敛提示，不硬性要求切回", async () => {
    writeOnboardingInviteState(["wechat:s1", "telegram:s2"]);
    const { api, run } = makeApi();
    registerBeforePromptBuildHook(api, {} as any);
    await run("wechat:s1", {
      prompt: "好的，开始登记吧",
      workspaceDir: tmpWorkspace,
    });

    const r = await run("telegram:s2", {
      prompt: "我就在这里继续初始化",
      workspaceDir: tmpWorkspace,
    });

    expect(r.appendSystemContext).toContain("可能已在另一条 IM 会话中开始");
    expect(r.appendSystemContext).toContain("可直接在本会话继续");
    expect(r.appendSystemContext).toContain("不要强行要求切回");
    expect(readOnboardingState()?.lockedSessionKey).toBe("wechat:s1");
  });

  it("已锁到本会话后，普通闲聊不会继续注入 onboarding 收敛块", async () => {
    writeOnboardingInviteState(["wechat:s1", "telegram:s2"]);
    const { api, run } = makeApi();
    registerBeforePromptBuildHook(api, {} as any);
    await run("telegram:s2", {
      prompt: "好的，开始登记吧",
      workspaceDir: tmpWorkspace,
    });

    const r = await run("telegram:s2", {
      prompt: "现在几点",
      workspaceDir: tmpWorkspace,
    });

    expect(r.appendSystemContext ?? "").not.toContain("Onboarding 会话收敛");
    expect(readOnboardingState()?.lockedSessionKey).toBe("telegram:s2");
  });

  it("已锁到本会话后，裸肯定短句不会继续注入 onboarding 收敛块", async () => {
    writeOnboardingInviteState(["wechat:s1", "telegram:s2"]);
    const { api, run } = makeApi();
    registerBeforePromptBuildHook(api, {} as any);
    await run("telegram:s2", {
      prompt: "好的",
      workspaceDir: tmpWorkspace,
    });

    const r = await run("telegram:s2", {
      prompt: "好的",
      workspaceDir: tmpWorkspace,
    });

    expect(r.appendSystemContext ?? "").not.toContain("Onboarding 会话收敛");
    expect(readOnboardingState()?.lockedSessionKey).toBe("telegram:s2");
  });

  it("已锁到本会话后，明确继续初始化仍会注入 onboarding 收敛块", async () => {
    writeOnboardingInviteState(["wechat:s1", "telegram:s2"]);
    const { api, run } = makeApi();
    registerBeforePromptBuildHook(api, {} as any);
    await run("telegram:s2", {
      prompt: "好的",
      workspaceDir: tmpWorkspace,
    });

    const r = await run("telegram:s2", {
      prompt: "继续初始化",
      workspaceDir: tmpWorkspace,
    });

    expect(r.appendSystemContext).toContain("当前会话已被锁定为正在继续的 onboarding 会话");
    expect(readOnboardingState()?.lockedSessionKey).toBe("telegram:s2");
  });

  it("已锁到另一会话后，普通设备指令不会注入 onboarding 收敛块", async () => {
    writeOnboardingInviteState(["wechat:s1", "telegram:s2"]);
    const { api, run } = makeApi();
    registerBeforePromptBuildHook(api, {} as any);
    await run("telegram:s2", {
      prompt: "好的，开始登记吧",
      workspaceDir: tmpWorkspace,
    });

    const r = await run("wechat:s1", {
      prompt: "帮我把客厅灯打开",
      workspaceDir: tmpWorkspace,
    });

    expect(r.appendSystemContext ?? "").not.toContain("Onboarding 会话收敛");
    expect(readOnboardingState()?.lockedSessionKey).toBe("telegram:s2");
  });
});
