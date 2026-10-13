/**
 * 「家里发生了什么」时间倒序流(meaningful_events / Mi Console v3 视觉)
 *
 * 数据源:GET /api/events(perception/events_router).一次推理 1 行.
 * 行展示:左 mono 时间 + 主区聚合 text(按 \n\n 分章节,规则段经
 *        humanizeRulesInText 把 rule_id 换成 rule_name).
 * 行展开:Accordion 显示 device × 3 张截图,缺图时显占位.
 * 实时更新:订阅 /api/events/stream SSE,新事件 prepend 到列表顶.
 * 时间筛选:datetime-local 双输入(自 / 至),非法值守(NaN 不更新 state).
 */

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  eventClipUrl,
  eventCropMeta,
  eventRefUrl,
  listActivity,
  listOnDemandLogs,
  onDemandClipUrl,
  revealDir,
  submitEventFeedback,
  submitOnDemandFeedback,
  subscribeEvents,
} from "@/api";
import {
  humanizeRulesInText,
  splitHumanizedSections,
  type TriggerStatusKind,
} from "@/lib/eventText";
import type { ActivityEvent, EventCropMeta, HomeId, OnDemandLogEntry } from "@/lib/types";

/** Lightbox 内容类型:clip 走 <video>,Smart Crop 参考帧走 <img>. */
type LightboxKind = "video" | "image";
import {
  ACTIONS_LIMIT,
  ActionRow,
  fetchActions,
  type BackendActionRow,
} from "./ActionsFeed";
import { TimeLabel } from "./TimeLabel";
import { toast } from "./Toast";

type ActivityTab = "events" | "queries";

interface Props {
  events: ActivityEvent[];
  /** 当前作用域 home;切换时整个列表 + SSE 都要重建 */
  homeId: HomeId;
  /** 真实的当前 in_use 家庭 id(来自 scope homes;HomeId 只是缓存 key 占位)。
   *  传入后动作流带 home_id 过滤——多 home 下切家不再串入他家动作;
   *  scope 未加载完时为 undefined → 先不过滤,到达后 reloadActions 依赖变化自动重拉。 */
  activeHomeId?: string;
  /** 事件初始页仍在加载。App 现无条件挂载本组件(让动作流独立于事件加载态),事件到达后
   *  经 setEvents 合并;加载中在事件区顶部显一条内联提示,不阻断动作流。 */
  eventsLoading?: boolean;
  /** 事件初始页加载失败——内联提示 + 重试,同样不阻断动作流。 */
  eventsError?: Error | null;
  onRetryEvents?: () => void;
  onDemandLogs?: OnDemandLogEntry[];
  onDemandLoading?: boolean;
  onDemandError?: Error | null;
  onRetryOnDemand: () => void;
  deviceNames?: Record<string, string>;
}

const PAGE_SIZE = 50;
const OD_PAGE_SIZE = 50;
const FILTER_DEBOUNCE_MS = 300;
const FEEDBACK_FORM_URL = "https://mi.feishu.cn/share/base/form/shrcnUmo9ez8NwkcpvpJsKSOdgd";
const EMPTY_OD_LOGS: OnDemandLogEntry[] = [];
// SSE 事件常成串到达(一次 agent 控制伴随多条事件),每条都全量重拉 500 行动作太重。
// 合并突发:末条到达后 ~1.5s 才拉一次(trailing debounce)。mount / homeId 切换仍即时拉。
const SSE_ACTIONS_DEBOUNCE_MS = 1500;

/** 单流合并后的行:事件 or 动作(tagged union),供渲染层分派 ActivityRow / ActionRow。 */
export type FeedRow =
  | { kind: "event"; ts: number; event: ActivityEvent }
  | { kind: "action"; ts: number; action: BackendActionRow };

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

/** 一次事件取数的三种语义。见 fetchPage。 */
export type FetchMode = "replace" | "append" | "refresh";

/** `refresh` 专用:第 0 页与手里最新的事件**接不接得上**。
 *
 *  接不上(`fresh` 的最旧一条仍比 `prev` 的最新一条更新)= 断线期间新增超过一页、
 *  中间空出一段。接得上则第 0 页与已有数据重叠,说明这段断开没漏掉东西。
 *
 *  `prev` 为空(手里没有可比对的)时当接不上——保守侧:底下有没有更早的仍是未知。 */
export function hasGapAbove(prev: ActivityEvent[], fresh: ActivityEvent[]): boolean {
  if (fresh.length === 0) return false;
  if (prev.length === 0) return true;
  return Math.min(...fresh.map((e) => e.timestamp)) > Math.max(...prev.map((e) => e.timestamp));
}

/** 取数成功后 `hasMore` 怎么变。抽成纯函数是因为这个标记**身兼两职**——
 *  既决定「查看更早」按钮显不显,也决定 feedLowerBound 要不要压事件地平线——
 *  写错一次就会把动作地平线整个关掉,退回那堵只有动作的墙。
 *
 *  - `replace` / `append`:满页即可能还有更早,短页即到底。分页的权威答案。
 *  - `refresh`(SSE 重连补漏):**只升不降,且抬升要求真的接不上**。两个方向都会出事:
 *    让它把短页给出的"已到底"打成 false,一次重连就能让「查看更早」永久消失、
 *    地平线失效,而列表中间还留着空洞;反过来让满页无条件抬成 true,一次与数据无关
 *    的重连就能把"已到底"推翻,让已经加载完整的窗口重新压上地平线——窗内更早的动作
 *    被裁掉,还挂着"更早的事件与动作尚未加载",而事件其实一条不缺。
 *    第 0 页满页只说明"上面至少还有一页",答不了"我手里这批的下面还有没有";
 *    它能抬升的唯一理由是第 0 页与手里数据之间空了段(hasGapAbove)。 */
export function nextHasMore(
  mode: FetchMode,
  prevHasMore: boolean,
  freshCount: number,
  gapAbove = false,
): boolean {
  const full = freshCount === PAGE_SIZE;
  return mode === "refresh" ? prevHasMore || (full && gapAbove) : full;
}

/** 取数成功后 `offset` 怎么变 —— 只进不退。
 *  refresh 拉的永远是第 0 页,若直接写 pageOffset+len,已翻到第 4 页的用户会被打回
 *  offset=50,下次「查看更早」重拉已有页。 */
export function nextOffset(prevOffset: number, pageOffset: number, freshCount: number): number {
  return Math.max(prevOffset, pageOffset + freshCount);
}

/** 取数成功后 `events` 怎么变 —— `replace` 硬替换,`append` / `refresh` 都 merge
 *  (按 id 去重 + 按 ts 重排)。
 *
 *  `refresh` 走 merge 是本 PR 的病灶本身:老代码这里走的是 replace 分支,一次重连
 *  就把用户翻了几页的事件打回第 0 页。抽成纯函数是为了让这条分派有红灯 ——
 *  写反时测试变红,而不是像老代码那样带着绿灯上线。 */
export function nextEvents(
  mode: FetchMode,
  prev: ActivityEvent[],
  fresh: ActivityEvent[],
): ActivityEvent[] {
  return mode === "replace" ? fresh : mergeAndSort(prev, fresh);
}

/** 事件流 + 动作流合并成单条时间倒序流。纯函数,导出供 tests 守 window + 交错顺序。
 *
 *  窗口规则:动作只保留 `[feedLowerBound, beforeMs]` 内的行——即同时受用户筛选段和
 *  事件地平线约束(见 feedLowerBound)。展示事件为空 / 事件 checkbox 关时不设地平线。
 *
 *  同 ts 时事件排在动作前(事件是"发生了什么"、动作是"因此做了什么",因果上事件在先)。
 */
export function mergeFeedRows(
  events: ActivityEvent[],
  actions: BackendActionRow[],
  showEvents: boolean,
  showActions: boolean,
  sinceMs?: number,
  beforeMs?: number,
  hasMoreEvents = true,
): FeedRow[] {
  const rows: FeedRow[] = [];
  if (showEvents) {
    for (const e of events) rows.push({ kind: "event", ts: e.timestamp, event: e });
  }
  if (showActions) {
    const lower = feedLowerBound(events, showEvents, sinceMs, hasMoreEvents);
    const upper = beforeMs ?? Infinity;
    for (const a of actions) {
      if (a.timestamp >= lower && a.timestamp <= upper) {
        rows.push({ kind: "action", ts: a.timestamp, action: a });
      }
    }
  }
  // ts DESC;同 ts 事件优先(event 在 action 前)。
  rows.sort((x, y) => {
    if (y.ts !== x.ts) return y.ts - x.ts;
    if (x.kind === y.kind) return 0;
    return x.kind === "event" ? -1 : 1;
  });
  return rows;
}

/** 合并两段 event 列表:by id dedup(后到的字段优先)+ timestamp DESC 排序.
 *
 *  解决并发场景:
 *  - "查看更早" 翻页 fetch 期间 SSE 推了几条新事件 → 老/新混在内存,需 dedup
 *  - backend `offset` 翻页跟 SSE 实时插入是独立两条流,合并时容易乱序 → 显式按 ts 重排
 *  - 同 event_id 出现两次(SSE + reload 都拿到同一条)→ 后到的赢
 *
 *  导出供 tests/ActivityFeed-merge.test.ts 守 dedup + 排序两个 invariant.
 */
export function mergeAndSort(
  primary: ActivityEvent[],
  secondary: ActivityEvent[],
): ActivityEvent[] {
  const byId = new Map<string, ActivityEvent>();
  for (const e of primary) byId.set(e.id, e);
  for (const e of secondary) byId.set(e.id, e); // secondary 覆盖 primary 同 id
  return Array.from(byId.values()).sort((a, b) => b.timestamp - a.timestamp);
}

