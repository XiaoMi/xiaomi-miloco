/**
 * 折叠视图的行装配——纯函数,组件只负责把它画出来。
 *
 * 「折叠」这件事的全部判断都在这里:哪些动作属于哪条支、哪些行落在时间窗内、
 * 两档强度各生成哪些行。抽出来的理由有两条:
 * - 测试环境没有 DOM(见 tests/ActionsFeed.test.ts 头的覆盖声明),装配规则
 *   只有在纯函数形态下才守得住;
 * - 折叠与筛选必须**正交**——筛选管「有什么」、折叠管「怎么画」,两套规则搅在
 *   一个组件的渲染分支里时,「勾掉事件后支还在不在」这种问题只能靠肉眼。
 *
 * 数据侧的三个事实决定了这里的形状(见 lib/types 与后端台账):
 * - 台账的 `trigger_event_id` 指向宿主事件主键,`phase` 是 'enter' / 'exit';
 *   两者恒为空的只有两类行:动态槽 / 人手直控(cli 写入),与 v5 迁移前的历史行
 *   (迁移时逐行打了 phase='legacy')。**退出支不是孤儿**——它携带 enter 行的 id。
 * - 事件按页取(50/页)、动作一次取回(上限 500),两条流的深度差着数量级,于是
 *   「动作的宿主还没加载」是一个常态而不是边角:宿主的时刻早于动作,它可能落在
 *   已加载页之下。这类支照样画,只是它的宿主行不在这场渲染里。
 * - 时间窗打在**块**上:宿主在窗内,它的支整批显示;宿主在窗外而某条支在窗内,
 *   宿主行也留着(打「范围外·作为宿主显示」),否则那条真发生在窗内的动作会无家可归。
 */

import type { ActivityEvent } from "./types";
import type { ActionLike } from "./actionText";

/** 折叠强度:一根轴的两端,没有中间档。
 *  - `weak` 各行其时:每条支一行,锚在自己的时刻上。
 *  - `strong` 并入事件:动作不占行,状态收进事件行的徽标。 */
export type FoldStrength = "weak" | "strong";

/** 本模块只读台账行的这几个字段——用结构类型而不是 import 组件的 BackendActionRow,
 *  免得 lib 层反向依赖 components 层(同 lib/actionText 的理由)。 */
export interface FoldActionLike extends ActionLike {
  id: string;
  timestamp: number;
  did: string;
  device_name: string | null;
  room: string | null;
  success: 0 | 1;
  result_msg: string | null;
  error: string | null;
  /** v5:相位。'enter' | 'exit' | 'legacy'(迁移前的历史行);无链路的行恒 null。 */
  phase?: string | null;
  /** v5:宿主事件主键。null = 这条动作没有触发源(或早于链路记录)。 */
  trigger_event_id?: string | null;
}

/** 挂不上宿主时那枚 chip 的种类。三种意思完全不同,不能合并:
 *  - `noTrigger`   这条动作确实没有触发源(动态槽 / 人手直控,source='cli')。
 *  - `preLink`     链路记录之前的历史行——「我们那时还没记」,不是「它没有」。
 *  - `hostMissing` 有宿主,但宿主没加载(翻页深度不够)。 */
export type FoldChip = "noTrigger" | "preLink" | "hostMissing";

/** 一条支 = 同一个宿主事件的同一个相位下的一批动作。 */
export interface FoldBranch {
  /** 展开集合的键。宿主相同、相位相同即同一条支。 */
  key: string;
  /** 宿主事件 id;null = 这条支挂不上任何事件。 */
  eventId: string | null;
  phase: string | null;
  /** 支自己的时刻 = 最后一个成员的时刻(弱档据它落位)。 */
  ts: number;
  actions: FoldActionLike[];
  /** 支内只要有失败成员就为真——整支转红、状态条出红段。 */
  failed: boolean;
  /** 无宿主时打的 chip;有宿主(不论加载与否)时为 null。 */
  chip: FoldChip | null;
}

