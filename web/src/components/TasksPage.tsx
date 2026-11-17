/**
 * 任务页——独立「任务」Tab 的主视图。
 *
 * 展示 miloco 为家庭创建的持续任务（GET /api/tasks/summary?window=day）：
 *  - 列表标题旁一个「?」帮助入口：Web 端不直接建任务（真正驱动任务的感知规则由装有
 *    miloco 插件的 Agent（如 OpenClaw）接线），点开弹窗引导用户与 Agent 对话创建，
 *    并给示例话术一键复制。
 *  - 任务行：描述 + 进度摘要 + 启停开关；整行点击打开详情抽屉。
 *  - 详情抽屉：复用列表已加载的 summary 数据（含驱动规则 / 关联 / 进度），无需再拉
 *    单条全量视图。**先驱动规则、后执行动作**：前者回答「什么时候触发」，后者回答
 *    「触发了做什么」，顺序与运行时的因果一致。
 *  - 驱动规则按方向分块：进入（含前提）一块、退出一块。前提是闸门不是触发器，所以嵌
 *    在进入块内部，与「触发条件」同级。
 *  - 执行动作翻成人话：设备（房间）· 能力 · 值，一条一行。动作来自 task 的边界槽
 *    （运行期唯一来源），规则上的旧动作列不展示——那是回退遗留，两处都显示会让人以为
 *    动作配了两遍。
 *  - 抽屉编辑是「整屉一档」：底部一个「编辑」按钮把任务描述与每条规则的触发条件一起
 *    变成可填写状态，底部同一组「取消 / 保存」收口；保存只提交真正改过的字段——描述走
 *    PATCH /api/tasks/{id}，触发条件走 PATCH /api/rules/{id}（落库后 backend 会重新
 *    装载进 RuleRunner，新条件即时生效）。执行动作与推送仍由 Agent 接线，此处只读。
 */

import type { ComponentType, ReactNode, SVGProps } from "react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  deleteTask,
  listDeviceMeta,
  setTaskEnabled,
  updateRuleQuery,
  updateTaskDescription,
} from "@/api";
import { useAsync } from "@/hooks/useAsync";
import { useEscClose } from "@/hooks/useEscClose";
import {
  IconDoorIn,
  IconDoorOut,
  IconFunnel,
  IconHelp,
  IconPencil,
  IconPlay,
  IconShield,
  IconTrash,
  IconX,
} from "@/lib/icons";
import { relativeTime } from "@/lib/relativeTime";
import { groupRuleBriefs, ruleConditionIsEditable } from "@/lib/ruleBrief";
import {
  describeTaskAction,
  type ActionCatalog,
  type TaskActionKind,
} from "@/lib/taskAction";
import type {
  Task,
  TaskAction,
  TaskRecordSummary,
  TaskRuleBrief,
  TaskRuleDirection,
} from "@/lib/types";
import { AgentPromptDialog } from "./AgentPromptDialog";
import { toast } from "./Toast";

interface Props {
  tasks: Task[] | undefined;
  loading: boolean;
  // 返回 Promise 时（App 传的 tasks.reload()）抽屉会 await 到列表真落地再退出编辑态，
  // 避免"保存成功但卡片还显示旧文案"的一拍闪回。
  onChanged: () => void | Promise<void>;
}

type TFn = ReturnType<typeof useTranslation>["t"];
type IconComp = ComponentType<SVGProps<SVGSVGElement>>;

// record 摘要 → 一行人话进度文案（列表行用）；无 record 返空串。
function recordText(record: TaskRecordSummary | null, t: TFn): string {
  if (!record) return "";
  const d = record.derived;
  const num = (k: string) => Number(d[k] ?? 0);
  if (record.kind === "progress") {
    return t("tasks.progress", {
      current: num("current"),
      target: num("target"),
      unit: String(d.unit ?? ""),
    }).trim();
  }
  if (record.kind === "duration") {
    const parts = [
      t("tasks.durationToday", { minutes: num("accumulated_minutes_today") }),
    ];
    if (num("target_minutes") > 0) {
      parts.push(t("tasks.durationTarget", { minutes: num("target_minutes") }));
    }
    if (record.activeSession) parts.push(t("tasks.timing"));
    return parts.join(" · ");
  }
  const parts = [t("tasks.eventTotal", { count: num("count_total") })];
  if ("count_today" in d) {
    parts.push(t("tasks.eventToday", { count: num("count_today") }));
  }
  return parts.join(" · ");
}

const DIRECTION_LABEL_KEY: Record<TaskRuleDirection, string> = {
  enter: "tasks.directionEnter",
  exit: "tasks.directionExit",
  session: "tasks.directionSession",
  guard: "tasks.directionGuard",
};

// 方向 → 图标。进入/会话共用「门 + 向内箭头」，退出用「门 + 向外箭头」；
// 前提是闸门，用盾。会话同时承载进出，取进入那枚。
const DIRECTION_ICON: Record<TaskRuleDirection, IconComp> = {
  enter: IconDoorIn,
  session: IconDoorIn,
  exit: IconDoorOut,
  guard: IconShield,
};

// 动作类别 → 文案 key。新增类别时只改这一处，避免散在 JSX 的三元里漏分支。
const ACTION_KIND_LABEL: Record<TaskActionKind, string> = {
  broadcast: "tasks.actionKindBroadcast",
  deviceAction: "tasks.actionKindDeviceAction",
  control: "tasks.actionKindControl",
  scene: "tasks.actionKindScene",
};