/** 今天 00:00 local 的 Unix ms — 默认 since(实时态起点);用户点 "↻ 实时" 也回到这里. */
function todayStartMs(): number {
  const d = new Date();
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

export function ActivityFeed({
  events: initial,
  homeId,
  activeHomeId,
  eventsLoading,
  eventsError,
  onRetryEvents,
  onDemandLogs: initialOdLogs,
  onDemandLoading,
  onDemandError,
  onRetryOnDemand,
  deviceNames = {},
}: Props) {
  const { t } = useTranslation();
  const [activeTab, setActiveTab] = useState<ActivityTab>("events");
  const [odCount, setOdCount] = useState((initialOdLogs ?? EMPTY_OD_LOGS).length);
  const [odHasMore, setOdHasMore] = useState((initialOdLogs ?? EMPTY_OD_LOGS).length === OD_PAGE_SIZE);
  const handleOdCount = useCallback((n: number, hasMore: boolean) => {
    setOdCount(n);
    setOdHasMore(hasMore);
  }, []);
  // 初始为空:App 层 useAsync 不带 since 参数,拿到的是全时段事件;本组件默认
  // since=todayStartMs(),若直接用 initial 初始化会在首帧闪现历史日志,随后被
  // fetchPage(带 since)替换——即 "切 tab 闪一下旧日志再变空" 的 bug。
  // 正确路径:filterActive 时由 fetchPage 填充;!filterActive 时由 sync effect 填充。
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  // since 默认今天 00:00,跟标题语义对齐;before 留空 → 后端取 now,允许"看到现在".
  // 用户改 since 看更早历史 / 设 before 卡截止 / 清 since 看全量.
  const [since, setSince] = useState<number | undefined>(todayStartMs);
  const [before, setBefore] = useState<number | undefined>();
  /** debounced 版本,用于 SSE useEffect deps / filter fetch — 避免 datetime-local
   *  每字符 onChange 触发 EventSource 频繁建/拆 (N3) + reload churn */
  const [appliedSince, setAppliedSince] = useState<number | undefined>(todayStartMs);
  const [appliedBefore, setAppliedBefore] = useState<number | undefined>();
  // 默认 filterActive=true → mount 即 fetchPage;起始 true 让首帧显"加载中"而非闪"暂无"。
  const [loading, setLoading] = useState(true);
  const [feedbackSet, setFeedbackSet] = useState<Set<string>>(new Set());
  const [feedbackPacks, setFeedbackPacks] = useState<Map<string, { path: string; size: number }>>(new Map());
  /** 已拉取的 offset(下次"查看更早"从这开始).事件量动态变(SSE prepend),
   *  分页用 since/before+offset 而非纯 offset 不够;约定 offset = 当前已 loaded 历史段长 */
  const [offset, setOffset] = useState(0);
  /** 后端最近一次 GET 是否还满 PAGE_SIZE(可能还有更早) */
  const [hasMore, setHasMore] = useState(false);
  /** 组件内取数失败(带筛选的那条请求)的消息。App 层的 eventsError 覆盖不到这里:
   *  它反映的是**不带筛选**的那次请求。失败后列表会被清空,只有瞬时 toast 的话,
   *  失败后的画面(事件 0 条 + 动作照旧)与"这个时间段没数据"肉眼难分,故留成 state
   *  由 banner 常驻,并自带重试入口。null = 本次取数没有失败。 */
  const [fetchError, setFetchError] = useState<string | null>(null);
  /** Promise generation token — stale fetch resolve 时丢弃(N1) */
  const fetchGenRef = useRef(0);
  /** 是否有取数在途。refresh(重连补漏)凭此决定跳过 —— 理由见 fetchPage 入口守卫。 */
  const fetchInFlightRef = useRef(false);
  /** `events` 的最新值,fetch resolve 时读。闭包里的 `events` 是**发请求那一刻**的快照,
   *  resolve 时可能已被 SSE prepend 过;判"第 0 页与手里数据接不接得上"必须用最新值——
   *  拿旧快照比,会把其实重叠的两段误判成空洞,反过来把地平线误开。 */
  const eventsRef = useRef<ActivityEvent[]>([]);
  /** 全屏播放器(点开看大):null 关闭.kind 决定用 <video> 还是 <img>(参考帧是 JPEG);
   *  crop 由参考帧卡透上来(它已经拉过坐标),放大后继续画框、不重复请求. */
  const [lightbox, setLightbox] = useState<{
    src: string;
    kind: LightboxKind;
    crop?: EventCropMeta | null;
  } | null>(null);

  // ── 单流:事件 / 动作两个 checkbox 筛选(默认都勾),动作一次拉全后 merge ──
  const [showEvents, setShowEvents] = useState(true);
  const [showActions, setShowActions] = useState(true);
  const [actions, setActions] = useState<BackendActionRow[]>([]);

  /** 动作拉取的 generation token(N1 同款,镜像事件流的 fetchGenRef):首屏先发的
   *  无 home 过滤请求 / 切家前旧请求若晚返回,不得覆盖已按新 home 过滤的结果。 */
  const actionsGenRef = useRef(0);

  /** 动作重拉:mount / homeId 切换 / 时间窗变化 / 手动 reload 时调,失败静默(不阻断事件流)。
   *  带上当前应用的时间窗(appliedSince/appliedBefore),让动作与事件同段,不混入范围外记录;
   *  带上 activeHomeId,切家后动作流只显当前家(依赖变化自动重拉,不再是空转)。
   *  只允许最新一代请求 setActions——stale 响应直接丢弃。 */
  const reloadActions = useCallback(() => {
    const gen = ++actionsGenRef.current;
    fetchActions(false, appliedSince, appliedBefore, activeHomeId)
      .then((rows) => {
        if (gen === actionsGenRef.current) setActions(rows);
      })
      .catch(() => {
        /* 动作流失败不影响事件流;保留上次结果 */
      });
  }, [appliedSince, appliedBefore, activeHomeId]);

  /** SSE 触发的动作重拉:trailing debounce 合并突发,避免每条事件都全量拉 500 行。 */
  const sseReloadTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const debouncedReloadActions = useCallback(() => {
    if (sseReloadTimerRef.current) clearTimeout(sseReloadTimerRef.current);
    sseReloadTimerRef.current = setTimeout(() => {
      sseReloadTimerRef.current = null;
      reloadActions();
    }, SSE_ACTIONS_DEBOUNCE_MS);
  }, [reloadActions]);

  // 卸载时清掉悬挂的 debounce 定时器,避免 setState-after-unmount。
  useEffect(
    () => () => {
      if (sseReloadTimerRef.current) clearTimeout(sseReloadTimerRef.current);
    },
    [],
  );

  // mount 时拉一次动作(homeId 变也重拉——切家后动作流应随之刷新)。即时,不 debounce。
  useEffect(() => {
    reloadActions();
  }, [reloadActions, homeId]);

  const filterActive = appliedSince !== undefined || appliedBefore !== undefined;

  // N3: filter input 抖动 debounce 300ms 后才应用 → 触发 fetch + SSE 重建
  useEffect(() => {
    const t = setTimeout(() => {
      setAppliedSince(since);
      setAppliedBefore(before);
    }, FILTER_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [since, before]);

  /** 统一拉取(filter / 翻页 / SSE 重连 reload 共用),fetchGen 守 stale overwrite.
   *
   *  三种模式必须分开,不能只靠一个 append 布尔:
   *  - `replace` 筛选段 / 切家变了,旧列表整体作废 → 硬替换。
   *  - `append`  「查看更早」翻页 → merge 进已有列表,offset 前进。
   *  - `refresh` SSE 重连补漏 → **也必须 merge**。老代码这里走的是 replace 分支,
   *    于是用户翻了四页攒到 200 条事件,网络抖一下 / 笔记本睡醒 / 后端重启,
   *    列表就被 setEvents(fresh) 打回 50 条;而 500 条动作走的是另一条取数路径、
   *    毫发无损 —— 字面意义上的"刚才还在的事件不见了",且不报错不留痕。
   *
   *  失败路径同样按模式分,不是统一一句"失败了":`replace` 列表与分页深度一起作废、
   *  并留下常驻 banner(可重试),`append` 保留列表与按钮、只出声,`refresh` 静默(重连
   *  补漏失败不动任何状态,等下一次重连的补漏 —— SSE 无重放,推送不会替它补)。
   *
   *  refresh 还有一条独有规则:任何取数在途时它**直接跳过**,不抢占代际令牌(理由见入口守卫)。 */
  const fetchPage = (opts: {
    mode?: FetchMode;
    pageOffset?: number;
  }) => {
    const mode = opts.mode ?? "replace";
    // refresh 是尽力而为的后台补漏,不参与代际令牌竞争:任何取数在途时直接跳过。
    // 否则它的 `++fetchGenRef.current` 会把**代表用户意图的那次取数**判成陈旧丢掉——
    // 丢的不只是结果:replace 的列表替换与失败上报一起作废(新筛选的取数没落地、
    // 该有的报错也没出现,用户只看到一次毫无反馈的切换),append 的翻页结果被吞掉;
    // 而 refresh 自己照常 merge,把**新**筛选的第 0 页倒进**旧**筛选的列表里
    // (事件在合流处不做窗口二次过滤),offset 还停在旧视图的深度上,之后翻页静默跳过一段。
    // 跳过不丢数据:replace 本来就拉同一窗口的第 0 页,与 refresh 想拿的是同一份;
    // 与 append 撞上时丢的是一次补漏 —— 且这段**不会自己回来**:SSE 是纯直播流、不重放
    // (后端 events_router 的 subscribe_sse 收的是订阅之后的广播,没有 Last-Event-ID / 补发),
    // 断线窗口内的事件不会被"后续推送"补发;能补回的只有下一次重连的补漏(且那些事件
    // 仍落在第 0 页 50 条以内)或下一次 replace(时间范围变化 / 切家)。
    // (已知代价:listActivity 没有超时,请求若悬挂不 settle,旗子会一直立着、补漏不触发,
    //  直到用户下一次亲手触发 replace / append 落地才收旗。悬挂期间症状**不一定可见**:
    //  组件内 loading 的渲染出口只有两处(空列表居中的"加载中"、以及需 showLoadMore
    //  成立的「查看更早」按钮),banner 的加载文案由 App 层 eventsLoading 驱动、与组件内
    //  悬挂无关 —— 窗口 hasMore=false 且列表非空时画面看不出异常,唯一后果是补漏停摆。
    //  属既有毛病(取数无超时),不在本次范围。)
    if (mode === "refresh" && fetchInFlightRef.current) return Promise.resolve();
    const gen = ++fetchGenRef.current;
    fetchInFlightRef.current = true;
    const pageOffset = opts.pageOffset ?? 0;
    setLoading(true);
    // 只有 replace 在开跑时清掉上次的失败:它是"当前视图从头再来一遍",旧失败已不代表现在;
    // append / refresh 是后台动作,清掉会让一次静默失败的 refresh 抹掉用户还没看的报错。
    if (mode === "replace") setFetchError(null);
    return listActivity(homeId, {
      since: appliedSince,
      before: appliedBefore,
      limit: PAGE_SIZE,
      offset: pageOffset,
    })
      .then((fresh) => {
        if (gen !== fetchGenRef.current) return; // N1: stale,丢弃
        // merge 的两种模式(append / refresh)其期间 SSE 都可能已经 prepend 新事件,
        // 简单 [...prev, ...fresh] 会让"更早的 fresh"夹在"SSE 推的更晚事件"中间 →
        // 视觉乱序。mergeAndSort 按 id dedup + 按 timestamp DESC 重排兜底,得到稳定顺序。
        setEvents((prev) => nextEvents(mode, prev, fresh));
        setFetchError(null);
        if (mode === "replace") setOffset(fresh.length);
        else setOffset((prev) => nextOffset(prev, pageOffset, fresh.length));
        // 判据要读**最新**列表(eventsRef),不是这次渲染闭包里的快照 —— 理由见 eventsRef 声明处。
        const gapAbove = hasGapAbove(eventsRef.current, fresh);
        setHasMore((prev) => nextHasMore(mode, prev, fresh.length, gapAbove));
      })
      .catch((e: unknown) => {
        if (gen !== fetchGenRef.current) return;
        // **不动 hasMore**。请求失败不回答"还有没有更早"这个问题,而 hasMore 同时
        // 是动作地平线的闸:把它打成 false,一次失败的「查看更早」就会把地平线关掉,
        // 底下重新涌出那堵只有动作的墙,而且按钮同时消失、无法重试。保持原值 →
        // 按钮还在、地平线还在,用户可以再点一次。
        if (mode === "replace") {
          // 列表整体作废,分页深度必须跟着作废:offset 的契约是"已 loaded 历史段长",
          // 列表清空后旧深度就是谎报。不清零的话下一次重连的 refresh 会把旧深度钉死
          // (nextOffset 只进不退),再点「查看更早」就从旧深度起拉,中间那段被静默跳过
          // —— 又一种"事件悄悄不见"。清空与归零必须成对出现。
          setEvents([]);
          setOffset(0);
          // 失败要留下比 toast 活得久的痕迹:失败后的画面(事件 0 条 + 动作仍在)与
          // "这段时间没数据"肉眼难分,而 toast 3.5s 就散,用户回头再看已无从分辨,
          // 也没有重试入口。改挂常驻 banner(自带重试),与 App 层取数失败同一处显示。
          setFetchError(e instanceof Error ? e.message : String(e));
        }
        // append 失败要出声:静默失败的点击会被读成"没有更早的了"。
        if (mode === "append") toast(t("activity.loadMoreFailed"), "warn");
      })
      .finally(() => {
        if (gen !== fetchGenRef.current) return;
        // 只由**最新**那次取数收旗:被取代的那次在这里提前返回,旗子归最新那次管
        // (它在自己的 finally 里收),否则一次过期请求的落地就会让补漏重新挤进在途窗口。
        fetchInFlightRef.current = false;
        setLoading(false);
      });
  };

  // eventsRef 恒指向最新列表(见其声明处:refresh 判空洞不能拿闭包快照比)。
  useEffect(() => {
    eventsRef.current = events;
  }, [events]);

  // M5/N2: prop 变(homeId 切换 / 父组件 reload)时同步 — 仅当 filter 未激活。
  // 直接 setEvents(initial) 立即给出全量视图。注意:这会 clobber 快照→resolve 之间
  // SSE 刚推的事件;清 filter 那次过渡有 SSE 重订阅补偿,但已处于 !filterActive 时的
  // 同 home retry 不触发重订阅——此窗口极窄,但被 clobber 的那几条不会自己回来
  // (SSE 无重放,后续推送送的是**新**事件),要等下一次 replace / 重连补漏才可能覆盖(pre-existing)。
  // 先 ++fetchGenRef 作废在途 filtered fetch,并手动 setLoading(false) / 降在途旗
  // (被作废的 fetch 其 finally 的 gen 守卫会 no-op,既不会替我们收 loading,也不会替我们收旗)。
  // 这一代**没有对应的 fetch**,没人替它收尾 —— 漏了降旗,旗子就永久卡 true,
  // 之后每次 SSE 重连的补漏都被在途守卫静默跳过(而跳过路径不碰旗),本 PR 要修的
  // "事件悄悄不见"又回来了,且全量视图不翻页时没有任何自愈路径。
  useEffect(() => {
    if (!filterActive) {
      ++fetchGenRef.current; // 作废可能仍在途的 filtered fetch
      fetchInFlightRef.current = false; // 被作废那代不会替我们收旗(同上)
      setLoading(false);
      setEvents(initial);
      setOffset(initial.length);
      setHasMore(initial.length === PAGE_SIZE);
      // 清掉筛选视图留下的失败:这一屏的数据来自 App 层的 initial,与那条失败的请求无关。
      setFetchError(null);
    }
    // filterActive 时 ignore initial 变化 — 由 filter useEffect 主导
  }, [initial, homeId, filterActive]);

  // filter 变化时主动拉取(homeId 也走这里)
  useEffect(() => {
    if (!filterActive) return; // 未筛选时由 prop sync useEffect 接管
    fetchPage({ mode: "replace", pageOffset: 0 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appliedSince, appliedBefore, homeId]);

  // SSE:实时事件流.
  // M7 仍订阅 + 越界丢弃;M6 dedup → merge;S6 visibility 守 + onopen reload.
  // deps 用 applied* 而非裸 since/before(N3 防 input 抖动 churn EventSource)
  useEffect(() => {
    let unsub: (() => void) | null = null;

    const inRange = (ts: number): boolean => {
      if (appliedSince !== undefined && ts < appliedSince) return false;
      if (appliedBefore !== undefined && ts >= appliedBefore) return false;
      return true;
    };

    // 真·断线重连时拉一次,补回断开期间错过的事件(spec B13)。走 fetchPage 享 gen 保护;
    // mode=refresh 保证是 merge 而非替换,已翻的页不会被一次重连抹掉。
    //
    // **只有同一个 EventSource 实例自己断线重连才走到这里**,不是字面意义上的"首次 open
    // 也拉一次":realSubscribeEvents 吞掉每个实例的首次 open(firstOpenSeen,S3——避免与
    // 挂载时的首次拉取重复),而挂载 / 切筛选 / 切家都会新建实例,首次 open 一律被吞。
    // 切后台再切回来同样不触发:onVisibility 的 stop()/start() 是**新建** EventSource,
    // 它的首次 open 也被吞 —— 于是页面隐藏期间产生的事件既没有推送、回来也不回填
    // (缺口大小与 PAGE_SIZE 无关),只有筛选变化 / 切家触发的 replace 从头重列才可能覆盖到。
    //
    // 残留缺口(与上面那条不同):断线期间新增多于 PAGE_SIZE 条时,第 0 页与手里旧数据
    // 之间会空出一段 —— hasGapAbove 认出来并把 hasMore 抬为 true,不把有洞的列表标成
    // "已到底"。按 offset 语义,这段洞只有落在当前 offset **以下**的部分能靠「查看更早」
    // 补回来(append 从 offset 往下拉,offset 落在洞里的话接下来几轮就补上它);落在
    // offset **以上**的那段没有任何路径取得到(refresh 只拉第 0 页、SSE 只推重连之后的
    // 新事件、offset 只进不退、不再覆盖走过的区间),要等筛选变化 / 切家触发的 replace
    // 从头重列才可能覆盖到。
    // 补不回的那段在界面上也无从表达:列表照 ts 排序、两个片段直接相邻,看不出中间缺了东西。
    const reload = () => {
      fetchPage({ mode: "refresh", pageOffset: 0 });
    };

    const start = () => {
      if (unsub) return;
      unsub = subscribeEvents(
        (e) => {
          if (!inRange(e.timestamp)) return; // 越界事件丢弃
          // 新事件到达时顺带重拉动作——事件常伴随 agent 控制,让动作行跟上单流。
          // debounce 合并突发:一串事件只在末条后拉一次,而非每条都全量拉 500 行。
          debouncedReloadActions();
          setEvents((prev) => {
            const idx = prev.findIndex((x) => x.id === e.id);
            if (idx === -1) {
              // 新事件:走 mergeAndSort 保证 timestamp DESC 顺序稳定.
              // 不直接 [e, ...prev] — 若 backend 时钟回拨 / 同窗口多 device
              // 时间戳乱序,简单 prepend 会让较旧的事件挤到最上.
              return mergeAndSort(prev, [e]);
            }
            const merged: ActivityEvent = {
              ...prev[idx],
              snapshot_count: Math.max(prev[idx].snapshot_count, e.snapshot_count),
              device_ids: e.device_ids.length ? e.device_ids : prev[idx].device_ids,
              rule_names: e.rule_names ?? prev[idx].rule_names,
              // S2 防御:同 event_id 多次 SSE(未来若改"先推 metadata 后推 with clip"
              // 或 publish 重试)时,后到的 clip_kind 应胜出 — 漏掉的话会回归 18:42:05
              // bug(行尾错显 🎬 / 展开走 <video> 黑屏).
              clip_kind: e.clip_kind ?? prev[idx].clip_kind,
              has_trace: e.has_trace ?? prev[idx].has_trace,
              has_ref: e.has_ref ?? prev[idx].has_ref,
              has_feedback: e.has_feedback ?? prev[idx].has_feedback,
              feedback_pack_path: e.feedback_pack_path ?? prev[idx].feedback_pack_path,
              feedback_pack_size: e.feedback_pack_size ?? prev[idx].feedback_pack_size,
            };
            const next = prev.slice();
            next[idx] = merged;
            return next;
          });
        },
        reload, // onOpen
      );
    };

    const stop = () => {
      if (unsub) {
        unsub();
        unsub = null;
      }
    };

    const onVisibility = () => {
      if (document.visibilityState === "visible") start();
      else stop();
    };
    onVisibility();
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
      stop();
    };
    // debouncedReloadActions 是稳定 useCallback,列入不 churn EventSource。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appliedSince, appliedBefore, homeId, debouncedReloadActions]);

  /** 触发翻页:offset += PAGE_SIZE,append 模式。
   *  翻页同时把事件地平线往下推,合流里被地平线裁掉的那段动作随之显出来。 */
  const loadMore = () => {
    if (loading || !hasMore) return;
    fetchPage({ mode: "append", pageOffset: offset });
  };

  // 事件 + 动作合并成单条时间倒序流(见 mergeFeedRows 的窗口规则);带上当前应用的时间窗,
  // 让动作与事件同段约束(即使无事件也按 since/before 卡界)。
  const feedRows = useMemo(
    () =>
      mergeFeedRows(
        events, actions, showEvents, showActions, appliedSince, appliedBefore, hasMore,
      ),
    [events, actions, showEvents, showActions, appliedSince, appliedBefore, hasMore],
  );

  const noneChecked = !showEvents && !showActions;
  // "查看更早" 仅在展示事件时有意义(动作已一次拉全 500,无分页)。
  const showLoadMore = showEvents && hasMore && events.length > 0;

  /** banner 的失败态:**两条失败来源取同一条**(组件内带筛选的优先),文案与重试按钮都跟着
   *  它走。两处置顶顺序一旦相反(文字跟 App 层、按钮重发组件内的),双失败时点「重试」会像
   *  没反应 —— 第一次点只清掉 fetchError,文案纹丝不动,得再点一次。 */
  const bannerError = fetchError ?? eventsError?.message ?? null;
  /** 事件 banner 的重试入口,与 bannerError 同源:fetchError → 重发带筛选的那次请求
   *  (App.tsx 的 `listActivity(homeId)` 不带筛选,修不了筛选视图的失败);否则用 App 层
   *  传下来的。**按钮的门槛是 `bannerError &&`** —— 加载态 bannerError 为空,于是不会出现
   *  "正在加载"旁边挂一个「重试」(App 恒传 onRetryEvents,只判函数在不在等于没判)。 */
  const bannerRetry = fetchError
    ? () => fetchPage({ mode: "replace", pageOffset: 0 })
    : onRetryEvents;

  // 计数按流拆开。老代码用合并后的 feedRows.length 显"已加载 312 条+",那个 + 挂在
  // 谁身上完全看不出来——恰恰是被截断的事件流被描述成完整的。
  const shownEvents = useMemo(
    () => feedRows.reduce((n, r) => n + (r.kind === "event" ? 1 : 0), 0),
    [feedRows],
  );
  const shownActions = feedRows.length - shownEvents;

  // 动作为什么可能不全,有两个互相独立的原因,提示语不能混为一谈:
  //  - clippedByHorizon:已取回但被事件地平线压在下面,翻页就能显出来。
  //  - truncatedByLimit:500 上限确实卡住了——仅当最旧一条已取动作仍**高于**展示
  //    下界时才成立;地平线卡在 500 之上时上限并不生效,此时提示"仅显示最近 500 条"
  //    会把用户引到错误的原因上。
  const { actionsClipped, actionsTruncated } = useMemo(() => {
    if (!showActions) return { actionsClipped: false, actionsTruncated: false };
    const lower = feedLowerBound(events, showEvents, appliedSince, hasMore);
    const clipped = actions.some((a) => a.timestamp < lower);
    const truncated =
      actions.length >= ACTIONS_LIMIT &&
      Math.min(...actions.map((a) => a.timestamp)) > lower;
    return { actionsClipped: clipped || truncated, actionsTruncated: truncated };
  }, [actions, showActions, events, showEvents, appliedSince, hasMore]);

  // 两条流各自带自己的 "+",不再用合并总数糊弄过去。
  const loadedDetail = [
    showEvents && t("activity.countEvents", { n: shownEvents, more: showLoadMore ? "+" : "" }),
    showActions && t("activity.countActions", { n: shownActions, more: actionsClipped ? "+" : "" }),
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <section
      className="rounded-xl bg-bg-secondary border border-border shadow-sm anim-in"
      aria-labelledby="activity-title"
    >
      <div className="flex items-baseline justify-between gap-3 px-5 pt-4 pb-3 flex-wrap">
        <h2
          id="activity-title"
          className="text-title text-text-primary inline-flex items-baseline gap-2"
        >
          {t("activity.title")}
          <span className="text-caption-mono text-text-tertiary font-normal">
            {activeTab === "events"
              ? /* 两个 checkbox 都关时 loadedDetail 为空,整段不渲染——否则标题旁
                   会挂一个没有宾语的"已加载"。 */
                loadedDetail && t("activity.loaded", { detail: loadedDetail })
              : t("activity.odLoaded", { n: odCount, more: odHasMore ? "+" : "" })}
          </span>
        </h2>
        <div className={"inline-flex items-center gap-3 flex-wrap" + (activeTab === "events" ? "" : " invisible")}>
          {/* 事件 / 动作 checkbox — 都默认勾选,仅组件内 state 不持久化 */}
          <label className="inline-flex items-center gap-1.5 text-caption text-text-secondary cursor-pointer select-none">
            <input
              type="checkbox"
              checked={showEvents}
              onChange={(e) => setShowEvents(e.target.checked)}
              className="accent-brand-primary w-[13px] h-[13px]"
            />
            {t("actions.filterEvents")}
          </label>
          <label className="inline-flex items-center gap-1.5 text-caption text-text-secondary cursor-pointer select-none">
            <input
              type="checkbox"
              checked={showActions}
              onChange={(e) => setShowActions(e.target.checked)}
              className="accent-brand-primary w-[13px] h-[13px]"
            />
            {t("actions.filterActions")}
          </label>
          <TimeRangeFilter
            since={since}
            before={before}
            onSinceChange={setSince}
            onBeforeChange={setBefore}
            onReset={() => {
              // 恢复"今日实时"默认态:since=今天 00:00 + before=undefined → SSE inRange
              // 不拦截后续新事件,Feed 继续实时刷新.
              setSince(todayStartMs());
              setBefore(undefined);
            }}
          />
        </div>
      </div>

      {/* Sub-tabs */}
      <div className="flex gap-0 px-5 border-b border-border" role="tablist" aria-label={t("activity.title")}>
        <SubTab active={activeTab === "events"} onClick={() => setActiveTab("events")} label={t("activity.tabEvents")} id="tab-events" controls="panel-events" />
        <SubTab active={activeTab === "queries"} onClick={() => setActiveTab("queries")} label={t("activity.tabOnDemand")} id="tab-queries" controls="panel-queries" />
      </div>

      {/* Tab panels */}
      <div className="min-h-[240px]">

      {/* Events panel */}
      <div id="panel-events" role="tabpanel" aria-labelledby="tab-events" hidden={activeTab !== "events"}>

      {/* 事件加载中 / 失败:内联提示,不阻断下方合流(动作已独立加载)。
          三种出现原因——App 层不带筛选的请求失败(eventsError)、本组件带筛选的取数失败
          (fetchError)、App 层请求进行中(eventsLoading)。失败压过加载:后者原先只有一条
          3.5s 的 toast,失败后的画面(事件 0 条 + 动作照旧)与"这段时间没数据"肉眼难分,
          回头再看已无从分辨,也没有重试入口 —— 故统一走这条常驻 banner。
          文案与按钮都由 bannerError 决定(见其声明处),不含第二个判据。 */}
      {(bannerError || eventsLoading) && (
        <div className="mx-5 mb-2 px-3 py-2 rounded-lg bg-bg-primary border border-border text-caption text-text-secondary flex items-center justify-between gap-2">
          <span>
            {bannerError
              ? t("activity.eventsBannerFailed", { msg: bannerError })
              : t("activity.eventsBannerLoading")}
          </span>
          {bannerError && bannerRetry && (
            <button
              type="button"
              onClick={bannerRetry}
              className="shrink-0 px-2 py-0.5 rounded border border-border text-text-primary hover:border-border-strong"
            >
              {t("activity.retry")}
            </button>
          )}
        </div>
      )}

      {noneChecked ? (
        <div className="text-body text-center py-10 text-text-secondary">
          {t("actions.emptyFilter")}
        </div>
      ) : loading && showEvents && events.length === 0 && feedRows.length === 0 ? (
        <div className="text-body text-center py-10 text-text-secondary">
          {t("activity.loading")}
        </div>
      ) : feedRows.length === 0 ? (
        <div className="text-body text-center py-10 text-text-secondary">
          {filterActive
            ? t("activity.emptyFiltered")
            : t("activity.emptyDefault")}
        </div>
      ) : (
        <ul className="divide-y divide-border">
          {feedRows.map((r) =>
            r.kind === "event" ? (
              <ActivityRow
                key={`e:${r.event.id}`}
                event={r.event}
                onOpenLightbox={(src, kind, crop) => setLightbox({ src, kind, crop })}
                feedbackSet={feedbackSet}
                feedbackPacks={feedbackPacks}
                onFeedbackSubmitted={(id, path, size) => {
                  setFeedbackSet(prev => new Set(prev).add(id));
                  setFeedbackPacks(prev => new Map(prev).set(id, { path, size }));
                }}
              />
            ) : (
              <ActionRow key={`a:${r.action.id}`} row={r.action} t={t} />
            ),
          )}
          {/* 动作拉取达上限(500)**且该上限确实卡住了展示**时才提示 —— 见 actionsTruncated。 */}
          {actionsTruncated && (
            <li className="px-5 py-2 text-caption text-text-tertiary text-center">
              {t("actions.limitHint")}
            </li>
          )}
        </ul>
      )}

      {showLoadMore && (
        <div className="px-5 py-3 border-t border-border flex flex-col items-center gap-1">
          {/* 列表到这里为止是**地平线**,不是"没有了"。老 UI 只为动作流写了截断提示、
              事件流截断却一声不吭,用户读到的就是"事件消失了"。动作关掉时不能提动作
              —— 那是在解释一条用户亲手隐藏的流。 */}
          <span className="text-caption text-text-tertiary">
            {showActions ? t("activity.horizonHint") : t("activity.horizonHintEventsOnly")}
          </span>
          <button
            type="button"
            onClick={loadMore}
            disabled={loading}
            className="text-caption text-text-secondary hover:text-text-primary underline-offset-4 hover:underline transition-colors disabled:opacity-50"
          >
            {loading ? t("activity.loading") : t("activity.loadMore")}
          </button>
        </div>
      )}

      </div>{/* end events panel */}

      {/* On-demand queries panel */}
      <div id="panel-queries" role="tabpanel" aria-labelledby="tab-queries" hidden={activeTab !== "queries"}>
        <OnDemandLogList initial={initialOdLogs ?? EMPTY_OD_LOGS} initialLoading={onDemandLoading ?? false} initialError={onDemandError ?? null} onRetryInitial={onRetryOnDemand} homeId={homeId} deviceNames={deviceNames} onCountChange={handleOdCount} />
      </div>

      </div>{/* end tab panels */}

      {lightbox && (
        <Lightbox
          src={lightbox.src}
          kind={lightbox.kind}
          crop={lightbox.crop}
          onClose={() => setLightbox(null)}
        />
      )}
    </section>
  );
}

/** 全屏播放器 — 点 backdrop / Esc 关闭. mp4 走 <video controls>,audio-only m4a 同样
 *  用 <video>(浏览器对纯音频 mp4/m4a render 黑底 + 音轨).
 *
 *  kind="image":Smart Crop 的全景参考帧 ref.jpg,走 <img> —— <video src=*.jpg>
 *  渲染不出来,所以按 kind 分叉而不是靠嗅探 URL.
 *
 *  M1: 不加 autoPlay — Chrome/Safari autoplay policy 会拦截带音轨自动播放(modal
 *      是 fresh element,不继承父点击的 user gesture);改让用户主动按 ▶,体验稳定.
 *  S2: 挂载时 pause 页面里所有其他 <video>,避免 inline ClipPlayer 跟 Lightbox 同时
 *      出声(用 querySelectorAll 一次性处理,避免 prop drill).
 *  S6: keydown 通过 useRef(onClose) 解耦,空 deps,避免父组件每次 render 都重绑. */
function Lightbox({
  src,
  kind = "video",
  crop,
  onClose,
}: {
  src: string;
  kind?: LightboxKind;
  /** 参考帧的 crop 框(仅 kind==="image").放大看正是为了核对裁切位置,这里不能丢框. */
  crop?: EventCropMeta | null;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const videoRef = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCloseRef.current();
    };
    document.addEventListener("keydown", onKey);
    // S2: pause 所有别的 <video>,只留下当前 Lightbox 这个继续播
    const others = Array.from(document.querySelectorAll("video")).filter(
      (v) => v !== videoRef.current,
    );
    others.forEach((v) => v.pause());
    return () => document.removeEventListener("keydown", onKey);
  }, []);
  return (
    <div
      onClick={onClose}
      className="fixed inset-0 z-50 bg-black/80 flex items-center justify-center p-4 cursor-zoom-out anim-in"
      role="dialog"
      // 必须跟着 kind 走:label 写在 kind 分叉之前,读屏会把参考帧对话框念成"事件回放"
      // ——与屏上内容不符,而读屏用户没有画面可以纠正这句描述。
      aria-label={kind === "image" ? t("activity.refFrame") : t("activity.playback")}
    >
      <button
        type="button"
        onClick={onClose}
        aria-label={t("activity.close")}
        className="absolute top-4 right-4 w-10 h-10 rounded-full bg-white/10 hover:bg-white/20 text-white text-xl flex items-center justify-center"
      >
        ✕
      </button>
      {kind === "image" ? (
        // 与 RefFrameCard 缩略卡同款:容器给**确定**尺寸,<img> 在其中 object-contain,
        // svg 覆盖层用同一个盒子 + preserveAspectRatio 做同样的 letterbox → 必然对齐.
        //
        // 不能让容器 shrink-to-fit(如 `flex max-w-full max-h-full`):那样容器高度是 auto,
        // <img> 的 `max-h-full` 百分比无从解析 → 高度不受约束,图溢出被切;同时容器按
        // max-content(帧原始宽)定宽,比图实际渲染宽,覆盖层跟着变宽,框整体横向错位.
        // 16:9 桌面最大化(可用高 < 可用宽 × 9/16)必然命中,而放大就是为了核对裁切位置.
        //
        // 代价:rounded/shadow 落在元素盒(= 整个可用区)而非可见图上,已去掉;
        // 也不再 stopPropagation —— 容器铺满后拦掉就等于废掉背景点击关闭,
        // 而静态图没有 <video> 那种需要保护的控件,点任意处关闭正合 cursor-zoom-out.
        <div className="relative w-full h-full">
          <img
            src={src}
            alt={t("activity.refFrame")}
            className="w-full h-full object-contain"
          />
          {crop && <CropBoxOverlay crop={crop} />}
        </div>
      ) : (
        <video
          ref={videoRef}
          src={src}
          controls
          className="max-w-full max-h-full rounded shadow-lg cursor-default bg-black"
          onClick={(e) => e.stopPropagation()}
        />
      )}
    </div>
  );
}

function TimeRangeFilter({
  since,
  before,
  onSinceChange,
  onBeforeChange,
  onReset,
}: {
  since: number | undefined;
  before: number | undefined;
  onSinceChange: (v: number | undefined) => void;
  onBeforeChange: (v: number | undefined) => void;
  /** 恢复"今日实时"默认态(since=今天 00:00 + before=undefined). */
  onReset: () => void;
}) {
  const { t } = useTranslation();
  // before 默认未设保持 undefined → SSE inRange 不冻结实时流;UI 显"至现在" button.
  // 用户点 "至现在" → commit before=Date.now() 立刻冻结实时流并显当前时刻为 input 值,
  // 让用户在此基础上微调.脱出实时态由右侧 "↻ 实时" 按钮明确恢复(不再用空 input 含蓄保住).
  const [beforeEditing, setBeforeEditing] = useState(false);
  const showInput = before !== undefined || beforeEditing;
  // "↻ 实时"按钮只在用户实际偏离默认态时显示 — 默认态下显示等于视觉噪声.
  // 偏离 = since ≠ 今天 00:00 (含 undefined) OR before 已设 OR before 正在编辑.
  const todayStart = useMemo(() => {
    const d = new Date();
    return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  }, []);
  const isAtDefault =
    since === todayStart && before === undefined && !beforeEditing;
  const handleReset = () => {
    setBeforeEditing(false);
    onReset();
  };
  // datetime-local input 值是 "YYYY-MM-DDTHH:mm";空值守.
  // B6:用户清空输入后 e.target.value="" → new Date("") = Invalid → NaN.
  // 把 NaN 当成"清除筛选",而不是让 API 收到 timestamp=NaN 422 报错.
  const fmt = (ms: number | undefined): string => {
    if (ms === undefined) return "";
    const d = new Date(ms);
    const pad = (n: number) => (n < 10 ? `0${n}` : `${n}`);
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  const parse = (s: string): number | undefined => {
    if (!s) return undefined;
    const ms = new Date(s).getTime();
    return Number.isNaN(ms) ? undefined : ms;
  };
  const inputCls =
    "bg-bg-primary border border-border rounded-md px-2 py-1 text-caption-mono text-text-primary " +
    "hover:border-border-strong focus:outline-none focus:border-brand-primary " +
    "transition-colors cursor-pointer [color-scheme:light] dark:[color-scheme:dark]";
  return (
    <div className="inline-flex items-center gap-1.5">
      <input
        type="datetime-local"
        value={fmt(since)}
        onChange={(e) => onSinceChange(parse(e.target.value))}
        onClick={(e) => e.currentTarget.showPicker?.()}
        className={inputCls}
        aria-label={t("activity.filterSince")}
        placeholder={t("activity.filterSincePlaceholder")}
      />
      <span className="text-caption-mono text-text-tertiary">→</span>
      {!showInput ? (
        // 默认 before=undefined → 实时态,显"至现在" button.点击 commit Date.now()
        // 作为初始值(避免 input 显 dd/mm/yyyy 占位空白让用户两步操作),input 用此
        // 时刻为锚点供微调.脱出实时态由 "↻ 实时" 按钮负责.
        <button
          type="button"
          onClick={() => {
            onBeforeChange(Date.now());
            setBeforeEditing(true);
          }}
          className={inputCls + " text-text-tertiary"}
          aria-label={t("activity.filterBeforeDefault")}
        >
          {t("activity.filterToNow")}
        </button>
      ) : (
        <input
          type="datetime-local"
          value={fmt(before)}
          onChange={(e) => {
            const v = parse(e.target.value);
            onBeforeChange(v);
            if (v === undefined) setBeforeEditing(false); // 清空后回到"至现在"按钮
          }}
          onClick={(e) => e.currentTarget.showPicker?.()}
          onBlur={(e) => {
            // 失焦时如果还是空值,退回按钮态(避免空 input 留在 UI 上)
            if (!e.target.value) setBeforeEditing(false);
          }}
          className={inputCls}
          aria-label={t("activity.filterBefore")}
          autoFocus
        />
      )}
      {!isAtDefault && (
        <button
          type="button"
          onClick={handleReset}
          className={
            inputCls +
            " text-text-secondary hover:text-text-primary"
          }
          aria-label={t("activity.resumeLive")}
        >
          {t("activity.liveButton")}
        </button>
      )}
    </div>
  );
}

/**
 * 折叠态的「触发状态」badge。
 *
 * 只在折叠态出现：那里每段裁 2 行且无 `pre-wrap`，而「触发状态」排在长度无上界的
 * 任务 / 规则之后，常被裁掉 —— badge 定长、排最前，稳定可见。展开态仍读原文本行。
 *
 * 配色语义：**颜色承载「有没有触发」**（绿=已触发 / 灰=未触发 / 琥珀=判不出），
 * 文字只说「为什么」。所以三种「未触发」共用灰色，短文案（持续中 / 计时中）单独看
 * 略有歧义也不至于误解成「已触发」；精确全称在 title 悬停与展开态里。
 */
function TriggerStatusBadge({ kind }: { kind: TriggerStatusKind }) {
  const { t } = useTranslation();
  const tone =
    kind === "fired"
      ? "text-success bg-success-bg"
      : kind === "unknown"
        ? "text-warning bg-warning-bg"
        : "text-text-tertiary bg-bg-tertiary";
  const key = kind.charAt(0).toUpperCase() + kind.slice(1);
  return (
    <span
      className={`inline-flex items-center gap-1 mr-1.5 px-2 rounded-full text-caption align-[2px] ${tone}`}
      title={t(`activity.trigger${key}Hint`)}
    >
      <span className="w-1.5 h-1.5 rounded-full bg-current shrink-0" aria-hidden="true" />
      {t(`activity.trigger${key}`)}
    </span>
  );
}

function ActivityRow({
  event,
  onOpenLightbox,
  feedbackSet,
  feedbackPacks,
  onFeedbackSubmitted,
}: {
  event: ActivityEvent;
  onOpenLightbox: (src: string, kind: LightboxKind, crop?: EventCropMeta | null) => void;
  feedbackSet: Set<string>;
  feedbackPacks: Map<string, { path: string; size: number }>;
  onFeedbackSubmitted: (eventId: string, path: string, size: number) => void;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const hasClips = event.snapshot_count > 0;
  // 区分音频事件 vs 视频事件 — backend stat 落盘文件后缀计算 clip_kind:
  //   "mp4" → 视频路径 (H264+AAC),UI 🎬
  //   "m4a" → audio-only 路径(纯 AAC,画面静止),UI 🎤 音频
  //   null/undefined → 未落盘(磁盘满预检失败 / 老库 event),UI 🎤
  const isAudioOnly = event.clip_kind === "m4a";

  // humanize 后按 \n\n 分章节渲染.每章节自成一段(line-clamp-2 折叠模式).
  const humanized = useMemo(
    () => humanizeRulesInText(event.text, event.rule_names),
    [event.text, event.rule_names],
  );
  // 折叠态:抽出每段的「触发状态」渲染成 badge(见 splitHumanizedSections 注释——折叠态
  // 无 pre-wrap + 裁 2 行,状态行排在长度无上界的任务/规则之后常被裁掉).展开态不用它、
  // 仍渲染完整 humanized 原文(状态行照旧在「规则」之后).
  const sections = useMemo(() => splitHumanizedSections(humanized), [humanized]);

  // 行尾标识:
  //   - 视频 clip → 🎬
  //   - 音频 clip(画面静止 audio-only)→ 🎤
  //   - 无 clip(metadata-only / 老库)→ 🎤
  // 展开状态显"收起".audio-only 跟"无 clip"用同一图标 — 都"没视频"语义一致.
  const trailing = expanded
    ? t("activity.collapse")
    : hasClips && !isAudioOnly
      ? "🎬"
      : "🎤";

  return (
    <li
      onClick={() => { if (!window.getSelection()?.toString()) setExpanded((x) => !x); }}
      aria-expanded={expanded}
      className="px-5 py-2.5 hover:bg-bg-tertiary transition-colors cursor-pointer"
    >
      <div className="flex flex-col gap-1 sm:grid sm:grid-cols-[70px_1fr_auto] sm:gap-x-3 sm:gap-y-1 sm:items-baseline">
        <TimeLabel timestamp={event.timestamp} />
        <div className="min-w-0 sm:order-2">
          {expanded ? (
            <pre className="text-body text-text-primary whitespace-pre-wrap break-words font-sans">
              {humanized}
            </pre>
          ) : (
            sections.map((s, i) => (
              <span
                key={i}
                className="text-body text-text-primary block break-words"
                style={{
                  display: "-webkit-box",
                  WebkitBoxOrient: "vertical",
                  WebkitLineClamp: 2,
                  overflow: "hidden",
                }}
              >
                {s.status && <TriggerStatusBadge kind={s.status} />}
                {s.text}
              </span>
            ))
          )}
        </div>
        <span
          className="text-caption-mono text-text-tertiary whitespace-nowrap sm:order-last sm:justify-self-end"
          aria-hidden="true"
        >
          {trailing}
        </span>
      </div>

      {expanded && hasClips && !isAudioOnly && (
        <div
          className="mt-3 flex gap-2 overflow-x-auto pb-2 sm:ml-[82px]"
          aria-label={t("activity.videoPlayback")}
        >
          {event.device_ids.map((did) => (
            // Smart Crop 事件:clip 是裁切放大的局部视频,紧跟一张全景参考帧 ——
            // 并排放才能一眼看出"模型盯的是全景里哪块"(参考帧上还画了 crop 框).
            // 多摄像头时按 device 成对铺开,不把参考帧全挤到末尾.
            <Fragment key={did}>
              <ClipPlayer
                event_id={event.id}
                device_id={did}
                onOpenLightbox={onOpenLightbox}
              />
              {event.has_ref && (
                <RefFrameCard
                  event_id={event.id}
                  device_id={did}
                  onOpenLightbox={onOpenLightbox}
                />
              )}
            </Fragment>
          ))}
        </div>
      )}

      {expanded && hasClips && isAudioOnly && (
        <div
          className="mt-3 flex gap-2 overflow-x-auto pb-2 sm:ml-[82px]"
          aria-label={t("activity.audioPlayback")}
        >
          {event.device_ids.map((did) => (
            <AudioClipPlayer
              key={did}
              event_id={event.id}
              device_id={did}
            />
          ))}
        </div>
      )}

      {expanded && !hasClips && (
        <div
          className="mt-3 px-4 py-6 rounded bg-bg-primary border border-border text-caption-mono text-text-tertiary text-center sm:ml-[82px]"
          aria-label={t("activity.noPlaybackAria")}
        >
          {t("activity.noPlayback")}
        </div>
      )}

      {event.has_trace && (
        <div className={expanded ? "" : "hidden"}>
          <FeedbackSection
            eventId={event.id}
            hasFeedback={event.has_feedback || feedbackSet.has(event.id)}
            packPath={feedbackPacks.get(event.id)?.path ?? event.feedback_pack_path}
            onSubmitted={onFeedbackSubmitted}
          />
        </div>
      )}
    </li>
  );
}

const ERROR_TYPE_KEYS = [
  "person",
  "pet",
  "action",
  "envDevice",
  "voice",
  "ruleFalse",
  "other",
] as const;

// 主动查询不经过规则匹配/建议生成,排除 ruleFalse;其余类别与事件侧同源。
const OD_ERROR_TYPE_KEYS = ERROR_TYPE_KEYS.filter((k) => k !== "ruleFalse");

function FeedbackSection({ eventId, hasFeedback, packPath, onSubmitted }: {
  eventId: string;
  hasFeedback?: boolean;
  packPath?: string | null;
  onSubmitted: (eventId: string, path: string, size: number) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [confirmedResubmit, setConfirmedResubmit] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [text, setText] = useState("");
  const [includeGallery, setIncludeGallery] = useState(false);
  const [status, setStatus] = useState<"idle" | "submitting" | "error">("idle");

  const handleToggle = (type: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  };

  const handleSubmit = async () => {
    setStatus("submitting");
    try {
      const result = await submitEventFeedback(eventId, [...selected], text, includeGallery);
      onSubmitted(eventId, result.pack_path, result.pack_size_bytes);
      setConfirmedResubmit(false);
      setStatus("idle");
      setOpen(false);
      setSelected(new Set());
      setText("");
      setIncludeGallery(false);
    } catch {
      setStatus("error");
    }
  };

  // idle: 反馈按钮 or 已反馈（显示路径）
  if (!open && status === "idle") {
    if (hasFeedback && !confirmedResubmit) {
      return (
        <div className="mt-2.5 sm:ml-[82px] px-3.5 py-2.5 rounded-lg bg-info-bg text-caption" onClick={(e) => e.stopPropagation()}>
          <div className="flex items-baseline justify-between">
            <span className="text-info">
              ✓ {t("activity.feedbackSaved", "反馈已记录，数据已保存到本地")}
              {packPath && <>，<button type="button" onClick={() => { const i = packPath.lastIndexOf("/"); if (i > 0) revealDir(packPath.substring(0, i)).catch(() => {}); }} className="text-info underline hover:opacity-80">{t("activity.feedbackReveal", "点击打开所在文件夹")}</button></>}
              ，<a href={FEEDBACK_FORM_URL} target="_blank" rel="noopener noreferrer" className="text-info underline hover:opacity-80">{t("activity.feedbackSubmitLink", "前往提交")}</a>
            </span>
            <button type="button" onClick={() => { setConfirmedResubmit(true); setOpen(true); }} className="flex-shrink-0 ml-3 text-[11px] text-text-tertiary hover:text-brand-primary transition-colors">
              {t("activity.feedbackModify", "修改反馈信息")}
            </button>
          </div>
          {packPath && (
            <div className="mt-1.5 text-[10px] text-text-tertiary font-mono truncate" title={packPath}>{packPath}</div>
          )}
        </div>
      );
    }

    return (
      <div className="mt-2 sm:ml-[82px]">
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); setOpen(true); }}
          className="inline-flex items-center gap-1 px-3 py-[5px] text-caption text-text-tertiary bg-transparent border border-border rounded-md hover:text-brand-primary hover:border-brand-primary hover:bg-brand-soft transition-colors"
        >
          <span className="text-[11px]">⚑</span> {t("activity.feedbackButton", "反馈")}
        </button>
      </div>
    );
  }

  // submitting
  if (status === "submitting") {
    return (
      <div className="mt-2.5 sm:ml-[82px] flex items-center gap-2 px-3.5 py-2.5 rounded-lg bg-bg-tertiary text-caption text-text-secondary" onClick={(e) => e.stopPropagation()}>
        <span className="inline-block w-3 h-3 border-2 border-border border-t-brand-primary rounded-full animate-spin" />
        {t("activity.feedbackSubmitting", "正在打包感知数据...")}
      </div>
    );
  }

  // error
  if (status === "error") {
    return (
      <div className="mt-2.5 sm:ml-[82px]" onClick={(e) => e.stopPropagation()}>
        <div className="px-3.5 py-2.5 rounded-lg text-caption" style={{ background: "rgba(220,38,38,.06)", color: "#DC2626" }}>
          ✗ {t("activity.feedbackError", "提交失败，请重试")}
        </div>
        <button type="button" onClick={() => setStatus("idle")} className="mt-1.5 text-caption text-text-tertiary hover:text-text-secondary">
          {t("activity.feedbackRetry", "重试")}
        </button>
      </div>
    );
  }

  // open: 反馈面板
  return (
    <div className="mt-2.5 sm:ml-[82px] p-3.5 rounded-[10px] bg-bg-primary border border-border" onClick={(e) => e.stopPropagation()}>
      <div className="text-[13px] font-semibold text-text-primary mb-2.5">
        {t("activity.feedbackTitle", "反馈感知问题")}
      </div>

      <div className="text-caption text-text-tertiary mb-1">{t("activity.feedbackErrorType")}</div>
      <div className="flex flex-wrap gap-1.5 mb-3">
        {ERROR_TYPE_KEYS.map((k) => (
          <button
            key={k}
            type="button"
            onClick={() => handleToggle(k)}
            className={`px-2.5 py-1 text-caption rounded-full border transition-colors ${
              selected.has(k)
                ? "text-brand-primary bg-brand-soft border-brand-primary"
                : "text-text-secondary bg-bg-secondary border-border hover:border-border-strong"
            }`}
          >
            {t(`activity.errorType.${k}`)}
          </button>
        ))}
      </div>

      <div className="text-caption text-text-tertiary mb-1">{t("activity.feedbackText", "补充说明（可选）")}</div>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={t("activity.feedbackPlaceholder", "如：识别错了，是爸爸不是爷爷")}
        className="w-full h-[52px] px-2.5 py-2 border border-border rounded-lg text-[13px] bg-bg-secondary text-text-primary resize-none focus:outline-none focus:border-brand-primary transition-colors"
      />

      <label className="flex items-center gap-1.5 mt-2 text-caption text-text-tertiary cursor-pointer">
        <input type="checkbox" checked={includeGallery} onChange={(e) => setIncludeGallery(e.target.checked)} className="accent-brand-primary w-[13px] h-[13px]" />
        {t("activity.feedbackGallery", "包含人员画廊（可能含人脸数据，默认不上传）")}
      </label>

      <div className="mt-2 px-2.5 py-1.5 rounded-md text-[11px] text-text-tertiary" style={{ background: "rgba(217,119,6,.06)" }}>
        ⚠ {t("activity.feedbackPrivacy", "提交反馈将收集相关音视频和感知数据并保存到本地")}
      </div>

      <div className="flex justify-end items-center gap-1.5 mt-2.5">
        <button
          type="button"
          onClick={() => { setOpen(false); setConfirmedResubmit(false); setSelected(new Set()); setText(""); setIncludeGallery(false); }}
          className="px-3.5 py-[5px] text-caption text-text-secondary rounded-md hover:bg-bg-tertiary transition-colors"
        >
          {t("activity.feedbackCancel", "取消")}
        </button>
        <button type="button" onClick={handleSubmit} className="px-4 py-[5px] text-caption font-medium text-white bg-brand-primary rounded-md hover:bg-brand-accent transition-colors">
          {t("activity.feedbackSubmit", "打包数据")}
        </button>
      </div>
    </div>
  );
}

/** 单 device clip 播放器:行内小尺寸 <video controls>,点击放大走 Lightbox.
 *  字节级 = omni 上传给 LLM 的 mp4(零重编).视频路径含 H264+AAC;audio-only
 *  路径仅 AAC,<video> 标签自动 render audio-only track(黑底 + 进度条). */
function ClipPlayer({
  event_id,
  device_id,
  onOpenLightbox,
}: {
  event_id: string;
  device_id: string;
  onOpenLightbox: (src: string, kind: LightboxKind, crop?: EventCropMeta | null) => void;
}) {
  const { t } = useTranslation();
  const [failed, setFailed] = useState(false);
  const src = eventClipUrl(event_id, device_id);
  if (failed) {
    return (
      <div
        // S1:阻断冒泡 — 跟正常态 <video> 对称.否则点了 "🎬 已过期" 占位会冒泡到
        // 父 <li onClick>,把整行 Accordion 收起,跟用户预期相反.
        onClick={(e) => e.stopPropagation()}
        className="flex-shrink-0 w-48 h-48 rounded bg-bg-primary border border-border flex items-center justify-center text-caption-mono text-text-tertiary"
        aria-label={t("activity.clipExpiredAria")}
      >
        {t("activity.clipExpired")}
      </div>
    );
  }
  return (
    <div
      // 外层 wrapper 同样阻断冒泡,处理 <video> + ⛶ 按钮之外的角落点击(group-hover
      // 间隙、padding 区域).
      onClick={(e) => e.stopPropagation()}
      className="flex-shrink-0 relative group"
    >
      <video
        src={src}
        controls
        preload="metadata"
        onError={() => setFailed(true)}
        onClick={(e) => e.stopPropagation()}
        className="w-48 h-48 rounded bg-black border border-border object-contain"
        aria-label={`${device_id} clip`}
      />
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onOpenLightbox(src, "video");
        }}
        aria-label={t("activity.zoomPlay")}
        className="absolute top-1 right-1 w-7 h-7 rounded-full bg-black/60 hover:bg-black/80 text-white text-xs flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
      >
        ⛶
      </button>
    </div>
  );
}

/** Smart Crop 全景参考帧卡:与 crop clip 并排的静态 <img>,叠一层 crop 框.
 *
 *  为什么要这张卡:crop 模式下送 LLM 的视频只是画面里的一小块,单看它无从判断"模型
 *  是不是盯错了地方".参考帧就是同一次推理里一并上送的整帧上下文(字节级 = omni 所见),
 *  加上框就能直接看出裁切位置对不对 —— badcase 复盘的主要抓手.
 *
 *  crop 框画法:绝对定位一层 `<svg viewBox="0 0 W H" preserveAspectRatio="xMidYMid meet">`
 *  盖在 object-contain 的 <img> 上.W/H = 全景帧原始尺寸,region 坐标也在这个空间,
 *  所以 letterbox 缩放交给浏览器做,前端一行坐标换算都不需要,窗口 resize 也自动跟随.
 *
 *  三种"拿不到"要分开处理,否则会给用户看假象:
 *  - crop 坐标 = null(后端 410:这台 device 本次没裁切 —— 非 crop 事件 / 落到全景兜底 /
 *    事件目录已被 cleanup 清)→ **整张卡不渲染**.因为列表里的 `has_ref` 是**事件级
 *    any-device**(后端 probe 只要任一 device 目录有 ref.jpg 就置 true),而本卡是**按
 *    device 渲染**的:多摄像头事件里完全可能 A 机裁了、B 机回退全景,此时 B 机的卡若照渲
 *    就会显示一个假的"参考帧已过期".这里用 crop 坐标的有无重新按 device 门控一次.
 *    (不改成 per-device has_ref 是因为要连带动 SSE 写侧 / list API / 类型 / 测试.)
 *    这一档现在都以「盘上没有这台 device 的 ref.jpg」为前提 —— 后端返 410 前会 stat 一次
 *    (events_service._no_box_status),所以隐藏整卡不会误伤;裁过但坐标读不出来的走下一档.
 *  - crop 请求失败(网络抖动 / 5xx,含后端"裁过但 trace 读坏"的 500)→ 保留卡,
 *    只是不画框.区别于上面:那是"确定没有",这是"没问出来" —— ref.jpg 还在盘上,
 *    不该把用户本来能看的参考帧藏掉.
 *  - ref.jpg 本身 404/410(cleanup 清掉)→ 显"已过期"占位,与 ClipPlayer.failed 对称. */
function RefFrameCard({
  event_id,
  device_id,
  onOpenLightbox,
}: {
  event_id: string;
  device_id: string;
  onOpenLightbox: (src: string, kind: LightboxKind, crop?: EventCropMeta | null) => void;
}) {
  const { t } = useTranslation();
  const [failed, setFailed] = useState(false);
  const [crop, setCrop] = useState<EventCropMeta | null>(null);
  /** 后端明确答"这台 device 没裁切"(410)→ 整卡不渲染,见组件 docstring. */
  const [absent, setAbsent] = useState(false);
  const src = eventRefUrl(event_id, device_id);

  // 只在本卡挂载时拉一次 crop 坐标(父层已按 expanded + has_ref 门控,折叠的行不会请求).
  useEffect(() => {
    let alive = true;
    setCrop(null);
    setAbsent(false);
    eventCropMeta(event_id, device_id)
      .then((m) => {
        if (!alive) return;
        setCrop(m);
        setAbsent(m === null);
      })
      .catch(() => {
        // 网络 / 5xx:没问出来 ≠ 没有 → 保留卡,只是不画框
      });
    return () => {
      alive = false;
    };
  }, [event_id, device_id]);

  if (absent) return null;
  if (failed) {
    return (
      <div
        onClick={(e) => e.stopPropagation()}
        className="flex-shrink-0 w-48 h-48 rounded bg-bg-primary border border-border flex items-center justify-center text-caption-mono text-text-tertiary"
        aria-label={t("activity.refExpiredAria")}
      >
        {t("activity.refExpired")}
      </div>
    );
  }
  return (
    <div
      onClick={(e) => e.stopPropagation()}
      className="flex-shrink-0 relative group"
    >
      <img
        src={src}
        alt={`${device_id} ${t("activity.refFrame")}`}
        onError={() => setFailed(true)}
        onClick={(e) => e.stopPropagation()}
        className="w-48 h-48 rounded bg-black border border-border object-contain"
      />
      {crop && <CropBoxOverlay crop={crop} />}
      <span className="absolute bottom-1 left-1 px-1.5 py-0.5 rounded bg-black/60 text-white text-caption-mono pointer-events-none">
        {t("activity.refFrame")}
      </span>
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onOpenLightbox(src, "image", crop);
        }}
        aria-label={t("activity.zoomRefFrame")}
        className="absolute top-1 right-1 w-7 h-7 rounded-full bg-black/60 hover:bg-black/80 text-white text-xs flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
      >
        ⛶
      </button>
    </div>
  );
}