/** 折叠视图的一行。**行是渲染单位,不是数据单位**:同一批动作在弱档是「一条支一行」,
 *  在强档收进事件行、自己 0 行,故档位决定生成哪些行。 */
export type FoldRow =
  /** 事件行。`branches` 恒为「属于它且在窗内可见」的支——哪一档都靠它画徽标。 */
  | {
      kind: "event";
      key: string;
      ts: number;
      event: ActivityEvent;
      branches: FoldBranch[];
      /** 宿主自己在用户窗口外,只是因为有支在窗内才留着(打「范围外·作为宿主显示」)。 */
      hostOutside: boolean;
    }
  /** 顶层独立支:弱档下每条支都是一行;任何档位下没有宿主行的支都落到这里。 */
  | {
      kind: "branch";
      key: string;
      ts: number;
      branch: FoldBranch;
      /** 宿主行在不在本次渲染里——在,才画得出一枚能跳的 ↩。 */
      hostRendered: boolean;
    }
  /** 单条、且挂不上任何支的动作。单独成一类而不是塞进「只有一个成员的支」,是因为
   *  它**没有相位可说**:画成支行会顶着一枚空相位头、还把设备名与人话藏进展开面,
   *  比今天的信息还少。它按原样画一行动作,只多一枚说明来历的 chip。 */
  | { kind: "action"; key: string; ts: number; action: FoldActionLike; chip: FoldChip };

/** 单流的时间下界 —— 两个下界取**较晚**的那个。纯函数,导出供 tests 与截断提示复用。
 *
 *  两个下界各管一件事,谁都不能替代谁:
 *  - `sinceMs`:用户显式筛的起点。权威硬界,即使一条事件都没有也生效
 *    (修过的老 bug:事件为空时动作曾无下界、混入范围外历史动作)。
 *  - **事件地平线** = 最旧一条**已加载**事件的 ts。事件按 PAGE_SIZE 分页、动作一次
 *    拉 ACTIONS_LIMIT 条,两条流取数深度差着数量级;地平线以下服务端还有事件没拉,
 *    此时若把动作放出来,列表尾部就成了一整段"只有动作、没有事件"的墙。
 *
 *  取 max 而不是 `??` 是这里的关键。老代码写的是 `sinceMs ?? 地平线`,而默认视图的
 *  since 恒为今天 00:00 —— 永远有值,`??` 永远短路,地平线那支是死代码,于是第 50 条
 *  事件以下全是动作。见 tests「sinceMs 已定义时地平线仍生效」。
 *
 *  `hasMoreEvents=false`(该窗口的事件已全部加载)时不设地平线:底下没有未加载的事件,
 *  动作可以一直铺到 sinceMs。默认 true 是保守侧——不知道有没有更多时,宁可裁。
 */
export function feedLowerBound(
  events: ActivityEvent[],
  showEvents: boolean,
  sinceMs?: number,
  hasMoreEvents = true,
): number {
  const horizon =
    showEvents && hasMoreEvents && events.length > 0
      ? Math.min(...events.map((e) => e.timestamp))
      : -Infinity;
  return Math.max(sinceMs ?? -Infinity, horizon);
}

export interface BuildFoldRowsInput {
  events: ActivityEvent[];
  actions: FoldActionLike[];
  showEvents: boolean;
  showActions: boolean;
  strength: FoldStrength;
  /** 用户显式筛的起点;undefined = 不限。 */
  sinceMs?: number;
  /** 用户筛的截止;undefined = 至现在。 */
  beforeMs?: number;
  /** 事件是否还有更早的没加载(决定要不要压事件地平线,见 feedLowerBound)。 */
  hasMoreEvents?: boolean;
}

/** 支的分组键:`宿主 + 相位`。分隔符取双冒号——event_id 是 uuid、相位是 'enter' /
 *  'exit',两段都不可能含冒号,故拼出来不会串。 */
