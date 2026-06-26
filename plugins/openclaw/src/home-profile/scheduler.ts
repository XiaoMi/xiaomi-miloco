import type { OpenClawPluginApi } from "openclaw/plugin-sdk/core";
import type { PluginHookGatewayCronService } from "openclaw/plugin-sdk/plugin-runtime";

const MANAGED_TAG = "[miloco:home-profile]";

let cachedCron: PluginHookGatewayCronService | undefined;

const kCronTasks = [
  {
    name: "miloco-perception-digest",
    description: "感知引擎日志摘要/压缩",
    schedule: {
      kind: "cron",
      expr: "*/15 * * * *",
      tz: "Asia/Shanghai",
    },
    payload: {
      kind: "agentTurn",
      lightContext: true,
      message:
        "执行感知日志摘要。加载 miloco-perception-digest skill 进行处理。",
    },
  },
  {
    name: "miloco-home-patrol",
    description: "家庭记忆/习惯巡检",
    schedule: {
      kind: "cron",
      expr: "*/30 * * * *",
      tz: "Asia/Shanghai",
    },
    payload: {
      kind: "agentTurn",
      lightContext: false,
      message:
        "执行家庭巡检。加载 miloco-home-patrol skill 进行巡检。每次巡检都是隔离会话，务必先读巡检日志接上次进度、只处理上次处理时间之后的新事件，巡检完把处理记录追加回日志，避免重复提醒 / 重复操作。",
    },
  },
  {
    name: "miloco-home-dreaming",
    description: "家庭记忆 Dreaming（Observe → Promote → Prune）",
    schedule: {
      kind: "cron",
      expr: "0 0 * * *",
      tz: "Asia/Shanghai",
    },
    payload: {
      kind: "agentTurn",
      lightContext: true,
      message: `执行 home-dreaming 流程。依次完成以下步骤：
1. **Observe** — 加载 miloco-home-observe skill，从感知/交互记忆中提取新知识写入候选区
2. **Promote** — 加载 miloco-home-promote skill，将候选区中达到条件的知识提升到正式档案
3. **Prune** — 加载 miloco-home-prune skill，统一主体命名、清理过期数据、提交持久化

执行规则：按顺序依次执行不可跳过。Step 1 没有新知识时仍需执行 Step 2（处理已有候选的提升）。`,
    },
  },
  {
    name: "miloco-habit-suggest",
    description: "每日习惯洞察 → 推荐建任务",
    schedule: {
      kind: "cron",
      expr: "0 10 * * *",
      tz: "Asia/Shanghai",
    },
    payload: {
      kind: "agentTurn",
      lightContext: true,
      message:
        "执行每日习惯洞察。加载 miloco-habit-suggest skill，按【路径 A · 扫描推荐】处理：从家庭档案识别值得建成任务的习惯，至多主动推荐一条。",
    },
  },
];

async function reconcile(cron: PluginHookGatewayCronService): Promise<void> {
  const existing = await cron.list({ includeDisabled: true });
  const managed = existing.filter((j) => j.description?.includes(MANAGED_TAG));

  for (const task of kCronTasks) {
    const target = {
      enabled: true,
      wakeMode: "now",
      sessionTarget: "isolated",
      delivery: { mode: "none" },
      ...task,
      description: `${MANAGED_TAG} ${task.name}`,
    };

    const found = managed.find((j) => j.name === task.name);

    if (!found) {
      await cron.add(target);
    } else {
      await cron.update(found.id, target);
    }
  }

  const validNames = new Set(kCronTasks.map((j) => j.name));
  for (const task of managed) {
    if (!validNames.has(task.name ?? "")) {
      await cron.remove(task.id);
    }
  }
}

async function teardown(cron: PluginHookGatewayCronService): Promise<void> {
  const existing = await cron.list({ includeDisabled: true });
  const managed = existing.filter((j) => j.description?.includes(MANAGED_TAG));
  for (const task of managed) {
    await cron.remove(task.id);
  }
}

export function registerHomeProfileScheduler(api: OpenClawPluginApi): void {
  api.on("gateway_start", async (_, ctx) => {
    const cron = ctx.getCron?.();
    if (!cron) return;
    cachedCron = cron;
    await reconcile(cron);
  });

  api.on("gateway_stop", async () => {
    if (!cachedCron) return;
    await teardown(cachedCron);
    cachedCron = undefined;
  });
}
