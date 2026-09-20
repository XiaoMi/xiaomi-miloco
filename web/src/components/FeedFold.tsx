/**
 * 折叠件的渲染面:强度分段控件、事件行上的相位徽标、弱档的支行、展开的成员表。
 *
 * 判断全在 lib/feedFold(纯函数,node 环境测得动);这里只管画与接线。分成两个文件的
 * 界线就是「读得出对错」与「看得出好坏」——装配规则写错了要能被断言抓住,而间距、层级、
 * 图标对不对只能靠看。
 *
 * 三处不变量值得先交代,它们都是**一处定义、多处引用**:
 * - 展开集合的键 = 支的 key。徽标写 aria-expanded、成员表挂 id、滚动与焦点都以它为准,
 *   各自算一套迟早对不上。
 * - `aria-controls` 只在被控内容真在 DOM 里时才写。折叠态下成员表根本不渲染(这正是折叠
 *   的意义),这时指向一个不存在的 id 是 ARIA 作者错误——`aria-expanded="false"` 已经说清
 *   了这里有个可展开的东西,不需要一个空指针。
 * - 每一枚徽标都是一个 `<button>`、整枚一个热区,角标是按钮内的指示符而非嵌套热区。
 */

import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from "react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import type { BackendPropSpec } from "@/api/real";
import {
  formatLatency,
  latencyMs,
  type FoldBranch,
  type FoldChip,
  type FoldStrength,
} from "@/lib/feedFold";
import type { ActivityEvent } from "@/lib/types";
import { ActionRow } from "./ActionsFeed";
import { TimeLabel } from "./TimeLabel";

/* ── 图标 ───────────────────────────────────────────────────
   四枚形状定稿于设计稿的候选表:触发=闪电(不讲方向,只讲"发生了"),
   退出=门框加向右穿出的箭头,下钻=准星(跳过去的方向随档位变,只有"定位过去"恒真),
   回返=一根直落箭头(它出现的地方目标都在下方,见 lib/feedFold 结尾的并列规则)。 */
const ICON = {
  enter: <path d="M13 2 3 14h8l-1 8 10-12h-8z" />,
  exit: (
    <>
      <path d="M11 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h5" />
      <path d="m16 16 4-4-4-4" />
      <path d="M20 12H9" />
    </>
  ),
  chevron: <path d="m6 9 6 6 6-6" />,
  target: (
    <>
      <circle cx="12" cy="12" r="8" />
      <circle cx="12" cy="12" r="2" fill="currentColor" stroke="none" />
    </>
  ),
  back: (
    <>
      <path d="M12 4v13" />
      <path d="m6 12 6 6 6-6" />
    </>
  ),
  unlink: <path d="M9 17H7A5 5 0 0 1 7 7h2M15 7h2a5 5 0 0 1 3.5 8.5M3 3l18 18" />,
} as const;

function Icon({ d, className }: { d: ReactNode; className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      {d}
    </svg>
  );
}

/* ── 展开三件套 ─────────────────────────────────────────────
   键 → 展开面 → 成员表。徽标(强档)与支行(弱档)各写一份的话,迟早有一处对不上,
   而症状是"点开以后滚动没动、焦点丢了"这类说不清的东西。

   展开面的 id **由摆放位置决定,不是由强度决定**:强档的成员表挂在宿主事件行下
   (同一行下可以有好几片),弱档、以及任何一档下"没有宿主行可依"的支,都挂在自己的
   支行里。点开时要滚向哪个 id 必须与摆法同源 —— 于是强档那片要一个派生 id(事件行的
   id 是事件 id,支的键在那儿没有元素承载),弱档不用:支行行体自己就带着支的键。 */
export function hostRegionOf(key: string): string {
  return "x-" + key;
}
/** 成员表 id,`aria-controls` 指它。 */
export function membersIdOf(key: string): string {
  return "m-" + key;
}
/** 展开后焦点落在的第一条动作——键盘用户不必再从展开面顶上自己找。 */
export function firstMemberIdOf(key: string): string {
  return key + "-m0";
}