/** 把 crop 元数据换算成 svg 几何(viewBox + rect).坏数据返 null = 不画框.
 *
 *  坐标不做任何缩放 —— viewBox 用全景帧原始尺寸,rect 用原始 region 像素坐标,
 *  letterbox 缩放交给浏览器(见 RefFrameCard 注释).
 *  stroke 宽度按帧宽比例给(不是固定 px):viewBox 单位会随缩放一起变,固定 2 在
 *  1920 宽的帧上细到看不见.
 *
 *  导出仅为单测(同 mergeAndSort);渲染入口是 CropBoxOverlay. */
export function cropBoxGeometry(crop: EventCropMeta): {
  viewBox: string;
  x: number;
  y: number;
  width: number;
  height: number;
  strokeWidth: number;
} | null {
  const [w, h] = crop.frame_size_wh ?? [];
  const [x1, y1, x2, y2] = crop.region_xyxy ?? [];
  // 后端理论上不会给出这些形状,但 trace 是历史产物(schema 演进 / 手工改过 / 截断),
  // 宁可不画框也不要吐一个 NaN viewBox 让整张 svg 变成花屏.
  if (![w, h, x1, y1, x2, y2].every((n) => Number.isFinite(n))) return null;
  if (!(w > 0 && h > 0 && x2 > x1 && y2 > y1)) return null;
  return {
    viewBox: `0 0 ${w} ${h}`,
    x: x1,
    y: y1,
    width: x2 - x1,
    height: y2 - y1,
    strokeWidth: Math.max(2, Math.round(w / 160)),
  };
}

