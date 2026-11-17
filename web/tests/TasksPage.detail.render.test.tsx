/**
 * 任务详情抽屉的渲染契约：多规则不丢、前提挂一次、前提在编辑态可改。
 *
 * 用 renderToStaticMarkup 在 node 环境渲成字符串断言结构，不需要 jsdom。
 * 静态渲染没有事件与状态，所以这里守不住「点编辑按钮之后」——编辑态是 props
 * 直接传进去的，只证明那一档的渲染分支本身对。
 *
 * 断言一律定位到具体文本来数次数，不用裸 toContain 扫全文：同页可能有多个
 * 「触发条件」标签，扫描式的断言会被别的元素满足。
 */

import { describe, it, expect } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import "@/i18n"; // 初始化 i18next 单例（组件里的 useTranslation 依赖它）
import i18n from "@/i18n";
import { GuardBlock, TaskDetailSheet } from "@/components/TasksPage";
import type { ActionCatalog } from "@/lib/taskAction";
import type { Task, TaskRuleBrief } from "@/lib/types";

const count = (haystack: string, needle: string): number =>
  haystack.split(needle).length - 1;

function rule(over: Partial<TaskRuleBrief> & { ruleId: string }): TaskRuleBrief {
  return {
    name: `[t1] ${over.ruleId}`,
    enabled: true,
    query: "条件",
    direction: "enter",
    sourceType: "omni",
    actionsDesc: [],
    ...over,
  };
}

const CATALOG: ActionCatalog = {
  devices: new Map(),
  scenes: new Map(),
  ready: false,
  t: (key) => key,
};

/** 带一台音箱的目录：动作行的设备名 / 能力名都靠它翻译。 */
const DEVICE_CATALOG: ActionCatalog = {
  devices: new Map([
    [
      "d1",
      {
        did: "d1",
        name: "小爱音箱",
        room: "客厅",
        spec: {
          "action.6.1": {
            description: "音箱 播放文本",
            prop_description: "play-text",
            type_name: "play-text",
            service_description: "音箱",
            in_params: [{ name: "text", format: "string" }],
          },
        },
      },
    ],
  ]),
  scenes: new Map(),
  ready: true,
  // 动作行会用到 actions.* 的文案键（引号 / 分隔符），这里必须过真 i18n
  // 才能断言实际渲染出的字符；桩会把 key 原样吐出来。
  t: (key, opts) => String(i18n.t(key, opts as never)),
};

function task(
  ruleBriefs: TaskRuleBrief[],
  actions: Partial<Task["actions"]> = {},
): Task {
  return {
    taskId: "t1",
    description: "书房无人时关闭空调",
    status: "active",
    pausedAt: null,
    createdAt: "2026-01-01T00:00:00+08:00",
    record: null,
    runtimeState: "on",
    ruleBriefs,
    actions: {
      onEnterDesc: null,
      onExitDesc: null,
      onTargetDesc: null,
      onEnterActions: [],
      onExitActions: [],
      onTargetActions: [],
      ...actions,
    },
  };
}

const noop = () => {};