function branchKeyOf(eventId: string, phase: string | null): string {
  return eventId + "::" + (phase ?? "");
}

/** 无宿主动作的 chip:有相位说明它走过规则链,只是那条链在链路记录之前。 */
export function orphanChipOf(action: FoldActionLike): FoldChip {
  return action.phase === "legacy" ? "preLink" : "noTrigger";
}

/** 装配折叠视图的行。返回的行已按「新的在前」排好,**同秒时动作在它的事件之前**
 *  (降序里动作更靠前)。
 *
 *  这条并列规则是从「回返角标恒为直落」倒推出来的:弱档的支行锚在自己(更晚)的时刻上,
 *  它的事件因此**永远在它下方**,直落箭头恒真;只有当两者同秒并列时先后才有歧义,
 *  按这个方向排,歧义那一半也落在同一边。反过来的排法(事件在前)会让同一屏里的角标
 *  一半指上、一半指下。 */
export function buildFoldRows(input: BuildFoldRowsInput): FoldRow[] {
  const { events, actions, showEvents, showActions, strength } = input;
  if (!showEvents && !showActions) return [];
  const lower = feedLowerBound(events, showEvents, input.sinceMs, input.hasMoreEvents);
  const upper = input.beforeMs ?? Infinity;
  const inWin = (ts: number) => ts >= lower && ts <= upper;

  // ── 分组:按宿主事件 + 相位收成支;挂不上宿主的单列出来 ──
  const hostById = new Map(events.map((e) => [e.id, e]));
  const branchMap = new Map<string, FoldBranch>();
  const branchOrder: FoldBranch[] = [];
  const singles: FoldActionLike[] = [];

  if (showActions) {
    for (const a of actions) {
      const eventId = a.trigger_event_id ?? null;
      if (eventId === null) {
        singles.push(a);
        continue;
      }
      const key = branchKeyOf(eventId, a.phase ?? null);
      let b = branchMap.get(key);
      if (!b) {
        b = {
          key,
          eventId,
          phase: a.phase ?? null,
          ts: a.timestamp,
          actions: [],
          failed: false,
          chip: hostById.has(eventId) ? null : "hostMissing",
        };
        branchMap.set(key, b);
        branchOrder.push(b);
      }
      b.actions.push(a);
      b.ts = Math.max(b.ts, a.timestamp);
      if (a.success !== 1) b.failed = true;
    }
  }

  const branchesOf = new Map<string, FoldBranch[]>();
  for (const b of branchOrder) {
    if (b.eventId === null) continue;
    const list = branchesOf.get(b.eventId);
    if (list) list.push(b);
    else branchesOf.set(b.eventId, [b]);
  }

  /** 支在本次渲染里可见吗——**块级命中**:宿主在窗内,整块显示;宿主在窗外,
   *  只要有一条支在窗内,这条支也显示(它的时刻才是真落在用户选的范围里的)。
   *  宿主没加载的支没有块可依,退回逐行判据。 */
  const branchVisible = (b: FoldBranch, hostIn: boolean): boolean =>
    hostIn || b.actions.some((a) => inWin(a.timestamp));

  const rows: FoldRow[] = [];

  // ── 事件行 ──
  if (showEvents) {
    for (const e of events) {
      const hostIn = inWin(e.timestamp);
      const branches = (branchesOf.get(e.id) ?? []).filter((b) => branchVisible(b, hostIn));
      // 事件行本身无条件画——事件流是服务端按窗取回来的,前端再裁一遍会把用户
      // 现在看得见的事件藏掉。「作为宿主显示」那枚 chip 只标**因支而留**的那些。
      rows.push({
        kind: "event",
        key: e.id,
        ts: e.timestamp,
        event: e,
        branches,
        hostOutside: !hostIn && branches.length > 0,
      });
    }
  }

  // ── 支:强档收进事件行(0 行),弱档各自成行;没有宿主行可依的一律成行 ──
  for (const b of branchOrder) {
    const host = b.eventId === null ? undefined : hostById.get(b.eventId);
    const hostIn = host ? inWin(host.timestamp) : false;
    const visible = host ? branchVisible(b, hostIn) : b.actions.some((a) => inWin(a.timestamp));
    if (!visible) continue;
    if (strength === "strong" && host !== undefined && showEvents) continue; // 已并进事件行
    rows.push({
      kind: "branch",
      key: b.key,
      ts: b.ts,
      branch: b,
      hostRendered: showEvents && host !== undefined,
    });
  }

  // ── 无宿主动作:没有相位头可画,按原样一行一条 ──
  for (const a of singles) {
    if (!inWin(a.timestamp)) continue;
    rows.push({ kind: "action", key: a.id, ts: a.timestamp, action: a, chip: orphanChipOf(a) });
  }

  // 新的在前;同一时刻动作排在它的事件前面(降序里更靠前),见函数头。
  const rank = (r: FoldRow) => (r.kind === "event" ? 1 : 0);
  return rows.sort((x, y) => (y.ts !== x.ts ? y.ts - x.ts : rank(x) - rank(y)));
}