/** crop 框:覆盖在参考帧上的 svg.stroke="currentColor" 让描边跟随 text-brand-primary,
 *  主题切换时不用另写一套色值. */
function CropBoxOverlay({ crop }: { crop: EventCropMeta }) {
  const geo = cropBoxGeometry(crop);
  if (!geo) return null;
  return (
    <svg
      viewBox={geo.viewBox}
      preserveAspectRatio="xMidYMid meet"
      className="absolute inset-0 w-full h-full pointer-events-none text-brand-primary"
      aria-hidden="true"
    >
      <rect
        x={geo.x}
        y={geo.y}
        width={geo.width}
        height={geo.height}
        fill="none"
        stroke="currentColor"
        strokeWidth={geo.strokeWidth}
      />
    </svg>
  );
}

/** audio-only 事件的紧凑播放器:仅 <audio controls>,无大黑框.
 *  audio-only 路径 omni 落 clip.m4a(纯 AAC,无视频流);用 <audio> 而不是 <video>
 *  能避免"黑屏看像坏掉了"的误导(18:42:05 这条记录就是因为前端用 <video> 显黑屏
 *  让用户以为是视频,实际只是音频). */
function AudioClipPlayer({
  event_id,
  device_id,
}: {
  event_id: string;
  device_id: string;
}) {
  const { t } = useTranslation();
  const [failed, setFailed] = useState(false);
  const src = eventClipUrl(event_id, device_id);
  if (failed) {
    return (
      <div
        onClick={(e) => e.stopPropagation()}
        className="flex-shrink-0 w-full px-4 py-3 rounded bg-bg-primary border border-border flex items-center gap-2 text-caption-mono text-text-tertiary"
        aria-label={t("activity.audioExpiredAria")}
      >
        {t("activity.audioExpired")}
      </div>
    );
  }
  return (
    <div
      onClick={(e) => e.stopPropagation()}
      className="flex-shrink-0 w-full px-4 py-3 rounded bg-bg-primary border border-border flex items-center gap-3"
    >
      <span className="text-caption-mono text-text-secondary whitespace-nowrap">
        {t("activity.audioOnly")}
      </span>
      <audio
        src={src}
        controls
        preload="metadata"
        onError={() => setFailed(true)}
        onClick={(e) => e.stopPropagation()}
        className="flex-1 min-w-0 h-9"
        aria-label={`${device_id} audio clip`}
      />
    </div>
  );
}

