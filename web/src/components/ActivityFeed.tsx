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

import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { eventClipUrl, listActivity, submitEventFeedback, subscribeEvents } from "@/api";
import { humanizeRulesInText } from "@/lib/eventText";
import { smartTimeParts } from "@/lib/relativeTime";
import type { ActivityEvent, HomeId } from "@/lib/types";

interface Props {
  events: ActivityEvent[];
  /** 当前作用域 home;切换时整个列表 + SSE 都要重建 */
  homeId: HomeId;
}

const PAGE_SIZE = 50;
const FILTER_DEBOUNCE_MS = 300;

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

export function ActivityFeed({ events: initial, homeId }: Props) {
  const { t } = useTranslation();
  const [events, setEvents] = useState<ActivityEvent[]>(initial);
  // since 默认今天 00:00,跟标题语义对齐;before 留空 → 后端取 now,允许"看到现在".
  // 用户改 since 看更早历史 / 设 before 卡截止 / 清 since 看全量.
  const [since, setSince] = useState<number | undefined>(todayStartMs);
  const [before, setBefore] = useState<number | undefined>();
  /** debounced 版本,用于 SSE useEffect deps / filter fetch — 避免 datetime-local
   *  每字符 onChange 触发 EventSource 频繁建/拆 (N3) + reload churn */
  const [appliedSince, setAppliedSince] = useState<number | undefined>(todayStartMs);
  const [appliedBefore, setAppliedBefore] = useState<number | undefined>();
  const [loading, setLoading] = useState(false);
  const [feedbackSet, setFeedbackSet] = useState<Set<string>>(new Set());
  const [feedbackPacks, setFeedbackPacks] = useState<Map<string, { path: string; size: number }>>(new Map());
  /** 已拉取的 offset(下次"查看更早"从这开始).事件量动态变(SSE prepend),
   *  分页用 since/before+offset 而非纯 offset 不够;约定 offset = 当前已 loaded 历史段长 */
  const [offset, setOffset] = useState(0);
  /** 后端最近一次 GET 是否还满 PAGE_SIZE(可能还有更早) */
  const [hasMore, setHasMore] = useState(false);
  /** Promise generation token — stale fetch resolve 时丢弃(N1) */
  const fetchGenRef = useRef(0);
  /** 全屏播放器(点开看大):null 关闭 */
  const [lightboxSrc, setLightboxSrc] = useState<string | null>(null);

  const filterActive = appliedSince !== undefined || appliedBefore !== undefined;

  // N3: filter input 抖动 debounce 300ms 后才应用 → 触发 fetch + SSE 重建
  useEffect(() => {
    const t = setTimeout(() => {
      setAppliedSince(since);
      setAppliedBefore(before);
    }, FILTER_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [since, before]);

  /** 统一拉取(filter / 翻页 / SSE 重连 reload 共用),fetchGen 守 stale overwrite. */
  const fetchPage = (opts: {
    append?: boolean;
    pageOffset?: number;
  }) => {
    const gen = ++fetchGenRef.current;
    const pageOffset = opts.pageOffset ?? 0;
    setLoading(true);
    return listActivity(homeId, {
      since: appliedSince,
      before: appliedBefore,
      limit: PAGE_SIZE,
      offset: pageOffset,
    })
      .then((fresh) => {
        if (gen !== fetchGenRef.current) return; // N1: stale,丢弃
        if (opts.append) {
          // append 期间 SSE 可能已经 prepend 新事件,简单 [...prev, ...fresh]
          // 会让"更早的 fresh"夹在"SSE 推的更晚事件"中间 → 视觉乱序.
          // mergeAndSort 按 id dedup + 按 timestamp DESC 重排兜底,得到稳定顺序.
          setEvents((prev) => mergeAndSort(prev, fresh));
          setOffset(pageOffset + fresh.length);
        } else {
          setEvents(fresh);
          setOffset(fresh.length);
        }
        setHasMore(fresh.length === PAGE_SIZE);
      })
      .catch(() => {
        if (gen !== fetchGenRef.current) return;
        if (!opts.append) setEvents([]);
        setHasMore(false);
      })
      .finally(() => {
        if (gen !== fetchGenRef.current) return;
        setLoading(false);
      });
  };

  // M5/N2: prop 变(homeId 切换 / 父组件 reload)时同步 — 但仅当 filter 未激活.
  // 不用 setEvents(initial) 直接抹掉 SSE 累积:filter 清空时 fetchPage({}) 主动拉一次,
  // 让 backend 给出最新 + SSE merge 已 accumulated 的事件,避免清 filter 瞬间闪烁.
  // useState(initial) 只首次生效,prop 后续变化由这里同步.
  useEffect(() => {
    if (!filterActive) {
      // 首次 mount 时 initial 跟 useState 一致,fetchPage 也能 idempotent 拿到一样数据.
      // 但用 initial 直接 setEvents 跳过一次 round-trip,首屏更快;后续 homeId 变化
      // 时 initial 已经是新 home 的数据(父组件 useAsync deps=[homeId] 已重拉).
      setEvents(initial);
      setOffset(initial.length);
      setHasMore(initial.length === PAGE_SIZE);
    }
    // filterActive 时 ignore initial 变化 — 由 filter useEffect 主导
  }, [initial, homeId, filterActive]);

  // filter 变化时主动拉取(homeId 也走这里)
  useEffect(() => {
    if (!filterActive) return; // 未筛选时由 prop sync useEffect 接管
    fetchPage({ pageOffset: 0 });
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

    // 重连成功 / 首次 open 时拉一次,补回断开期间错过的事件(spec B13).
    // 走 fetchPage 享 gen 保护;不会 overwrite SSE prepend 的更新事件 — fetchPage 拉到
    // 的列表会和 SSE merge 的 in-memory 版本一致(后端是 SoT).
    const reload = () => {
      fetchPage({ pageOffset: 0 });
    };

    const start = () => {
      if (unsub) return;
      unsub = subscribeEvents(
        (e) => {
          if (!inRange(e.timestamp)) return; // 越界事件丢弃
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appliedSince, appliedBefore, homeId]);

  /** 触发翻页:offset += PAGE_SIZE,append 模式 */
  const loadMore = () => {
    if (loading || !hasMore) return;
    fetchPage({ append: true, pageOffset: offset });
  };

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
            {/* 已加载 N 条 — 仅反映当前内存中加载/累积的数量;hasMore=true 时后端
                还有更早的事件可拉,N 不等于"事件总数" */}
            {t("activity.loadedCount", {
              n: events.length,
              more: hasMore ? "+" : "",
            })}
          </span>
        </h2>
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

      {loading && events.length === 0 ? (
        <div className="text-body text-center py-10 text-text-secondary">
          {t("activity.loading")}
        </div>
      ) : events.length === 0 ? (
        <div className="text-body text-center py-10 text-text-secondary">
          {filterActive
            ? t("activity.emptyFiltered")
            : t("activity.emptyDefault")}
        </div>
      ) : (
        <ul className="divide-y divide-border">
          {events.map((e) => (
            <ActivityRow key={e.id} event={e} onOpenLightbox={setLightboxSrc} feedbackSet={feedbackSet} feedbackPacks={feedbackPacks} onFeedbackSubmitted={(id, path, size) => { setFeedbackSet(prev => new Set(prev).add(id)); setFeedbackPacks(prev => new Map(prev).set(id, { path, size })); }} />
          ))}
        </ul>
      )}

      {hasMore && events.length > 0 && (
        <div className="px-5 py-3 border-t border-border flex justify-center">
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

      {lightboxSrc && (
        <Lightbox src={lightboxSrc} onClose={() => setLightboxSrc(null)} />
      )}
    </section>
  );
}

/** 全屏播放器 — 点 backdrop / Esc 关闭. mp4 走 <video controls>,audio-only m4a 同样
 *  用 <video>(浏览器对纯音频 mp4/m4a render 黑底 + 音轨).
 *
 *  M1: 不加 autoPlay — Chrome/Safari autoplay policy 会拦截带音轨自动播放(modal
 *      是 fresh element,不继承父点击的 user gesture);改让用户主动按 ▶,体验稳定.
 *  S2: 挂载时 pause 页面里所有其他 <video>,避免 inline ClipPlayer 跟 Lightbox 同时
 *      出声(用 querySelectorAll 一次性处理,避免 prop drill).
 *  S6: keydown 通过 useRef(onClose) 解耦,空 deps,避免父组件每次 render 都重绑. */
function Lightbox({ src, onClose }: { src: string; onClose: () => void }) {
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
      aria-label={t("activity.playback")}
    >
      <button
        type="button"
        onClick={onClose}
        aria-label={t("activity.close")}
        className="absolute top-4 right-4 w-10 h-10 rounded-full bg-white/10 hover:bg-white/20 text-white text-xl flex items-center justify-center"
      >
        ✕
      </button>
      <video
        ref={videoRef}
        src={src}
        controls
        className="max-w-full max-h-full rounded shadow-lg cursor-default bg-black"
        onClick={(e) => e.stopPropagation()}
      />
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

function TimeLabel({ timestamp }: { timestamp: number }) {
  // 双行布局:第 1 行日期(YYYY/MM/DD 或"今天/昨天"),第 2 行时分秒.
  // sm+(>=640px):父 grid 三列(70px / 1fr / auto),TimeLabel 在 70px 列内
  // sm:justify-self-stretch 占满,两行 sm:text-center 各自居中(等宽字体下日期
  // 10 字符撑满列宽,时间 8 字符居中显著).
  // mobile(<640px):父 flex-col 纵向堆叠,TimeLabel 自然左对齐,不加 text-center
  // 保持跟下方 text-body 文本同锚线(否则居中会让 feed 视觉节奏断裂).
  const { time, date } = smartTimeParts(timestamp);
  return (
    <div className="sm:justify-self-stretch leading-tight">
      <div className="text-caption-mono text-text-secondary whitespace-nowrap sm:text-center">
        {date}
      </div>
      <div className="text-caption-mono text-text-tertiary whitespace-nowrap sm:text-center">
        {time}
      </div>
    </div>
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
  onOpenLightbox: (src: string) => void;
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
  const sections = useMemo(
    () => humanized.split(/\n\n/).filter((s) => s.trim()),
    [humanized],
  );

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
      onClick={() => setExpanded((x) => !x)}
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
                {s}
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
            <ClipPlayer
              key={did}
              event_id={event.id}
              device_id={did}
              onOpenLightbox={onOpenLightbox}
            />
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

      {expanded && event.has_trace && (
        <FeedbackSection
          eventId={event.id}
          hasFeedback={event.has_feedback || feedbackSet.has(event.id)}
          packPath={feedbackPacks.get(event.id)?.path ?? event.feedback_pack_path}
          packSize={feedbackPacks.get(event.id)?.size ?? event.feedback_pack_size}
          onSubmitted={onFeedbackSubmitted}
        />
      )}
    </li>
  );
}

const ERROR_TYPES = [
  "人物识别错误",
  "宠物识别错误",
  "动作识别错误",
  "环境/设备状态识别错误",
  "语音识别错误",
  "规则/建议误触发",
  "其他",
] as const;

function FeedbackSection({ eventId, hasFeedback, packPath, packSize, onSubmitted }: {
  eventId: string;
  hasFeedback?: boolean;
  packPath?: string | null;
  packSize?: number | null;
  onSubmitted: (eventId: string, path: string, size: number) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [confirmedResubmit, setConfirmedResubmit] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [text, setText] = useState("");
  const [includeGallery, setIncludeGallery] = useState(false);
  const [status, setStatus] = useState<"idle" | "submitting" | "success" | "saved" | "error">("idle");
  const [packInfo, setPackInfo] = useState<{ path: string; size: number } | null>(null);

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
      setPackInfo({ path: result.pack_path, size: result.pack_size_bytes });
      onSubmitted(eventId, result.pack_path, result.pack_size_bytes);
      setConfirmedResubmit(false);
      const nextStatus = result.uploaded ? "success" : "saved";
      setStatus(nextStatus);
      setTimeout(() => {
        setStatus("idle");
        setOpen(false);
        setSelected(new Set());
        setText("");
        setIncludeGallery(false);
      }, nextStatus === "success" ? 3000 : 5000);
    } catch {
      setStatus("error");
    }
  };

  // idle: 反馈按钮 or 已反馈（显示路径）
  if (!open && status === "idle") {
    if (hasFeedback && !confirmedResubmit) {
      return (
        <div className="mt-2.5 sm:ml-[82px] px-3.5 py-2.5 rounded-lg text-caption" style={{ background: "rgba(37,99,235,.06)", color: "#2563EB" }} onClick={(e) => e.stopPropagation()}>
          <div>✓ {t("activity.feedbackSaved", "反馈已记录，数据已保存到本地")}</div>
          {packPath && (
            <div className="mt-1.5 text-[11px] text-text-tertiary space-y-0.5">
              <div>{t("activity.feedbackPath", "文件路径")}：<code className="bg-bg-secondary px-1.5 py-0.5 rounded text-[10px] font-mono border border-border select-all">{packPath}</code></div>
              {packSize != null && <div>{t("activity.feedbackSize", "数据包大小")}：{(packSize / 1024).toFixed(0)} KB</div>}
            </div>
          )}
          <button type="button" onClick={() => { setConfirmedResubmit(true); setOpen(true); }} className="mt-1.5 text-[11px] text-brand-primary hover:underline">
            {t("activity.feedbackResubmit", "再次反馈")}
          </button>
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

  // success (uploaded)
  if (status === "success") {
    return (
      <div className="mt-2.5 sm:ml-[82px] px-3.5 py-2.5 rounded-lg text-caption" style={{ background: "rgba(22,163,74,.06)", color: "#16A34A" }} onClick={(e) => e.stopPropagation()}>
        <div>✓ {t("activity.feedbackUploaded", "反馈已提交，感谢反馈")}</div>
        {packInfo && (
          <div className="mt-1 text-[11px] opacity-70">
            {t("activity.feedbackSize", "数据包大小")}: {(packInfo.size / 1024).toFixed(0)} KB
          </div>
        )}
      </div>
    );
  }

  // saved (local)
  if (status === "saved") {
    return (
      <div className="mt-2.5 sm:ml-[82px] px-3.5 py-2.5 rounded-lg text-caption" style={{ background: "rgba(37,99,235,.06)", color: "#2563EB" }} onClick={(e) => e.stopPropagation()}>
        <div>✓ {t("activity.feedbackSaved", "反馈已记录，数据已保存到本地")}</div>
        {packInfo && (
          <div className="mt-1.5 text-[11px] text-text-tertiary space-y-0.5">
            <div>{t("activity.feedbackPath", "文件路径")}：<code className="bg-bg-secondary px-1.5 py-0.5 rounded text-[10px] font-mono border border-border select-all">{packInfo.path}</code></div>
            <div>{t("activity.feedbackSize", "数据包大小")}：{(packInfo.size / 1024).toFixed(0)} KB</div>
          </div>
        )}
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

      <div className="flex flex-wrap gap-1.5 mb-3">
        {ERROR_TYPES.map((type) => (
          <button
            key={type}
            type="button"
            onClick={() => handleToggle(type)}
            className={`px-2.5 py-1 text-caption rounded-full border transition-colors ${
              selected.has(type)
                ? "text-brand-primary bg-brand-soft border-brand-primary"
                : "text-text-secondary bg-bg-secondary border-border hover:border-border-strong"
            }`}
          >
            {type}
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
        ⚠ {t("activity.feedbackPrivacy", "提交反馈将上传相关音视频和感知数据")}
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
          {t("activity.feedbackSubmit", "提交反馈")}
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
  onOpenLightbox: (src: string) => void;
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
          onOpenLightbox(src);
        }}
        aria-label={t("activity.zoomPlay")}
        className="absolute top-1 right-1 w-7 h-7 rounded-full bg-black/60 hover:bg-black/80 text-white text-xs flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity"
      >
        ⛶
      </button>
    </div>
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