// 轻量开关——on=品牌色 / off=中性边框色，无障碍 role="switch"。
function Switch({
  checked,
  disabled,
  onChange,
  label,
}: {
  checked: boolean;
  disabled?: boolean;
  onChange: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onChange}
      className={`relative shrink-0 inline-flex h-5 w-9 items-center rounded-full transition-colors disabled:opacity-50 ${
        checked ? "bg-brand-primary" : "bg-border-strong"
      }`}
    >
      <span
        className={`inline-block h-4 w-4 rounded-full bg-white shadow-sm transition-transform ${
          checked ? "translate-x-[18px]" : "translate-x-0.5"
        }`}
      />
    </button>
  );
}

// 进度条：target>0 时才有意义。label 在上、百分比在右，条在下。
function ProgressBar({ pct, label }: { pct: number; label: string }) {
  const clamped = Math.max(0, Math.min(100, pct));
  return (
    <div>
      <div className="flex items-baseline justify-between gap-2 mb-1.5">
        <span className="text-body text-text-primary num">{label}</span>
        <span className="text-caption-mono text-text-tertiary num">{clamped}%</span>
      </div>
      <div className="h-2 rounded-full bg-bg-tertiary overflow-hidden">
        <div
          className="h-full rounded-full bg-brand-primary transition-[width]"
          style={{ width: `${clamped}%` }}
        />
      </div>
    </div>
  );
}

// 两个大数字统计（event 类无目标时用）。
function StatPair({
  today,
  total,
  t,
}: {
  today: number | null;
  total: number;
  t: TFn;
}) {
  return (
    <div className="flex gap-8">
      {today !== null && (
        <div>
          <div className="text-title text-text-primary num">{today}</div>
          <div className="text-caption text-text-tertiary">
            {t("tasks.labelToday")} · {t("tasks.unitTimes")}
          </div>
        </div>
      )}
      <div>
        <div className="text-title text-text-primary num">{total}</div>
        <div className="text-caption text-text-tertiary">
          {t("tasks.labelTotal")} · {t("tasks.unitTimes")}
        </div>
      </div>
    </div>
  );
}

// 进度可视化：按 record.kind 多态。
function ProgressViz({
  record,
  t,
}: {
  record: TaskRecordSummary;
  t: TFn;
}) {
  const d = record.derived;
  const num = (k: string) => Number(d[k] ?? 0);

  let body: ReactNode;
  if (record.kind === "progress") {
    const cur = num("current");
    const tgt = num("target");
    const unit = String(d.unit ?? "");
    const pct = tgt > 0 ? Math.round((cur / tgt) * 100) : 0;
    body = <ProgressBar pct={pct} label={`${cur}/${tgt} ${unit}`.trim()} />;
  } else if (record.kind === "duration") {
    const today = num("accumulated_minutes_today");
    const tgt = num("target_minutes");
    if (tgt > 0) {
      body = (
        <ProgressBar
          pct={Math.round((today / tgt) * 100)}
          label={`${t("tasks.durationToday", { minutes: today })} · ${t(
            "tasks.durationTarget",
            { minutes: tgt },
          )}`}
        />
      );
    } else {
      body = (
        <div className="text-body text-text-primary num">
          {t("tasks.durationToday", { minutes: today })}
        </div>
      );
    }
  } else {
    const total = num("count_total");
    const today = "count_today" in d ? num("count_today") : null;
    body = <StatPair today={today} total={total} t={t} />;
  }

  return (
    <div className="space-y-2">
      {body}
      <div className="flex items-center gap-2 flex-wrap">
        {record.kind === "duration" && record.activeSession && (
          <span className="text-caption px-1.5 py-0.5 rounded bg-brand-soft text-brand-primary">
            {t("tasks.timing")}
          </span>
        )}
        {record.completed && (
          <span className="text-caption px-1.5 py-0.5 rounded bg-success-bg text-success">
            {t("tasks.completed")}
          </span>
        )}
        {record.windowRemaining && (
          <span className="text-caption text-text-tertiary num">
            {t("tasks.windowRemaining", {
              display: record.windowRemaining.display,
            })}
          </span>
        )}
      </div>
    </div>
  );
}

// 一节：图标 + 小标题 + 内容。图标是可选的——只有「驱动规则 / 执行动作」这两节用。
function Section({
  title,
  icon: Icon,
  children,
}: {
  title: string;
  icon?: IconComp;
  children: ReactNode;
}) {
  return (
    <section>
      <h3 className="flex items-center gap-1.5 text-caption font-semibold text-text-secondary uppercase tracking-wide mb-2">
        {Icon && <Icon width={14} height={14} className="text-text-tertiary shrink-0" />}
        {title}
      </h3>
      {children}
    </section>
  );
}

// 方向徽标：图标 + 文字，只用在规则卡的方向行上。
// 动作区的槽标签是纯文字——达标槽没有方向，硬套徽标会让两处对不上。
function DirectionTag({ direction, t }: { direction: TaskRuleDirection; t: TFn }) {
  const Icon = DIRECTION_ICON[direction];
  return (
    <span className="inline-flex items-center gap-1 text-caption px-1.5 py-0.5 rounded bg-bg-secondary text-text-secondary shrink-0">
      <Icon width={14} height={14} />
      {t(DIRECTION_LABEL_KEY[direction])}
    </span>
  );
}

// 规则名：等宽 + 浅底 pill。它是 prompt 里的标识、也是规则日志的检索键，
// 做成「标识符」而不是标题——读正文时能跳过，要复制去查日志时一眼找到。
function RuleNameBadge({ name }: { name: string }) {
  if (!name) return null;
  return (
    <span className="font-mono text-caption text-text-tertiary bg-bg-secondary border border-border rounded-md px-2 py-0.5 break-all">
      {name}
    </span>
  );
}