/* ── Sub-tab button ── */

function SubTab({ active, onClick, label, id, controls }: {
  active: boolean;
  onClick: () => void;
  label: string;
  id: string;
  controls: string;
}) {
  return (
    <button
      type="button"
      role="tab"
      id={id}
      aria-selected={active}
      aria-controls={controls}
      onClick={onClick}
      className={
        "px-4 py-2 text-body font-medium border-b-2 -mb-px transition-colors " +
        (active
          ? "text-brand-primary border-brand-primary font-semibold"
          : "text-text-tertiary border-transparent hover:text-text-secondary")
      }
    >
      {label}
    </button>
  );
}

/* ── On-demand log list ── */

function OnDemandLogList({ initial, initialLoading, initialError, onRetryInitial, homeId, deviceNames, onCountChange }: {
  initial: OnDemandLogEntry[];
  initialLoading: boolean;
  initialError: Error | null;
  onRetryInitial: () => void;
  homeId: HomeId;
  deviceNames: Record<string, string>;
  onCountChange: (n: number, hasMore: boolean) => void;
}) {
  const { t } = useTranslation();
  const [logs, setLogs] = useState<OnDemandLogEntry[]>(initial);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [hasMore, setHasMore] = useState(initial.length === OD_PAGE_SIZE);
  const [lightboxSrc, setLightboxSrc] = useState<string | null>(null);

  useEffect(() => {
    setLogs(initial);
    setHasMore(initial.length === OD_PAGE_SIZE);
    onCountChange(initial.length, initial.length === OD_PAGE_SIZE);
  }, [initial, homeId, onCountChange]);

  const refresh = () => {
    if (refreshing) return;
    setRefreshing(true);
    listOnDemandLogs(homeId, { limit: OD_PAGE_SIZE })
      .then((fresh) => {
        setLogs(fresh);
        setHasMore(fresh.length === OD_PAGE_SIZE);
        onCountChange(fresh.length, fresh.length === OD_PAGE_SIZE);
      })
      .catch(() => {
        toast(t("activity.odLoadFailed", { msg: "" }), "warn");
      })
      .finally(() => setRefreshing(false));
  };

  const loadMore = () => {
    if (loading || !hasMore || logs.length === 0) return;
    const oldest = logs[logs.length - 1];
    setLoading(true);
    listOnDemandLogs(homeId, { before: oldest.timestamp, before_id: oldest.id, limit: OD_PAGE_SIZE })
      .then((older) => {
        const next = [...logs, ...older];
        setLogs(next);
        setHasMore(older.length === OD_PAGE_SIZE);
        onCountChange(next.length, older.length === OD_PAGE_SIZE);
      })
      .catch(() => setHasMore(false))
      .finally(() => setLoading(false));
  };

  if (logs.length === 0 && initialLoading) {
    return (
      <div className="text-body text-center py-10 text-text-secondary">
        {t("activity.odLoading")}
      </div>
    );
  }
  if (logs.length === 0 && initialError) {
    return (
      <div className="text-body text-center py-10 text-text-secondary">
        <div>{t("activity.odLoadFailed", { msg: initialError.message })}</div>
        <button
          type="button"
          onClick={onRetryInitial}
          disabled={initialLoading}
          className="mt-3 text-caption text-text-tertiary hover:text-text-primary transition-colors disabled:opacity-50"
        >
          {initialLoading ? t("activity.odLoading") : t("activity.retry")}
        </button>
      </div>
    );
  }
  if (logs.length === 0) {
    return (
      <div className="text-body text-center py-10 text-text-secondary">
        <div>{t("activity.odEmpty")}</div>
        <button
          type="button"
          onClick={refresh}
          disabled={refreshing}
          className="mt-3 text-caption text-text-tertiary hover:text-text-primary transition-colors disabled:opacity-50"
        >
          {refreshing ? t("activity.odLoading") : t("activity.odRefresh")}
        </button>
      </div>
    );
  }

  return (
    <>
      <div className="px-5 py-2 flex justify-end">
        <button
          type="button"
          onClick={refresh}
          disabled={refreshing}
          className="text-caption text-text-tertiary hover:text-text-primary transition-colors disabled:opacity-50"
        >
          {refreshing ? t("activity.odLoading") : t("activity.odRefresh")}
        </button>
      </div>
      <ul className="divide-y divide-border">
        {logs.map((log) => (
          <OnDemandRow key={log.id} log={log} onOpenLightbox={setLightboxSrc} deviceNames={deviceNames} />
        ))}
      </ul>
      {lightboxSrc && (
        <Lightbox src={lightboxSrc} onClose={() => setLightboxSrc(null)} />
      )}
      {hasMore && (
        <div className="px-5 py-3 border-t border-border flex justify-center">
          <button
            type="button"
            onClick={loadMore}
            disabled={loading}
            className="text-caption text-text-secondary hover:text-text-primary underline-offset-4 hover:underline transition-colors disabled:opacity-50"
          >
            {loading ? t("activity.odLoading") : t("activity.loadMore")}
          </button>
        </div>
      )}
    </>
  );
}