/** 折叠视图的两个计数。**按数据算,不按行算**:强档把动作折进徽标、一条行都不占,
 *  弱档把同一批动作拆成好几行——两种画法下「已加载多少条」必须是同一个数,否则
 *  切一下档位计数就变,而那批数据一个字节都没动。
 *
 *  一支可能在事件行和自己的支行里各出现一次(弱档),按 key 去重,别数两遍。 */
export function foldCounts(rows: FoldRow[]): { events: number; actions: number } {
  let events = 0;
  let actions = 0;
  const seen = new Set<string>();
  const takeBranch = (b: FoldBranch) => {
    if (seen.has(b.key)) return;
    seen.add(b.key);
    actions += b.actions.length;
  };
  for (const r of rows) {
    if (r.kind === "event") {
      events += 1;
      for (const b of r.branches) takeBranch(b);
    } else if (r.kind === "branch") {
      takeBranch(r.branch);
    } else {
      actions += 1;
    }
  }
  return { events, actions };
}

/** 退出支距它的事件多久 —— 弱档把这枚徽标打在支行上,补的是「因果链被时间轴冲散」
 *  这个代价:退出支可能落在几十行之外,它的真实时刻在左列,两者的间隔只有这枚徽标
 *  说得清。返回 null 表示「这一档下没有可说的间隔」:不是退出支,或宿主不在手上。 */
export function latencyMs(branch: FoldBranch, event: ActivityEvent | undefined): number | null {
  if (!event || branch.phase !== "exit") return null;
  const d = branch.ts - event.timestamp;
  return d > 0 ? d : null;
}

/** i18next 的 t 收敛成最小签名——本模块只用 key 与插值两件事(同 lib/actionText)。 */
export type Translate = (key: string, options?: Record<string, unknown>) => string;

/** 毫秒 → 「41 分 25 秒」。粒度只到分和秒:这条徽标要解释的是「相隔几十行」,
 *  那是分秒级的差距;到了「多少天」这个尺度,精确到秒只是噪音,天数照旧保留。
 *
 *  单位走 i18n(四档各一个键),不在这里拼中文——en 界面下「41 分」是 bug。
 *  拼法固定为「大单位在前、空格分隔」,zh 得到 `41 分 25 秒`、en 得到 `41m 25s`。 */
export function formatLatency(ms: number, t: Translate): string {
  const total = Math.round(ms / 1000);
  const parts: string[] = [];
  const d = Math.floor(total / 86400);
  const h = Math.floor((total % 86400) / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = total % 60;
  if (d) parts.push(t("actions.latencyDay", { n: d }));
  if (h) parts.push(t("actions.latencyHour", { n: h }));
  if (m) parts.push(t("actions.latencyMin", { n: m }));
  if (sec || parts.length === 0) parts.push(t("actions.latencySec", { n: sec }));
  return parts.join(" ");
}