// 前提：暖色块 + 圆点列表。它是闸门不是触发器，颜色与「触发条件」分开，
// 但层级上与它并列（同属「进入」的两个条件）。
// 编辑态下 omni 源的前提也要能改 —— 那同样是住户写的自然语言，旧版逐卡编辑时
// 一直是可改的；把它降级成静态文本属于能力回退。
export function GuardBlock({
  rules,
  editing,
  draftOf,
  onDraftChange,
  idle = false,
}: {
  rules: TaskRuleBrief[];
  editing: boolean;
  draftOf: (rule: TaskRuleBrief) => string;
  onDraftChange: (rule: TaskRuleBrief, value: string) => void;
  /** 没有生效中的进入规则时的独立渲染：条件仍可见、omni 前提仍可改，
   *  但副标题不承诺「未满足时不进入」——当前根本没有可拦的进入。 */
  idle?: boolean;
}) {
  const { t } = useTranslation();
  if (rules.length === 0) return null;
  return (
    <div className="rounded-lg bg-warning-bg px-2.5 py-2">
      <div className="flex items-center gap-2 mb-1 flex-wrap">
        <span
          className="inline-flex items-center gap-1 text-caption px-1.5 py-0.5 rounded text-white shrink-0"
          style={{ background: "var(--color-warning)" }}
        >
          <IconShield width={14} height={14} />
          {t("tasks.directionGuard")}
        </span>
        <span className="text-caption text-text-tertiary">
          {idle
            ? t("tasks.rulesOnlyGuards", { count: rules.length })
            : t("tasks.guardsConjunction", { count: rules.length })}
        </span>
      </div>
      <ul className="space-y-1.5 list-disc pl-[18px]">
        {rules.map((r) => (
          <li key={r.ruleId} className="text-body text-text-primary leading-relaxed break-words">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0 flex-1">
                {editing && ruleConditionIsEditable(r) ? (
                  <>
                    <textarea
                      value={draftOf(r)}
                      onChange={(e) => onDraftChange(r, e.target.value)}
                      rows={2}
                      maxLength={200}
                      placeholder={t("tasks.triggerPlaceholder")}
                      className="w-full resize-none rounded-lg bg-bg-secondary border border-border px-3 py-2 text-body text-text-primary focus:outline-none focus:border-brand-primary"
                    />
                    <p className="text-caption text-text-tertiary leading-relaxed mt-1">
                      {t("tasks.triggerPhrasingHint")}
                    </p>
                  </>
                ) : (
                  <>
                    {r.query}
                    {editing && (
                      <p className="text-caption text-text-tertiary leading-relaxed mt-1">
                        {t("tasks.triggerNotEditable")}
                      </p>
                    )}
                  </>
                )}
              </div>
              {/* 前提也是进 prompt 的规则，规则名要和进入 / 退出规则一样能对上 */}
              <RuleNameBadge name={r.name} />
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

// 一组规则（进入 / 退出）。前提是 task 级闸门：运行期所有进入请求都先挂起，
// 再用该 task 的全部前提逐条比对，全成立才放行——它对每一条进入规则都生效。
// 所以前提放在组内最前面，而不是塞进某一条规则里：挂在单条规则上会让别的进入
// 条件看起来不受约束。方向标签提到组头，组内每条规则只留规则名与触发条件。
export function RuleGroupCard({
  direction,
  rules,
  guards,
  editing,
  draftOf,
  onDraftChange,
}: {
  direction: TaskRuleDirection;
  rules: TaskRuleBrief[];
  guards: TaskRuleBrief[];
  editing: boolean;
  draftOf: (rule: TaskRuleBrief) => string;
  onDraftChange: (rule: TaskRuleBrief, value: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="rounded-xl bg-bg-primary border border-border overflow-hidden">
      <div className="px-3.5 py-2.5 border-b border-border flex items-center gap-2">
        <DirectionTag direction={direction} t={t} />
      </div>
      <div className="px-3.5 py-3 space-y-3">
        <GuardBlock
          rules={guards}
          editing={editing}
          draftOf={draftOf}
          onDraftChange={onDraftChange}
        />
        {rules.map((rule, i) => {
          const conditionIsReadOnly = !ruleConditionIsEditable(rule);
          return (
            <div
              key={rule.ruleId}
              className={i > 0 ? "pt-3 border-t border-border" : ""}
            >
              <div className="flex items-start justify-between gap-3 mb-1.5">
                <span className="text-caption text-text-tertiary">
                  {t("tasks.triggerCondition")}
                </span>
                <RuleNameBadge name={rule.name} />
              </div>
              {editing && !conditionIsReadOnly ? (
                <>
                  <textarea
                    value={draftOf(rule)}
                    onChange={(e) => onDraftChange(rule, e.target.value)}
                    rows={2}
                    maxLength={200}
                    placeholder={t("tasks.triggerPlaceholder")}
                    className="w-full resize-none rounded-lg bg-bg-secondary border border-border px-3 py-2 text-body text-text-primary focus:outline-none focus:border-brand-primary"
                  />
                  {/* 措辞提示：backend _validate_query_phrasing 会挡「检测到」这类断言性
                      开头（感知模型会当成已发生的事实 → 连续误触发）。校验仍以 backend
                      为单一真源，这里只前置引导，不在前端复制那份前缀表。 */}
                  <p className="text-caption text-text-tertiary leading-relaxed mt-1.5">
                    {t("tasks.triggerPhrasingHint")}
                  </p>
                </>
              ) : (
                <>
                  <div className="text-body text-text-primary leading-relaxed break-words">
                    {rule.query}
                  </div>
                  {editing && conditionIsReadOnly && (
                    <p className="text-caption text-text-tertiary leading-relaxed mt-1.5">
                      {t("tasks.triggerNotEditable")}
                    </p>
                  )}
                </>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}


// 单个动作槽：交给 Agent 的文案 + 一条一行的人话动作。
// 槽里既没有直控动作、也没有 Agent 文案时整块不渲染（判断在调用处）。
function ActionSlot({
  label,
  actions,
  desc,
  catalog,
}: {
  label: string;
  actions: TaskAction[];
  desc: string | null;
  catalog: ActionCatalog;
}) {
  const { t } = useTranslation();
  return (
    <div className="mb-2 last:mb-0">
      <div className="mb-1 text-caption text-text-tertiary">{label}</div>
      <div className="rounded-xl bg-bg-primary border border-border divide-y divide-border">
        {desc?.trim() && (
          <div className="px-3.5 py-2.5 text-body text-text-secondary leading-relaxed break-words">
            {t("tasks.agentAction", { desc: desc.trim() })}
          </div>
        )}
        {actions.map((action, i) => {
          const d = describeTaskAction(action, catalog);
          return (
            <div
              key={i}
              title={d.raw}
              className="px-3.5 py-2.5 flex flex-wrap items-center gap-x-2 gap-y-1.5 text-body text-text-secondary"
            >
              <span
                className={`text-caption px-1.5 py-0.5 rounded shrink-0 ${
                  d.kind === "broadcast"
                    ? "bg-brand-soft text-brand-primary"
                    : "bg-bg-secondary text-text-secondary"
                }`}
              >
                {t(ACTION_KIND_LABEL[d.kind])}
              </span>
              <span className="font-semibold text-text-primary break-all">
                {d.device}
              </span>
              {d.room && (
                <span className="text-caption text-text-tertiary">
                  {t("tasks.roomParen", { room: d.room })}
                </span>
              )}
              {/* 场景行没有能力名，跳过分隔点，别留一个孤立的「·」 */}
              {d.capability && (
                <>
                  <span className="text-text-disabled">·</span>
                  <span className="break-all">{d.capability}</span>
                </>
              )}
              {d.detail && (
                <span className="text-text-primary break-words">{d.detail}</span>
              )}
              {d.chips.map((chip) => (
                <span
                  key={chip}
                  className="text-caption px-1.5 py-0.5 rounded bg-bg-tertiary text-text-tertiary"
                >
                  {chip}
                </span>
              ))}
              {d.unresolved && (
                <span className="text-caption px-1.5 py-0.5 rounded bg-warning-bg text-warning">
                  {t(
                    d.kind === "scene"
                      ? "tasks.chipUnresolvedScene"
                      : "tasks.chipUnresolvedTarget",
                  )}
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// 详情抽屉：驱动规则在前、执行动作在后；一个编辑态改描述 + 触发条件，外加删除。
export function TaskDetailSheet({
  task,
  catalog,
  catalogError,
  onRetryCatalog,
  onClose,
  onChanged,
}: {
  task: Task;
  catalog: ActionCatalog;
  /** 设备目录拉取失败。加载中不带这个——两者在界面上是不同的话。 */
  catalogError?: Error;
  onRetryCatalog?: () => void;
  onClose: () => void;
  onChanged: () => void | Promise<void>;
}) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [descDraft, setDescDraft] = useState(task.description);
  // ruleId → 触发条件草稿；进入编辑态时按当前规则快照重建。
  const [ruleDrafts, setRuleDrafts] = useState<Record<string, string>>({});
  const [confirmDel, setConfirmDel] = useState(false);
  const [busy, setBusy] = useState(false);

  const startEdit = () => {
    setDescDraft(task.description);
    setRuleDrafts(
      Object.fromEntries(task.ruleBriefs.map((r) => [r.ruleId, r.query])),
    );
    setEditing(true);
  };
  // 退编辑态不必清草稿：下次 startEdit 会按那时的最新数据重建。
  const cancelEdit = () => setEditing(false);

  // 编辑 / 删除确认态下，ESC 先退回浏览态而非关整个抽屉；保存中不响应。
  useEscClose(true, () => {
    if (busy) return;
    if (editing) cancelEdit();
    else if (confirmDel) setConfirmDel(false);
    else onClose();
  });

  const paused = task.status === "paused";
  const ruleDraft = (r: TaskRuleBrief) => ruleDrafts[r.ruleId] ?? r.query;

  // 规则按方向分块：进入（或会话）一块并带上前提，退出一块。
  // 前提是闸门、只能拦进入，所以不单列——单列会让人以为它也是一条触发路径。
  // 同一方向可以有多条规则，分组恒为数组（见 groupRuleBriefs）。
  const { entry: entryRules, exit: exitRules, guard: guardRules } =
    groupRuleBriefs(task.ruleBriefs);
  // 停用的规则不画：它们不进运行期拓扑、也不进感知 prompt。判断「有没有规则」
  // 也要用它，否则全被停用时会显示一句「可修改」却一条规则都没有。
  const visibleRules = task.ruleBriefs.filter((r) => r.enabled);

  const enterActions = task.actions.onEnterActions;
  const exitActions = task.actions.onExitActions;
  const targetActions = task.actions.onTargetActions;

  // 标题区统计：数量为 0 的项整条不渲染（与「没有前提就不显示前提」同一条规则），
  // 量词统一（前提论「项」，动作论「条」）。
  const stats: string[] = [];
  if (guardRules.length > 0) {
    stats.push(t("tasks.statGuards", { count: guardRules.length }));
  }
  if (enterActions.length > 0) {
    stats.push(t("tasks.statEnterActions", { count: enterActions.length }));
  }
  if (exitActions.length > 0) {
    stats.push(t("tasks.statExitActions", { count: exitActions.length }));
  }
  if (targetActions.length > 0) {
    stats.push(t("tasks.statTargetActions", { count: targetActions.length }));
  }

  const enterSlotFilled =
    enterActions.length > 0 || Boolean(task.actions.onEnterDesc?.trim());
  const exitSlotFilled =
    exitActions.length > 0 || Boolean(task.actions.onExitDesc?.trim());
  const targetSlotFilled =
    targetActions.length > 0 || Boolean(task.actions.onTargetDesc?.trim());

  // 一次保存整屉改动：描述 → PATCH /api/tasks/{id}；每条触发条件 → PATCH /api/rules/{id}
  // （只带 condition.query，感知设备 perceive_device_ids 由 backend 保留）。
  // 只提交真改过的字段；清空视作放弃这一处修改（空描述 / 空条件没有意义）。
  const save = async () => {
    const jobs: Array<() => Promise<void>> = [];
    const nextDesc = descDraft.trim();
    if (nextDesc && nextDesc !== task.description) {
      jobs.push(() => updateTaskDescription(task.taskId, nextDesc));
    }
    // 只提交界面上看得到的规则：停用规则不渲染、也没有草稿可改，
    // 遍历全量只会在它的条件带首尾空白时发出一次静默 PATCH。
    for (const r of visibleRules) {
      // 判据就是源本身。靠 `next !== r.query` 跳过只读 rule 的话，依赖的是服务端渲染
      // 出的 query 首尾无空白——那是巧合不是保证，一旦带上空白就会对 iot rule 发一次
      // 必被拒的 PATCH，而这里是串行保存，同屉别的改动会「部分成功」。
      if (!ruleConditionIsEditable(r)) continue;
      const next = ruleDraft(r).trim();
      if (next && next !== r.query) {
        jobs.push(() => updateRuleQuery(r.ruleId, next));
      }
    }
    if (jobs.length === 0) {
      cancelEdit();
      return;
    }
    setBusy(true);
    let saved = 0;
    try {
      // 串行提交：backend 会退回断言性措辞（422），失败就停在编辑态、保留输入，
      // 住户能看清是哪一句被退回。已落库的部分靠下面的 onChanged() 同步到界面。
      for (const job of jobs) {
        await job();
        saved += 1;
      }
      toast(t("tasks.saved"), "ok");
      // 先等列表重拉落地再退编辑态：抽屉的 task 由列表数据派生，否则会闪一拍旧文案。
      await onChanged();
      setEditing(false);
    } catch (e) {
      toast(e instanceof Error ? e.message : t("family.operationFail"), "warn");
      // 部分成功：把已落库的拉回来，界面别停在与后端不一致的状态上。
      // 拉回后已保存字段的草稿 === 新值，重试时不会重复提交。
      if (saved > 0) await onChanged();
    } finally {
      setBusy(false);
    }
  };

  const doDelete = async () => {
    setBusy(true);
    try {
      await deleteTask(task.taskId);
      toast(t("tasks.deleted"), "ok");
      await onChanged();
      onClose();
    } catch (e) {
      toast(e instanceof Error ? e.message : t("family.operationFail"), "warn");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-[65] flex items-end md:items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={(e) => {
        e.stopPropagation();
        if (busy) return;
        // 编辑态下点遮罩先退回浏览态：别让一次误点把整屉还没保存的输入丢掉。
        if (editing) cancelEdit();
        else onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="task-detail-title"
        className="flex w-full max-h-[85vh] flex-col bg-bg-secondary border border-border rounded-t-2xl md:max-w-2xl md:rounded-2xl shadow-lg anim-in"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 头部 */}
        <div className="flex items-start justify-between gap-3 px-5 pt-5 pb-4 border-b border-border shrink-0">
          {/* 编辑态下标题换成模式名：描述本身已经在下面作为可填写字段，
              标题再重复一遍长文案只会挤掉屏幕、也说不清"现在在改什么"。 */}
          <div className="min-w-0 flex-1">
            {editing ? (
              <h2
                id="task-detail-title"
                className="text-title font-semibold text-text-primary"
              >
                {t("tasks.editTitle")}
              </h2>
            ) : (
              <>
                <div className="text-caption text-text-tertiary mb-1">
                  {t("tasks.detailTitle")}
                </div>
                <h2
                  id="task-detail-title"
                  className="text-title font-semibold text-text-primary break-words"
                >
                  {task.description}
                </h2>
                <span
                  className={`inline-block mt-2 text-caption px-1.5 py-0.5 rounded ${
                    paused
                      ? "bg-bg-tertiary text-text-tertiary"
                      : "bg-success-bg text-success"
                  }`}
                >
                  {paused ? t("tasks.statusPaused") : t("tasks.statusActive")}
                </span>
                {/* 停用时运行态恒 off, 那句标签没有信息量; 启用而"未进行"才是
                    排查「规则怎么没反应」要看的那一眼 */}
                {!paused && (
                  <span
                    className={`ml-2 inline-block text-caption px-1.5 py-0.5 rounded ${
                      task.runtimeState === "on"
                        ? "bg-brand-soft text-brand-primary"
                        : "bg-bg-secondary text-text-secondary"
                    }`}
                    title={t("tasks.runtimeStateHint")}
                  >
                    {t(
                      task.runtimeState === "on"
                        ? "tasks.runtimeOn"
                        : "tasks.runtimeOff",
                    )}
                  </span>
                )}
                {/* 结构统计：同为「任务现在是什么样」的状态展示，与上面两枚同族；
                    与元信息（创建时间）分开——后者在主体末尾。 */}
                {stats.map((s) => (
                  <span
                    key={s}
                    className="ml-2 inline-block text-caption px-1.5 py-0.5 rounded bg-info-bg text-info"
                  >
                    {s}
                  </span>
                ))}
                {paused && task.pausedAt && (
                  <span className="ml-2 text-caption text-text-tertiary num">
                    {t("tasks.pausedAtLabel", {
                      time: relativeTime(task.pausedAt),
                    })}
                  </span>
                )}
              </>
            )}
          </div>
          <button
            type="button"
            // 编辑态下 X 退回浏览态而非关抽屉，和 ESC / 点遮罩一个口径。
            onClick={editing ? cancelEdit : onClose}
            disabled={busy}
            aria-label={editing ? t("family.cancel") : t("family.close")}
            className="shrink-0 p-1.5 -mr-1.5 rounded-md text-text-tertiary hover:text-text-primary hover:bg-bg-tertiary transition-colors disabled:opacity-50"
          >
            <IconX width={18} height={18} />
          </button>
        </div>

        {/* 主体：驱动规则 → 执行动作 → 进度 → 创建时间（次要） */}
        <div className="px-5 py-5 overflow-y-auto space-y-6">
          {/* 描述字段只在编辑态出现——浏览态它就是上面的大标题。 */}
          {editing && (
            <Section title={t("tasks.descriptionLabel")}>
              <textarea
                value={descDraft}
                onChange={(e) => setDescDraft(e.target.value)}
                rows={2}
                maxLength={200}
                autoFocus
                placeholder={t("tasks.descPlaceholder")}
                className="w-full resize-none rounded-lg bg-bg-primary border border-border px-3 py-2 text-body text-text-primary focus:outline-none focus:border-brand-primary"
              />
            </Section>
          )}

          <Section title={t("tasks.rulesTitle")} icon={IconFunnel}>
            {/* 有生效中的进入 / 退出规则才画规则区：前提是进入组的闸门，
                没有可拦的进入时单独画出来是一句空承诺。 */}
            {entryRules.length > 0 || exitRules.length > 0 ? (
              <div className="space-y-3">
                {/* 进入与退出各成一组；前提是 task 级闸门，放在进入组的最前面，
                    对组内每一条进入规则都生效（见 RuleGroupCard 的说明）。 */}
                {entryRules.length > 0 && (
                  <RuleGroupCard
                    direction={entryRules[0]?.direction ?? "enter"}
                    rules={entryRules}
                    guards={guardRules}
                    editing={editing}
                    draftOf={ruleDraft}
                    onDraftChange={(r, v) =>
                      setRuleDrafts((m) => ({ ...m, [r.ruleId]: v }))
                    }
                  />
                )}
                {/* 进入规则被停用或还没接线时，前提块不在任何进入组里；补一个独立块，
                    排在退出组之前——前提是拦进入的闸门，「前提 → 进入 → 退出」的顺序
                    不能因为进入组缺席就倒过来。顺带让标题区的「前提 N 项」在正文里
                    有落点、omni 前提这一轮仍可改。 */}
                {entryRules.length === 0 && guardRules.length > 0 && (
                  <GuardBlock
                    rules={guardRules}
                    editing={editing}
                    draftOf={ruleDraft}
                    onDraftChange={(r, v) =>
                      setRuleDrafts((m) => ({ ...m, [r.ruleId]: v }))
                    }
                    idle
                  />
                )}
                {exitRules.length > 0 && (
                  <RuleGroupCard
                    direction="exit"
                    rules={exitRules}
                    guards={[]}
                    editing={editing}
                    draftOf={ruleDraft}
                    onDraftChange={(r, v) =>
                      setRuleDrafts((m) => ({ ...m, [r.ruleId]: v }))
                    }
                  />
                )}
                <p className="text-caption text-text-tertiary">
                  {visibleRules.some(ruleConditionIsEditable)
                    ? t("tasks.rulesManagedHint")
                    : t("tasks.rulesManagedHintReadOnly")}
                </p>
              </div>
            ) : guardRules.length > 0 ? (
              <div className="space-y-3">
                {/* 只剩前提（进入 / 退出规则被停用或还没接线）：条件仍可见、omni 前提
                    仍可改，但不画空的进入组——那会让「未满足时不进入」成为空承诺。 */}
                <GuardBlock
                  rules={guardRules}
                  editing={editing}
                  draftOf={ruleDraft}
                  onDraftChange={(r, v) =>
                    setRuleDrafts((m) => ({ ...m, [r.ruleId]: v }))
                  }
                  idle
                />
                {/* 说明条与「有进入 / 退出规则」那支同源：这里同样能改，
                    同样要讲清楚动作与推送由谁管。 */}
                <p className="text-caption text-text-tertiary">
                  {visibleRules.some(ruleConditionIsEditable)
                    ? t("tasks.rulesManagedHint")
                    : t("tasks.rulesManagedHintReadOnly")}
                </p>
              </div>
            ) : task.ruleBriefs.length > 0 ? (
              // 有规则但一条都没在生效：停用规则不参与运行期判定，
              // 落进下面的「暂无关联规则」会把住户引向查 Agent 接线。
              <div className="text-caption text-text-tertiary">
                {t("tasks.rulesAllDisabled", {
                  count: task.ruleBriefs.length,
                })}
              </div>
            ) : (
              <div className="text-caption text-text-tertiary">
                {t("tasks.noRules")}
              </div>
            )}
          </Section>

          {(enterSlotFilled || exitSlotFilled || targetSlotFilled) && (
            <Section title={t("tasks.taskActionsTitle")} icon={IconPlay}>
              {catalogError && (
                <p className="mb-2 text-caption text-text-tertiary">
                  {t("tasks.deviceMetaFailed")}{" "}
                  <button
                    type="button"
                    onClick={onRetryCatalog}
                    className="underline hover:text-text-primary transition-colors"
                  >
                    {t("common.retry")}
                  </button>
                </p>
              )}
              {enterSlotFilled && (
                <ActionSlot
                  label={t("tasks.slotEnter")}
                  actions={enterActions}
                  desc={task.actions.onEnterDesc}
                  catalog={catalog}
                />
              )}
              {exitSlotFilled && (
                <ActionSlot
                  label={t("tasks.slotExit")}
                  actions={exitActions}
                  desc={task.actions.onExitDesc}
                  catalog={catalog}
                />
              )}
              {targetSlotFilled && (
                <ActionSlot
                  label={t("tasks.slotTarget")}
                  actions={targetActions}
                  desc={task.actions.onTargetDesc}
                  catalog={catalog}
                />
              )}
            </Section>
          )}

          {task.record && (
            <Section title={t("tasks.progressLabel")}>
              <ProgressViz record={task.record} t={t} />
            </Section>
          )}

          <div className="text-caption text-text-tertiary num pt-1">
            {t("tasks.createdLabel")} {relativeTime(task.createdAt)}
          </div>
        </div>

        {/* 底部操作 */}
        <div className="flex items-center justify-between gap-2 px-4 py-3 border-t border-border shrink-0">
          {editing ? (
            <>
              <span />
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={cancelEdit}
                  disabled={busy}
                  className="h-9 px-4 rounded-lg text-caption text-text-secondary hover:text-text-primary hover:bg-bg-tertiary transition-colors disabled:opacity-60"
                >
                  {t("family.cancel")}
                </button>
                <button
                  type="button"
                  onClick={save}
                  disabled={busy}
                  className="h-9 px-4 rounded-lg text-caption font-semibold bg-brand-primary text-white hover:bg-brand-accent transition-colors disabled:opacity-60"
                >
                  {busy ? t("family.saving") : t("family.save")}
                </button>
              </div>
            </>
          ) : confirmDel ? (
            <>
              {/* 删除是级联且不可逆（连带清理规则 / 记录），确认态显式给出后果警示。 */}
              <span className="flex-1 min-w-0 text-caption text-error break-words line-clamp-2">
                {t("tasks.confirmDeleteMessage", { desc: task.description })}
              </span>
              <div className="flex items-center gap-2 shrink-0">
                <button
                  type="button"
                  onClick={() => setConfirmDel(false)}
                  disabled={busy}
                  className="h-9 px-4 rounded-lg text-caption text-text-secondary hover:text-text-primary hover:bg-bg-tertiary transition-colors disabled:opacity-60"
                >
                  {t("family.cancel")}
                </button>
                <button
                  type="button"
                  onClick={doDelete}
                  disabled={busy}
                  className="h-9 px-4 rounded-lg text-caption font-semibold bg-error text-white hover:opacity-90 transition-opacity disabled:opacity-60"
                >
                  {busy ? t("family.deleting") : t("family.confirmDelete")}
                </button>
              </div>
            </>
          ) : (
            <>
              <button
                type="button"
                onClick={() => setConfirmDel(true)}
                disabled={busy}
                className="inline-flex items-center gap-1.5 h-9 px-4 rounded-lg text-caption text-text-tertiary hover:text-error hover:bg-error-bg transition-colors disabled:opacity-60"
              >
                <IconTrash width={15} height={15} />
                {t("tasks.delete")}
              </button>
              {/* 一个入口进编辑：描述与全部触发条件一起变可填写，收口在同一组按钮上。 */}
              <button
                type="button"
                onClick={startEdit}
                disabled={busy}
                className="inline-flex items-center gap-1.5 h-9 px-4 rounded-lg text-caption font-semibold bg-bg-secondary border border-border text-text-secondary hover:text-text-primary hover:border-border-strong transition-colors disabled:opacity-60"
              >
                <IconPencil width={15} height={15} />
                {t("family.edit")}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

export function TasksPage({ tasks, loading, onChanged }: Props) {
  const { t } = useTranslation();
  const [busyId, setBusyId] = useState<string | null>(null);
  // 只记 id、渲染时回列表取最新一条：抽屉里改完描述 / 触发条件后 onChanged 重拉，
  // 抽屉能直接看到新值（存 Task 快照会定死在打开那一刻）。任务被删/消失 → 派生成
  // null，抽屉自然收起。useAsync 重拉期间保留旧 data，故加载中不会闪空。
  const [detailId, setDetailId] = useState<string | null>(null);
  const [helpOpen, setHelpOpen] = useState(false);

  // 设备名 / 房间 / spec 与场景表：动作翻人话用。与设备页共用同一个 /api/miot/home
  // 缓存，不额外拉每台设备的状态。拉不到时 catalog.ready=false，动作回落成裸 did。
  const meta = useAsync(() => listDeviceMeta(), []);
  const catalog = useMemo<ActionCatalog>(
    () => ({
      devices: new Map((meta.data?.devices ?? []).map((d) => [d.did, d])),
      scenes: new Map((meta.data?.scenes ?? []).map((s) => [s.id, s.name])),
      ready: meta.data !== undefined,
      t,
    }),
    [meta.data, t],
  );

  const run = async (taskId: string, fn: () => Promise<void>, okMsg: string) => {
    setBusyId(taskId);
    try {
      await fn();
      toast(okMsg, "ok");
      onChanged();
    } catch (e) {
      toast(e instanceof Error ? e.message : t("family.operationFail"), "warn");
    } finally {
      setBusyId(null);
    }
  };

  const list = tasks ?? [];
  const empty = !loading && list.length === 0;
  const detail = detailId
    ? (list.find((x) => x.taskId === detailId) ?? null)
    : null;

  return (
    <div className="space-y-6">
      <section
        className="rounded-xl bg-bg-secondary border border-border shadow-sm anim-in"
        aria-labelledby="tasks-list-title"
      >
        <div className="flex items-start justify-between gap-2 px-5 pt-4 pb-1">
          <div className="min-w-0">
            <h2
              id="tasks-list-title"
              className="text-title text-text-primary inline-flex items-baseline gap-2"
            >
              {t("tasks.title")}
              <span className="text-caption-mono text-text-tertiary font-normal num">
                {t("tasks.count", { count: list.length })}
              </span>
            </h2>
            <p className="text-caption text-text-tertiary mt-0.5">
              {t("tasks.hint")}
            </p>
          </div>
          {/* Web 端不直接建任务（感知规则由 Agent 接线），故按钮带「?」暗示这是引导 */}
          <button
            type="button"
            onClick={() => setHelpOpen(true)}
            title={t("tasks.howToTitle")}
            className="shrink-0 inline-flex items-center gap-1.5 h-9 px-3 rounded-lg text-caption font-semibold border border-border bg-bg-primary text-text-secondary hover:text-text-primary hover:border-border-strong transition-colors"
          >
            {t("tasks.addTask")}
            <IconHelp width={14} height={14} className="text-text-tertiary" />
          </button>
        </div>

        {loading && !tasks ? (
          <div className="text-body text-text-secondary py-10 px-5 text-center">
            <span className="inline-flex items-center gap-2">
              <span className="inline-block w-2 h-2 rounded-full bg-text-tertiary animate-pulse" />
              {t("family.loading")}
            </span>
          </div>
        ) : empty ? (
          <div className="py-10 px-5 text-center">
            <div className="text-body text-text-secondary">{t("tasks.empty")}</div>
            <div className="text-caption text-text-tertiary mt-1">
              {t("tasks.emptyHint")}
            </div>
            <button
              type="button"
              onClick={() => setHelpOpen(true)}
              className="mt-4 inline-flex items-center gap-1.5 text-caption px-3 py-1.5 rounded-md border border-border bg-bg-primary text-text-secondary hover:text-text-primary hover:border-border-strong transition-colors"
            >
              <IconHelp width={15} height={15} />
              {t("tasks.viewExamples")}
            </button>
          </div>
        ) : (
          <div className="px-5 pt-2 pb-4 divide-y divide-border">
            {list.map((task) => {
              const paused = task.status === "paused";
              const summary = recordText(task.record, t);
              const busy = busyId === task.taskId;
              return (
                <div key={task.taskId} className="group flex items-center gap-3 py-3">
                  <button
                    type="button"
                    onClick={() => setDetailId(task.taskId)}
                    className="min-w-0 flex-1 text-left rounded-md -mx-2 px-2 py-1 hover:bg-bg-tertiary transition-colors"
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      <span
                        className={`text-body truncate ${
                          paused ? "text-text-tertiary" : "text-text-primary"
                        }`}
                      >
                        {task.description}
                      </span>
                      {task.record?.completed && (
                        <span className="shrink-0 text-caption px-1.5 py-0.5 rounded bg-success-bg text-success">
                          {t("tasks.completed")}
                        </span>
                      )}
                    </div>
                    {summary && (
                      <div className="text-caption text-text-tertiary mt-0.5 num">
                        {summary}
                      </div>
                    )}
                  </button>

                  <Switch
                    checked={!paused}
                    disabled={busy}
                    label={paused ? t("tasks.enable") : t("tasks.pause")}
                    onChange={() =>
                      run(
                        task.taskId,
                        () => setTaskEnabled(task.taskId, paused),
                        paused ? t("tasks.enabled") : t("tasks.paused"),
                      )
                    }
                  />
                </div>
              );
            })}
          </div>
        )}
      </section>

      {detail && (
        <TaskDetailSheet
          key={detail.taskId}
          task={detail}
          catalog={catalog}
          catalogError={meta.error}
          onRetryCatalog={() => void meta.reload()}
          onClose={() => setDetailId(null)}
          onChanged={onChanged}
        />
      )}

      {helpOpen && (
        <AgentPromptDialog
          title={t("tasks.howToTitle")}
          hint={t("tasks.howToBody")}
          examples={[
            t("tasks.example1"),
            t("tasks.example2"),
            t("tasks.example3"),
          ]}
          onClose={() => setHelpOpen(false)}
        />
      )}
    </div>
  );
}