function OnDemandRow({ log, onOpenLightbox, deviceNames }: {
  log: OnDemandLogEntry;
  onOpenLightbox: (src: string) => void;
  deviceNames: Record<string, string>;
}) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const [feedbackDone, setFeedbackDone] = useState(false);
  const [feedbackPack, setFeedbackPack] = useState<{ path: string; size: number } | null>(null);
  const hasFeedback = log.has_feedback || feedbackDone;
  const packPath = feedbackPack?.path ?? log.feedback_pack_path ?? null;
  const packSize = feedbackPack?.size ?? log.feedback_pack_size ?? null;
  const hasClips = log.snapshot_count > 0;
  const clipDids = log.clip_dids ?? [];
  const allAudioOnly = hasClips && clipDids.every((did) => (log.clip_kinds?.[did] ?? "mp4") === "m4a");

  const trailing = expanded
    ? t("activity.collapse")
    : hasClips && !allAudioOnly
      ? "🎬"
      : "💬";

  return (
    <li
      onClick={() => { if (!window.getSelection()?.toString()) setExpanded((x) => !x); }}
      aria-expanded={expanded}
      className="px-5 py-2.5 hover:bg-bg-tertiary transition-colors cursor-pointer list-none"
    >
      <div className="flex flex-col gap-1 sm:grid sm:grid-cols-[70px_1fr_auto] sm:gap-x-3 sm:gap-y-1 sm:items-baseline">
        <TimeLabel timestamp={log.timestamp} />
        <div className="min-w-0 sm:order-2">
          <div className="text-body text-text-primary font-semibold break-words whitespace-pre-line">Q: {log.query}</div>
          <div className="text-body text-text-secondary break-words whitespace-pre-line mt-0.5">A: {log.answer || <span className="text-text-tertiary italic">{t("activity.odNoAnswer", "推理失败，无答案")}</span>}</div>
          <div className="text-caption-mono text-text-tertiary mt-1">
            {log.sources.map((did) => deviceNames[did] ? `${deviceNames[did]}(${did})` : did).join(", ")}
          </div>
        </div>
        <span className="text-caption-mono text-text-tertiary whitespace-nowrap sm:order-last sm:justify-self-end" aria-hidden="true">
          {trailing}
        </span>
      </div>

      {expanded && hasClips && (
        <div className="mt-3 flex gap-2 overflow-x-auto pb-2 sm:ml-[82px]">
          {clipDids.map((did) =>
            (log.clip_kinds?.[did] ?? "mp4") === "m4a"
              ? <OnDemandAudioPlayer key={did} logId={log.id} deviceId={did} />
              : <OnDemandClipPlayer key={did} logId={log.id} deviceId={did} onOpenLightbox={onOpenLightbox} />,
          )}
        </div>
      )}

      {log.has_trace && (
        <div className={expanded ? "" : "hidden"}>
          <OnDemandFeedback
            logId={log.id}
            feedbackDone={hasFeedback}
            feedbackPack={packPath && packSize ? { path: packPath, size: packSize } : null}
            onSubmitted={(path, size) => { setFeedbackDone(true); setFeedbackPack({ path, size }); }}
          />
        </div>
      )}
    </li>
  );
}