describe("TaskDetailSheet · 规则分块", () => {
  it("同方向的两条进入规则各自渲染，不折叠", () => {
    const html = renderToStaticMarkup(
      <TaskDetailSheet
        task={task([
          rule({ ruleId: "e1", query: "有人进入客厅" }),
          rule({ ruleId: "e2", query: "门磁被打开" }),
        ])}
        catalog={CATALOG}
        onClose={noop}
        onChanged={noop}
      />,
    );
    // 条件与规则名都要在——折叠成一条时第二条两样都会消失
    expect(count(html, "有人进入客厅")).toBe(1);
    expect(count(html, "门磁被打开")).toBe(1);
    expect(count(html, "[t1] e1")).toBe(1);
    expect(count(html, "[t1] e2")).toBe(1);
  });

  it("前提只挂一次，不随进入规则数量重复", () => {
    const html = renderToStaticMarkup(
      <TaskDetailSheet
        task={task([
          rule({ ruleId: "e1", query: "有人进入客厅" }),
          rule({ ruleId: "e2", query: "门磁被打开" }),
          rule({ ruleId: "g1", direction: "guard", sourceType: "iot", query: "客厅灯开着" }),
        ])}
        catalog={CATALOG}
        onClose={noop}
        onChanged={noop}
      />,
    );
    expect(count(html, "客厅灯开着")).toBe(1);
    // 前提是 task 级闸门：必须排在所有进入规则之前，不能只挂在第一条规则里
    const guardAt = html.indexOf("客厅灯开着");
    expect(guardAt).toBeGreaterThan(-1);
    expect(guardAt).toBeLessThan(html.indexOf("有人进入客厅"));
    expect(guardAt).toBeLessThan(html.indexOf("门磁被打开"));
  });

  it("会话规则与退出规则各自成块", () => {
    const html = renderToStaticMarkup(
      <TaskDetailSheet
        task={task([
          rule({ ruleId: "s1", direction: "session", query: "有人坐在椅子上" }),
          rule({ ruleId: "x1", direction: "exit", query: "客厅无人" }),
        ])}
        catalog={CATALOG}
        onClose={noop}
        onChanged={noop}
      />,
    );
    expect(count(html, "有人坐在椅子上")).toBe(1);
    expect(count(html, "客厅无人")).toBe(1);
  });
});

describe("TaskDetailSheet · 停用规则", () => {
  it("规则全部停用时说明是停用，而不是「暂无关联规则」", () => {
    const html = renderToStaticMarkup(
      <TaskDetailSheet
        task={task([
          rule({ ruleId: "e1", enabled: false, query: "有人进入客厅" }),
        ])}
        catalog={CATALOG}
        onClose={noop}
        onChanged={noop}
      />,
    );
    expect(html).toContain(i18n.t("tasks.rulesAllDisabled", { count: 1 }));
    // 停用规则不是「没有规则」：落进 noRules 会把住户引去查 Agent 接线
    expect(html).not.toContain(i18n.t("tasks.noRules"));
    expect(html).not.toContain("有人进入客厅");
  });

  it("只有前提、没有生效中的进入规则时，条件仍可见但不画空的进入组", () => {
    const html = renderToStaticMarkup(
      <TaskDetailSheet
        task={task([
          rule({
            ruleId: "g1",
            direction: "guard",
            sourceType: "iot",
            query: "客厅灯开着",
          }),
        ])}
        catalog={CATALOG}
        onClose={noop}
        onChanged={noop}
      />,
    );
    // 条件仍可见、可回顾；但不能承诺「未满足时不进入」——当前没有可拦的进入
    expect(html).toContain("客厅灯开着");
    expect(html).toContain(i18n.t("tasks.rulesOnlyGuards", { count: 1 }));
    expect(html).not.toContain(i18n.t("tasks.guardsConjunction", { count: 1 }));
    // 也不能说「均未生效」——这条前提本身是开着的
    expect(html).not.toContain(i18n.t("tasks.rulesAllDisabled", { count: 1 }));
    // 说明条不能丢：这条前提是 iot 源（只读），所以落在 ReadOnly 文案上；
    // 关键是与「全部停用」那支一样，这里也得有这句解释
    expect(html).toContain(i18n.t("tasks.rulesManagedHintReadOnly"));
  });

  it("退出规则仍在、进入规则被停用时，前提单独成块而不是消失", () => {
    const html = renderToStaticMarkup(
      <TaskDetailSheet
        task={task([
          rule({ ruleId: "x1", direction: "exit", query: "客厅无人" }),
          rule({
            ruleId: "g1",
            direction: "guard",
            sourceType: "iot",
            query: "客厅灯开着",
          }),
        ])}
        catalog={CATALOG}
        onClose={noop}
        onChanged={noop}
      />,
    );
    expect(html).toContain("客厅无人");
    // 退出组照画时前提也要有落点，否则标题区的「前提 1 项」在正文里找不到
    expect(html).toContain("客厅灯开着");
    expect(html).toContain(i18n.t("tasks.rulesOnlyGuards", { count: 1 }));
    expect(html).not.toContain(i18n.t("tasks.rulesAllDisabled", { count: 2 }));
    // 前提是拦进入的闸门：进入组缺席时也不能掉到退出组后面，否则读起来是倒的
    expect(html.indexOf("客厅灯开着")).toBeLessThan(html.indexOf("客厅无人"));
  });
});