function phaseName(phase: string | null, t: TFunction): string {
  if (phase === "exit") return t("actions.phaseExit");
  if (phase === "legacy") return t("actions.phaseLegacy");
  if (phase === "enter") return t("actions.phaseEnter");
  return t("actions.phaseUnknown"); // 有链路却读不出相位:不冒充"触发动作"
}

function phaseIcon(phase: string | null): ReactNode {
  return ICON[phase === "exit" ? "exit" : "enter"];
}

/* ── 状态条 ─────────────────────────────────────────────────
   一个动作一段,段数恒等于动作数。**不做"全成功就画一根实心条"的压缩**:那会让 9 个
   全成功和 1 个成功长得一样,而且只有带失败的那条才分节——"为什么只有失败的那条有
   划分"就是这么来的。计数不写在条上,它进 aria-label;条本身对读屏是隐藏的。 */
export function StatusStrip({
  actions,
  mini = false,
  label,
}: {
  actions: FoldBranch["actions"];
  mini?: boolean;
  /** 非迷你条的读屏文本(迷你条在徽标里,由徽标整枚承载语义)。 */
  label?: string;
}) {
  // 段数多到缝的占比过大时(>16 段)缝就省了,靠相邻色块自己分界。
  const gap = actions.length > 16 ? 0 : 1;
  return (
    <span
      className={`inline-flex items-center overflow-hidden rounded-sm bg-border ${
        mini ? "shrink-0 w-[58px] h-1.5" : "w-full h-2"
      }`}
      {...(mini ? { "aria-hidden": true } : { role: "img", "aria-label": label })}
    >
      {actions.map((a, i) => (
        <i
          key={a.id}
          className={`h-full ${a.success === 1 ? "bg-success" : "bg-error"}`}
          style={{ flex: 1, marginRight: i === actions.length - 1 ? 0 : gap }}
        />
      ))}
    </span>
  );
}

/** 相位头:图标 + 相位名 + 「· N 个」。徽标与支行两处共用同一个写法,字与图标不随档位变。 */
function PhaseChip({ phase, n }: { phase: string | null; n: number }) {
  const { t } = useTranslation();
  return (
    <span className="inline-flex items-center gap-1 text-caption text-text-secondary whitespace-nowrap">
      <Icon d={phaseIcon(phase)} className="w-3.5 h-3.5" />
      <span>{phaseName(phase, t)}</span>
      <span className="text-text-tertiary">{t("actions.phaseCount", { n })}</span>
    </span>
  );
}

/** 挂不上宿主的说明 chip 的外观。三处共用一套(支行两种 + 单条动作行一种),各写一份
 *  迟早走样。 */
export const CHIP_CLASS =
  "inline-flex items-center gap-1 text-caption text-text-tertiary border border-border rounded-full px-1.5 py-px whitespace-nowrap";

/** 挂在支上的那枚 chip。三种来历意思完全不同,不能合并成一句「无触发事件」:
 *  - 无触发事件:这条动作确实没有触发源(动态槽 / 人手直控)。
 *  - 早于链路记录:链路是这一版才记的,那时还没有——「没记」不是「没有」。
 *  - 触发事件未加载:**有**宿主,只是没翻到。这枚是可点的:点一下去问后端它在不在、
 *    在哪一刻(见 ActivityFeed 的 onLookupHost)。**不把它抓进列表**——那会动到用户亲手
 *    选的时间窗,而窗口是用户的东西;这枚 chip 只负责说实话。
 *
 *  单条无宿主动作行也用这一枚(那种行只会是前两种,不会问后端)。 */
export function FoldChipPill({ chip, onLookup }: { chip: FoldChip; onLookup?: () => void }) {
  const { t } = useTranslation();
  if (chip === "hostMissing" && onLookup) {
    return (
      <button
        type="button"
        onClick={onLookup}
        title={t("actions.chipHostMissingHint")}
        className={`${CHIP_CLASS} hover:text-text-primary transition-colors`}
      >
        {t("actions.chipHostMissing")}
      </button>
    );
  }
  return (
    <span className={CHIP_CLASS}>
      {chip === "noTrigger" && <Icon d={ICON.unlink} className="w-3 h-3" />}
      {chip === "noTrigger" ? t("actions.chipNoTrigger") : t("actions.chipPreLink")}
    </span>
  );
}