function OnDemandClipPlayer({ logId, deviceId, onOpenLightbox }: {
  logId: string; deviceId: string; onOpenLightbox: (src: string) => void;
}) {
  const { t } = useTranslation();
  const [failed, setFailed] = useState(false);
  const src = onDemandClipUrl(logId, deviceId);
  if (failed) {
    return (
      <div onClick={(e) => e.stopPropagation()} className="flex-shrink-0 w-48 h-48 rounded bg-bg-primary border border-border flex items-center justify-center text-caption-mono text-text-tertiary"
        aria-label={t("activity.clipExpiredAria")}>
        {t("activity.clipExpired")}
      </div>
    );
  }
  return (
    <div onClick={(e) => e.stopPropagation()} className="flex-shrink-0 relative group">
      <video src={src} controls preload="metadata" onError={() => setFailed(true)} onClick={(e) => e.stopPropagation()} className="w-48 h-48 rounded bg-black border border-border object-contain"
        aria-label={`${deviceId} clip`} />
      <button type="button" onClick={(e) => { e.stopPropagation(); onOpenLightbox(src); }} aria-label={t("activity.zoomPlay")}
        className="absolute top-1 right-1 w-7 h-7 rounded-full bg-black/60 hover:bg-black/80 text-white text-xs flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity">
        ⛶
      </button>
    </div>
  );
}