describe("TaskDetailSheet · 动作行", () => {
  it("动作翻成设备名 + 能力名 + 值，不露英文 type_name", () => {
    const html = renderToStaticMarkup(
      <TaskDetailSheet
        task={task([rule({ ruleId: "e1" })], {
          onEnterActions: [
            { did: "d1", iid: "action.6.1", value: null, params: ["关灯"] },
          ],
        })}
        catalog={DEVICE_CATALOG}
        onClose={noop}
        onChanged={noop}
      />,
    );
    expect(html).toContain("小爱音箱");
    expect(html).toContain("播放文本");
    // 单入参加引号，与活动流同一个文案键
    expect(html).toContain("「关灯」");
    // 英文 type_name 不该出现在界面上；did · iid 只留在 title 里排查用
    expect(html).not.toContain("play-text");
  });
});

describe("TaskDetailSheet · 设备目录失败态", () => {
  it("拉取失败时给出提示与重试入口", () => {
    const html = renderToStaticMarkup(
      <TaskDetailSheet
        task={task([rule({ ruleId: "e1", query: "有人进入客厅" })], {
          onEnterActions: [{ did: "d1", iid: "prop.2.1", value: 1 }],
        })}
        catalog={CATALOG}
        catalogError={new Error("boom")}
        onRetryCatalog={noop}
        onClose={noop}
        onChanged={noop}
      />,
    );
    expect(html).toContain(i18n.t("tasks.deviceMetaFailed"));
    expect(html).toContain(i18n.t("common.retry"));
  });

  it("加载中（无 error）不显示失败提示", () => {
    const html = renderToStaticMarkup(
      <TaskDetailSheet
        task={task([rule({ ruleId: "e1", query: "有人进入客厅" })], {
          onEnterActions: [{ did: "d1", iid: "prop.2.1", value: 1 }],
        })}
        catalog={CATALOG}
        onClose={noop}
        onChanged={noop}
      />,
    );
    expect(html).not.toContain(i18n.t("tasks.deviceMetaFailed"));
  });
});

describe("GuardBlock · 编辑态", () => {
  const omniGuard = rule({
    ruleId: "g1",
    direction: "guard",
    sourceType: "omni",
    query: "客厅灯开着",
  });
  const iotGuard = { ...omniGuard, sourceType: "iot" as const };

  it("omni 前提在编辑态渲染成可改的输入框", () => {
    const html = renderToStaticMarkup(
      <GuardBlock
        rules={[omniGuard]}
        editing
        draftOf={() => "客厅灯开着（草稿）"}
        onDraftChange={noop}
      />,
    );
    expect(html).toContain("<textarea");
    expect(html).toContain("客厅灯开着（草稿）");
  });

  it("前提项也展示规则名徽标，和进入 / 退出规则对得上", () => {
    const html = renderToStaticMarkup(
      <GuardBlock
        rules={[omniGuard]}
        editing={false}
        draftOf={() => ""}
        onDraftChange={noop}
      />,
    );
    expect(html).toContain("客厅灯开着");
    expect(html).toContain("[t1] g1");
  });

  it("iot 前提在编辑态仍是只读文本，并给出不可编辑提示", () => {
    const html = renderToStaticMarkup(
      <GuardBlock
        rules={[iotGuard]}
        editing
        draftOf={() => "不该出现"}
        onDraftChange={noop}
      />,
    );
    expect(html).not.toContain("<textarea");
    expect(html).toContain("客厅灯开着");
    // 只读原因要给出来，否则住户会以为是自己点不开
    expect(html).toContain("不能在这里修改");
  });

  it("浏览态一律只读", () => {
    const html = renderToStaticMarkup(
      <GuardBlock
        rules={[omniGuard]}
        editing={false}
        draftOf={() => "不该出现"}
        onDraftChange={noop}
      />,
    );
    expect(html).not.toContain("<textarea");
    expect(html).not.toContain("不该出现");
  });
});