/* ── 事件行上的相位徽标 ─────────────────────────────────────
   整枚是一个按钮、一个语义动作一个热区,箭头是按钮内的指示符(WCAG 2.5.8)。
   每枚都自带一根状态条:徽标在事件行上、支行可能在几十行之外,两个表面会同时出现在
   一屏里,各自都得自足,谁也不能靠对方活着。

   **两档的语义不同,这是有意的**:强档的成员表就挂在事件行下,徽标是它的开合器,
   所以它写 aria-expanded/aria-controls,角标在展开后换成雪佛龙;弱档的支行是独立的
   顶层单元、锚在自己的时刻上(可能远在几十分钟之外),徽标只是它的引用——点它一律是
   "带我去那儿",不写 aria-expanded(写它就是在宣称自己开合了什么),角标恒为下钻。 */
export function FoldBadge({
  branch,
  strength,
  open,
  onActivate,
}: {
  branch: FoldBranch;
  strength: FoldStrength;
  open: boolean;
  onActivate: () => void;
}) {
  const { t } = useTranslation();
  const n = branch.actions.length;
  const ok = branch.actions.filter((a) => a.success === 1).length;
  const bad = n - ok;
  const toggleable = strength === "strong";
  const what = toggleable
    ? open
      ? t("actions.badgeCollapse")
      : t("actions.badgeExpand")
    : t("actions.badgeLocate");
  const label = t("actions.badgeAria", {
    phase: phaseName(branch.phase, t),
    n,
    ok,
    bad: bad ? t("actions.badgeBad", { n: bad }) : "",
    what,
  });
  return (
    <button
      type="button"
      data-jump={branch.key}
      onClick={(e) => {
        e.stopPropagation(); // 事件行整行是"展开详情"的热区,徽标有自己的动作
        onActivate();
      }}
      aria-label={label}
      title={label}
      {...(toggleable
        ? {
            "aria-expanded": open,
            ...(open ? { "aria-controls": membersIdOf(branch.key) } : {}),
          }
        : {})}
      className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-caption transition-colors hover:bg-bg-tertiary ${
        branch.failed ? "border-error bg-error-bg" : "border-border bg-bg-secondary"
      }`}
    >
      <span className="inline-flex items-center gap-1 whitespace-nowrap">
        <Icon d={phaseIcon(branch.phase)} className="w-3.5 h-3.5" />
        {phaseName(branch.phase, t)}
      </span>
      <StatusStrip actions={branch.actions} mini />
      <span className="text-text-tertiary">
        <Icon d={toggleable && open ? ICON.chevron : ICON.target} className="w-3 h-3" />
      </span>
    </button>
  );
}

/* ── 展开的成员表 ───────────────────────────────────────────
   两端各一个出口:头部那个 chevron 在读到表尾时早就滚出视野了,页脚这个带文字
   (一个孤零零的箭头说不出它是干什么的,而这时候人已经不在上下文里了)。 */
export function FoldMembers({
  branch,
  specs,
  onToggle,
}: {
  branch: FoldBranch;
  specs: Map<string, Record<string, BackendPropSpec>>;
  onToggle: () => void;
}) {
  const { t } = useTranslation();
  const n = branch.actions.length;
  return (
    <ul id={membersIdOf(branch.key)} aria-label={t("actions.branchAria", { n })}>
      {branch.actions.map((a, i) => (
        <ActionRow
          key={a.id}
          row={a}
          t={t}
          nested
          domId={i === 0 ? firstMemberIdOf(branch.key) : undefined}
          spec={specs.get(a.did)}
        />
      ))}
      <li className="flex justify-center">
        <button
          type="button"
          data-toggle={branch.key}
          onClick={onToggle}
          aria-expanded="true"
          aria-controls={membersIdOf(branch.key)}
          title={t("actions.collapseBranch")}
          aria-label={t("actions.collapseBranch")}
          className="text-caption text-text-secondary hover:text-text-primary inline-flex items-center gap-1 py-1.5 transition-colors"
        >
          <Icon d={ICON.chevron} className="w-3.5 h-3.5 rotate-180" />
          <span>{t("actions.collapseN", { n })}</span>
        </button>
      </li>
    </ul>
  );
}

/** 宿主事件的标题。台账里没有规则名列,事件正文就是能认出"这是哪件事"的那句;
 *  截到一行以内——长文不该把支行撑成一坨。 */
function hostTitle(event: ActivityEvent): string {
  const first = event.text.split("\n", 1)[0] ?? "";
  return first.length > 40 ? first.slice(0, 40) + "…" : first;
}

/* ── 支行(弱档主线,以及任何档位下没有宿主行的支)────────────────
   这一行的 l1 就是它的相位头——展开面里不再重复一遍相位名。 */
export function BranchRow({
  branch,
  hostEvent,
  hostRendered,
  showEvents,
  open,
  flash,
  specs,
  onToggle,
  onBack,
  onLookupHost,
}: {
  branch: FoldBranch;
  /** 宿主事件;宿主没加载时为 undefined(那就既画不出标题、也画不出回返角标)。 */
  hostEvent?: ActivityEvent;
  /** 宿主行在不在本次渲染里——在,才画得出一枚能跳的 ↩。 */
  hostRendered: boolean;
  showEvents: boolean;
  open: boolean;
  flash: boolean;
  specs: Map<string, Record<string, BackendPropSpec>>;
  onToggle: () => void;
  onBack: (eventId: string) => void;
  onLookupHost: (eventId: string) => void;
}) {
  const { t } = useTranslation();
  const n = branch.actions.length;
  const ok = branch.actions.filter((a) => a.success === 1).length;
  const gap = hostEvent ? latencyMs(branch, hostEvent) : null;
  // 用户亲手关掉事件流时,支照样画,但**不再解释宿主去哪了**——那时"未加载"是用户自己
  // 造成的,推给它一句"触发事件未加载"是把界面选择的后果说成数据缺失。
  const chip = showEvents ? branch.chip : null;

  return (
    <li
      // 行体的 id 就是这一档的滚动目标(branchRegionOf)。收起时展开面已从 DOM 里消失,
      // 若没有它,焦点与滚动就没处可落。
      id={branch.key}
      tabIndex={-1}
      className={`px-5 py-2.5 border-l-2 transition-colors ${
        branch.failed ? "bg-error-bg border-error" : "bg-success-bg border-success"
      } ${flash ? "ring-2 ring-brand-ring" : ""}`}
    >
      <div className="flex flex-col gap-1 sm:grid sm:grid-cols-[70px_1fr_auto] sm:gap-x-3 sm:gap-y-1 sm:items-baseline">
        <TimeLabel timestamp={branch.ts} />
        <div className="min-w-0 sm:order-2">
          <div className="flex items-center gap-2 flex-wrap text-body text-text-primary">
            <PhaseChip phase={branch.phase} n={n} />
            {/* 回返角标长在标题上、标题文字自己就是热区:旁边再挂一枚独立按钮既把标题
                挤开,又让读屏在同一次跳转上撞见两个控件。宿主不在(没加载 / 事件流被关)
                时退回一段不可点的纯文字——点了没反应的按钮比没有按钮更糟。 */}
            {hostRendered && hostEvent && (
              <button
                type="button"
                data-back={hostEvent.id}
                onClick={() => onBack(hostEvent.id)}
                title={t("actions.backToEvent")}
                aria-label={t("actions.backToEvent")}
                className="inline-flex items-center gap-1 min-w-0 text-caption text-text-secondary hover:text-text-primary transition-colors"
              >
                <span className="truncate max-w-[22em]">{hostTitle(hostEvent)}</span>
                <Icon d={ICON.back} className="w-3 h-3 shrink-0" />
              </button>
            )}
            {gap !== null && (
              <span className="text-caption text-text-tertiary whitespace-nowrap">
                {t("actions.latencyAfter", { d: formatLatency(gap, t) })}
              </span>
            )}
            {chip && (
              <FoldChipPill
                chip={chip}
                onLookup={() => branch.eventId && onLookupHost(branch.eventId)}
              />
            )}
          </div>
          <div className="mt-1.5">
            <StatusStrip
              actions={branch.actions}
              label={t("actions.stripAria", { n, ok, bad: n - ok })}
            />
          </div>
        </div>
        <span className="flex items-center gap-2 sm:order-last sm:justify-self-end">
          <span
            className={`text-caption px-2 py-0.5 rounded-full whitespace-nowrap ${
              branch.failed ? "text-error bg-error-bg" : "text-success bg-success-bg"
            }`}
          >
            {ok}/{n}
          </span>
          <button
            type="button"
            data-toggle={branch.key}
            onClick={onToggle}
            aria-expanded={open}
            {...(open ? { "aria-controls": membersIdOf(branch.key) } : {})}
            title={open ? t("actions.collapseBranch") : t("actions.expandBranch", { n })}
            aria-label={open ? t("actions.collapseBranch") : t("actions.expandBranch", { n })}
            className="text-text-tertiary hover:text-text-primary transition-colors"
          >
            <Icon
              d={ICON.chevron}
              className={`w-4 h-4 transition-transform ${open ? "rotate-180" : ""}`}
            />
          </button>
        </span>
      </div>
      {open && (
        // 这一片**只写 data-region、不写 id**:滚动目标是行体那个 li,它已经有 id 了,
        // 这里再写一个同值的就是文档里两个同 id 的元素,getElementById 返回哪个只看排版
        // 顺序。强档那一片(见 ActivityFeed)反过来必须有 id —— 它的行体是事件行,带的是
        // 事件 id,不是支的键。
        <div data-region={branch.key} className="mt-1">
          <FoldMembers branch={branch} specs={specs} onToggle={onToggle} />
        </div>
      )}
    </li>
  );
}

/* ── 强度分段控件 ───────────────────────────────────────────
   两档是**两个离散的模式**,不是一个连续量,所以是分段控件而不是滑块:滑块唯一的优势是
   它教得会,代价是用户拖的是自己正在读的内容;分段控件改档时同样会重排,但那是用户明确
   点了一下的结果,重排可预期。 */
export function StrengthToggle({
  strength,
  onChange,
}: {
  strength: FoldStrength;
  onChange: (s: FoldStrength) => void;
}) {
  const { t } = useTranslation();
  const items: { key: FoldStrength; label: string; title: string }[] = [
    { key: "weak", label: t("actions.foldWeak"), title: t("actions.foldWeakTitle") },
    { key: "strong", label: t("actions.foldStrong"), title: t("actions.foldStrongTitle") },
  ];
  // 单选组用 roving tabindex:未选中的那一档 tabIndex=-1,不在 Tab 序列里,靠方向键到达。
  // 少了方向键,键盘用户就够不着另一档——那比用两个普通按钮更糟。
  const onKeyDown = (e: ReactKeyboardEvent<HTMLSpanElement>) => {
    const i = items.findIndex((x) => x.key === strength);
    let next = -1;
    if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = (i - 1 + items.length) % items.length;
    else if (e.key === "ArrowRight" || e.key === "ArrowDown") next = (i + 1) % items.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = items.length - 1;
    if (next < 0) return;
    e.preventDefault();
    onChange(items[next].key);
    // 焦点跟着走:不然键盘用户改了档、焦点还停在原处,下一次方向键又从旧位置起算。
    e.currentTarget.querySelectorAll<HTMLElement>('[role="radio"]')[next]?.focus();
  };
  return (
    <span
      role="radiogroup"
      aria-label={t("actions.foldStrengthAria")}
      onKeyDown={onKeyDown}
      className="inline-flex rounded-md border border-border overflow-hidden"
    >
      {items.map((it) => (
        <button
          key={it.key}
          type="button"
          role="radio"
          aria-checked={strength === it.key}
          tabIndex={strength === it.key ? 0 : -1}
          onClick={() => onChange(it.key)}
          title={it.title}
          className={`text-caption px-2 py-0.5 transition-colors ${
            strength === it.key
              ? "bg-brand-soft text-text-primary"
              : "text-text-secondary hover:bg-bg-tertiary"
          }`}
        >
          {it.label}
        </button>
      ))}
    </span>
  );
}