function OnDemandAudioPlayer({ logId, deviceId }: { logId: string; deviceId: string }) {
  const { t } = useTranslation();
  const [failed, setFailed] = useState(false);
  const src = onDemandClipUrl(logId, deviceId);
  if (failed) {
    return (
      <div onClick={(e) => e.stopPropagation()} className="flex-shrink-0 w-full px-4 py-3 rounded bg-bg-primary border border-border text-caption-mono text-text-tertiary"
        aria-label={t("activity.audioExpiredAria")}>
        {t("activity.audioExpired")}
      </div>
    );
  }
  return (
    <div onClick={(e) => e.stopPropagation()} className="flex-shrink-0 w-full px-4 py-3 rounded bg-bg-primary border border-border flex items-center gap-3">
      <span className="text-caption-mono text-text-secondary whitespace-nowrap">{t("activity.audioOnly")}</span>
      <audio src={src} controls preload="metadata" onError={() => setFailed(true)} className="flex-1 min-w-0 h-9"
        aria-label={`${deviceId} audio clip`} />
    </div>
  );
}

function OnDemandFeedback({ logId, feedbackDone, feedbackPack, onSubmitted }: {
  logId: string; feedbackDone: boolean; feedbackPack: { path: string; size: number } | null;
  onSubmitted: (path: string, size: number) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [confirmedResubmit, setConfirmedResubmit] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [text, setText] = useState("");
  const [status, setStatus] = useState<"idle" | "submitting" | "error">("idle");

  if (!open && status === "idle") {
    if (feedbackDone && !confirmedResubmit) {
      return (
        <div className="mt-2.5 sm:ml-[82px] px-3.5 py-2.5 rounded-lg bg-info-bg text-caption" onClick={(e) => e.stopPropagation()}>
          <div className="flex items-baseline justify-between">
            <span className="text-info">
              ✓ {t("activity.feedbackSaved")}
              {feedbackPack?.path && <>，<button type="button" onClick={() => { const i = feedbackPack.path.lastIndexOf("/"); if (i > 0) revealDir(feedbackPack.path.substring(0, i)).catch(() => {}); }} className="text-info underline hover:opacity-80">{t("activity.feedbackReveal")}</button></>}
              ，<a href={FEEDBACK_FORM_URL} target="_blank" rel="noopener noreferrer" className="text-info underline hover:opacity-80">{t("activity.feedbackSubmitLink")}</a>
            </span>
            <button type="button" onClick={() => { setConfirmedResubmit(true); setOpen(true); }} className="flex-shrink-0 ml-3 text-[11px] text-text-tertiary hover:text-brand-primary transition-colors">
              {t("activity.feedbackModify", "修改反馈信息")}
            </button>
          </div>
          {feedbackPack?.path && (
            <div className="mt-1.5 text-[10px] text-text-tertiary font-mono truncate" title={feedbackPack.path}>{feedbackPack.path}</div>
          )}
        </div>
      );
    }
    return (
      <div className="mt-2 sm:ml-[82px]">
        <button type="button" onClick={(e) => { e.stopPropagation(); setOpen(true); }}
          className="inline-flex items-center gap-1 px-3 py-[5px] text-caption text-text-tertiary bg-transparent border border-border rounded-md hover:text-brand-primary hover:border-brand-primary hover:bg-brand-soft transition-colors">
          <span className="text-[11px]">⚑</span> {t("activity.feedbackButton")}
        </button>
      </div>
    );
  }

  if (status === "submitting") {
    return (
      <div className="mt-2.5 sm:ml-[82px] flex items-center gap-2 px-3.5 py-2.5 rounded-lg bg-bg-tertiary text-caption text-text-secondary" onClick={(e) => e.stopPropagation()}>
        <span className="inline-block w-3 h-3 border-2 border-border border-t-brand-primary rounded-full animate-spin" />
        {t("activity.feedbackSubmitting")}
      </div>
    );
  }

  if (status === "error") {
    return (
      <div className="mt-2.5 sm:ml-[82px]" onClick={(e) => e.stopPropagation()}>
        <div className="px-3.5 py-2.5 rounded-lg text-caption" style={{ background: "rgba(220,38,38,.06)", color: "#DC2626" }}>
          ✗ {t("activity.feedbackError")}
        </div>
        <button type="button" onClick={() => setStatus("idle")} className="mt-1.5 text-caption text-text-tertiary hover:text-text-secondary">
          {t("activity.feedbackRetry")}
        </button>
      </div>
    );
  }

  const handleSubmit = async () => {
    setStatus("submitting");
    try {
      const result = await submitOnDemandFeedback(logId, [...selected], text);
      onSubmitted(result.pack_path, result.pack_size_bytes);
      setConfirmedResubmit(false);
      setStatus("idle");
      setOpen(false);
      setSelected(new Set());
      setText("");
    } catch {
      setStatus("error");
    }
  };

  return (
    <div className="mt-2.5 sm:ml-[82px] p-3.5 rounded-[10px] bg-bg-primary border border-border" onClick={(e) => e.stopPropagation()}>
      <div className="text-[13px] font-semibold text-text-primary mb-2.5">{t("activity.feedbackTitle")}</div>
      <div className="text-caption text-text-tertiary mb-1">{t("activity.feedbackErrorType")}</div>
      <div className="flex flex-wrap gap-1.5 mb-3">
        {OD_ERROR_TYPE_KEYS.map((k) => (
          <button key={k} type="button"
            onClick={() => setSelected((prev) => { const n = new Set(prev); if (n.has(k)) n.delete(k); else n.add(k); return n; })}
            className={`px-2.5 py-1 text-caption rounded-full border transition-colors ${selected.has(k) ? "text-brand-primary bg-brand-soft border-brand-primary" : "text-text-secondary bg-bg-secondary border-border hover:border-border-strong"}`}>
            {t(`activity.errorType.${k}`)}
          </button>
        ))}
      </div>
      <div className="text-caption text-text-tertiary mb-1">{t("activity.feedbackText")}</div>
      <textarea value={text} onChange={(e) => setText(e.target.value)} placeholder={t("activity.feedbackPlaceholder")}
        className="w-full h-[52px] px-2.5 py-2 border border-border rounded-lg text-[13px] bg-bg-secondary text-text-primary resize-none focus:outline-none focus:border-brand-primary transition-colors" />
      <div className="mt-2 px-2.5 py-1.5 rounded-md text-[11px] text-text-tertiary" style={{ background: "rgba(217,119,6,.06)" }}>
        ⚠ {t("activity.feedbackPrivacy")}
      </div>
      <div className="flex justify-end items-center gap-1.5 mt-2.5">
        <button type="button" onClick={() => { setOpen(false); setConfirmedResubmit(false); setSelected(new Set()); setText(""); }}
          className="px-3.5 py-[5px] text-caption text-text-secondary rounded-md hover:bg-bg-tertiary transition-colors">
          {t("activity.feedbackCancel")}
        </button>
        <button type="button" onClick={handleSubmit}
          className="px-4 py-[5px] text-caption font-medium text-white bg-brand-primary rounded-md hover:bg-brand-accent transition-colors">
          {t("activity.feedbackSubmit")}
        </button>
      </div>
    </div>
  );
}
