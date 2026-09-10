/**
 * 「模型」页顶部的 omni 模型配置卡(可折叠,默认展开)。
 *
 * 两块:
 * - 上:**当前模型** —— 当前生效配置(model / Base URL / 打码 key);未配 key 给警告。
 * - 下:**模型列表** —— 每行 模型 | Base URL | API Key(打码),可「启用」/「删除」;
 *   「＋ 新增」展开表单(Base URL → API Key → 模型组合框 + 测试连接 + 保存)。
 *
 * 档案名对用户隐藏:内部用 `model @ base_url` 作为后端 label(唯一 id)。重复添加同
 * (model, base_url) = 更新该配置的 key(等价编辑)。后端按 label activate/delete/upsert。
 * 保存写 config.json,感知下个推理周期热生效(免重启);api_key 打码、留空=沿用原 key。
 */

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  OMNI_CONFIG_STALE_EVENT,
  getOmniConfig,
  updateOmniConfig,
  activateOmniConfig,
  deactivateOmniConfig,
  deleteOmniConfig,
  listOmniModels,
  testOmniConfig,
} from "@/api";
import type { OmniConfigState, OmniHealth, OmniProfile, OmniTestResult } from "@/lib/types";
import { IconX, IconEye, IconEyeOff, IconChevronDown, IconChevronUp } from "@/lib/icons";
import { relativeTime, smartTimeLabel } from "@/lib/relativeTime";
import { placePopover } from "@/lib/popoverPlace";
import { toast } from "./Toast";

const INPUT_CLS =
  "w-full px-3 py-2 rounded-lg bg-bg-primary border border-border " +
  "focus:border-brand-primary focus:outline-none text-text-primary num";

// omni 测试 / 模型列表的后端机器码 → i18n key;命中走前端本地化,
// 未命中(如 http_error,含动态 HTTP 细节)回退后端 message。
// 与 error_classifier.CODES 保持一致(10 个 code):测试路径撞 429 会返 rate_limited、
// 200+非 JSON body 会返 bad_response,retry 被中断会返 cancelled。缺 code = 回退
// 后端硬编码中文 message、污染英文界面。
const OMNI_CODE_KEY: Record<string, string> = {
  ok: "usage.testOk",
  bad_key: "usage.testBadKey",
  not_found: "usage.testNotFound",
  rejected_authed: "usage.testRejectedAuthed",
  unreachable: "usage.testUnreachable",
  no_key: "usage.testNoKey",
  http_error: "usage.testHttpError",
  rate_limited: "usage.testRateLimited",
  bad_response: "usage.testBadResponse",
  cancelled: "usage.testCancelled",
};

// 测试结果三档语义:连接正常(✓ 绿,chat 调通)/ 鉴权过但探测被拒(⚠ 黄,rejected_authed)/ 失败(✗ 红)。
const TEST_WARN_CODES = new Set(["rejected_authed"]);
type Severity = "ok" | "warn" | "error";
function severityOf(res: OmniTestResult): Severity {
  if (res.code && TEST_WARN_CODES.has(res.code)) return "warn";
  return res.ok ? "ok" : "error";
}
const SEV_GLYPH: Record<Severity, string> = { ok: "✓", warn: "⚠", error: "✗" };
const SEV_CLASS: Record<Severity, string> = {
  ok: "text-success",
  warn: "text-warning",
  error: "text-error",
};

// 模型列的状态标记:形状 + 颜色双编码(色弱也能辨认),判断顺序见 markerStateOf。
type MarkerState = "testing" | "err" | "warn" | "ok" | "fail" | "untested";
const MARKER_GLYPH: Record<MarkerState, string> = {
  testing: "◌",
  err: "✕",
  warn: "▲",
  ok: "●",
  fail: "✕",
  untested: "○",
};
const MARKER_CLASS: Record<MarkerState, string> = {
  testing: "text-text-tertiary animate-pulse",
  err: "text-error",
  warn: "text-warning",
  ok: "text-success",
  fail: "text-error",
  untested: "text-text-tertiary",
};

// 拉模型/测试失败的机器码 → 该错误属于哪个表单字段(就近显示,而非全堆模型框下)。
// key 类(鉴权)→ API Key;可达性/地址类 → Base URL;其余(空结果/未知)→ 模型框。
function errFieldOf(code: string | null): "url" | "key" | "model" {
  if (code === "bad_key" || code === "no_key") return "key";
  if (code === "unreachable" || code === "http_error" || code === "not_found") return "url";
  return "model";
}

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function Field({
  label,
  children,
  className = "",
}: {
  label: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <label className={`block ${className}`}>
      <span className="text-caption text-text-secondary mb-1 block">{label}</span>
      {children}
    </label>
  );
}

/** 通用组合框:可输入(自由文本)+ 尖括号展开面板点选。输入时按子串过滤;已选/空时点它看全部。 */
function ComboBox({
  value,
  onChange,
  options,
  placeholder,
  ariaLabel,
}: {
  value: string;
  onChange: (v: string) => void;
  options: string[];
  placeholder?: string;
  ariaLabel?: string;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const h = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [open]);
  const q = value.trim().toLowerCase();
  const exact = options.some((o) => o.toLowerCase() === q);
  const list = !q || exact ? options : options.filter((o) => o.toLowerCase().includes(q));
  return (
    <div className="relative" ref={ref}>
      <input
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        placeholder={placeholder}
        className={INPUT_CLS + " pr-9"}
        autoComplete="off"
        aria-label={ariaLabel}
      />
      {options.length > 0 && (
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="absolute right-2 top-1/2 -translate-y-1/2 text-text-tertiary px-1 leading-none"
          aria-label={t("usage.expandOptions")}
        >
          <IconChevronDown width={16} height={16} />
        </button>
      )}
      {open && list.length > 0 && (
        <ul className="absolute left-0 right-0 mt-1 z-20 max-h-60 overflow-auto rounded-lg bg-bg-secondary border border-border shadow-md py-1">
          {list.map((o) => (
            <li key={o}>
              <button
                type="button"
                onClick={() => {
                  onChange(o);
                  setOpen(false);
                }}
                className="w-full text-left px-3 py-2 text-caption text-text-primary hover:bg-bg-primary num"
              >
                {o}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function UsageOmniConfig() {
  const { t } = useTranslation();
  const [state, setState] = useState<OmniConfigState | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(false); // 默认展开

  // 新增 / 编辑表单(共用):editing 非空表示在编辑该 label 对应的已有配置
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [showKey, setShowKey] = useState(false); // API Key 明文/密文切换(末端眼睛图标)
  const [model, setModel] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsMsg, setModelsMsg] = useState<string | null>(null);
  const [modelsErr, setModelsErr] = useState(false); // modelsMsg 是否为错误(决定红色突出)
  const [modelsErrCode, setModelsErrCode] = useState<string | null>(null); // 错误机器码(决定就近显示在哪个字段)
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<OmniTestResult | null>(null);
  // 列表行内「测试」:正在测的 label(结果落在后端 last_verified,测完 load() 刷新即可,不再本地缓存)
  const [rowTesting, setRowTesting] = useState<string | null>(null);
  const [activating, setActivating] = useState<string | null>(null); // 正在「启用前测试+启用」的 label
  const [deactivating, setDeactivating] = useState<string | null>(null); // 正在「停用」的 label
  // 模型列状态标记的悬浮气泡:锚定元素底部的全文浮层(fixed 定位,免原生 title 延迟、不被表格 overflow 裁剪)。
  // anchor 记锚点视口坐标 + 待展示文案,pos 由 layout effect 量出气泡真实尺寸后经 placePopover 算出
  // (下方装不下就翻上方,两轴夹回视口内,与 UsageUrlChip 的清理菜单同一套算法)。
  const [tipAnchor, setTipAnchor] = useState<{ rect: DOMRect; lines: string[] } | null>(null);
  const [tipPos, setTipPos] = useState<{ left: number; top: number } | null>(null);
  const tipRef = useRef<HTMLDivElement | null>(null);
  // 删除确认弹窗(web 风格,代替 window.confirm):待删项 + 删除中
  const [deleteTarget, setDeleteTarget] = useState<OmniProfile | null>(null);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    void load();
    // OmniHealthBanner 的 SSE 重连(backend 重启后)会 dispatch 此事件,
    // 让当前页面同步 refetch 最新 config,避免视觉与实际状态错位。
    const onStale = () => void load();
    window.addEventListener(OMNI_CONFIG_STALE_EVENT, onStale);
    return () => window.removeEventListener(OMNI_CONFIG_STALE_EVENT, onStale);
  }, []);

  // silent = true:失败只弹 toast,不写 loadErr(用于行内操作后的刷新,避免一次网络抖动
  // 就把整张卡片(表头/全部档案行/操作按钮)替换成错误页,导致用户再无可点元素恢复)。
  async function load(opts?: { silent?: boolean }) {
    try {
      setState(await getOmniConfig());
      setLoadErr(null);
    } catch (e) {
      const msg = e instanceof Error ? e.message : t("usage.configLoadError");
      if (opts?.silent) {
        toast(msg, "danger");
      } else {
        setLoadErr(msg);
      }
    }
  }

  const profiles = state?.profiles ?? [];
  const active = state?.active;
  const hasKey = active?.has_key ?? false;
  // 新增表单里同 (model, base_url) 是否已存(→ 改为更新该条)
  const existing = profiles.find(
    (p) => p.base_url === baseUrl.trim() && p.model === model.trim(),
  );

  function startAdd() {
    setAdding(true);
    setEditing(null);
    setBaseUrl("");
    setApiKey("");
    setShowKey(false);
    setModel("");
    setModels([]);
    setModelsMsg(null);
    setModelsErr(false);
    setModelsErrCode(null);
    setTestResult(null);
  }

  // 编辑已有配置:预填 base_url / model,key 留空(占位提示「留空则不修改」),复用同一表单与 onSave 的 upsert。
  function startEdit(p: OmniProfile) {
    setAdding(true);
    setEditing(p.label);
    setBaseUrl(p.base_url);
    setApiKey("");
    setShowKey(false);
    setModel(p.model);
    setModels([]);
    setModelsMsg(null);
    setModelsErr(false);
    setModelsErrCode(null);
    setTestResult(null);
    void fetchModels(p.base_url, "", p.label);
  }

  // label 非空时:编辑态下未填新 key 也能让后端用该档案存档 key 拉模型(否则需 key 的厂商
  // 会回 bad_key,一打开编辑就误报红错)。
  async function fetchModels(bu: string, key: string, label?: string | null) {
    if (!bu.trim()) return;
    setModelsLoading(true);
    setModelsMsg(null);
    setModelsErr(false);
    setModelsErrCode(null);
    try {
      const res = await listOmniModels({
        base_url: bu.trim(),
        api_key: key.trim() || undefined,
        label: label || undefined,
      });
      if (res.ok) {
        setModels(res.models);
        if (!res.models.length) setModelsMsg(t("usage.modelsEmptyResult"));
      } else {
        setModels([]);
        const k = res.code ? OMNI_CODE_KEY[res.code] : undefined;
        setModelsMsg(k ? t(k) : res.message || t("usage.modelsFetchFailed"));
        setModelsErr(true);
        setModelsErrCode(res.code ?? null);
      }
    } catch (e) {
      setModels([]);
      setModelsMsg(e instanceof Error ? e.message : t("usage.modelsFetchFailed"));
      setModelsErr(true);
      setModelsErrCode("unreachable"); // 网络/解析异常归为 Base URL 不可达
    } finally {
      setModelsLoading(false);
    }
  }

  async function onSave() {
    const bu = baseUrl.trim();
    const m = model.trim();
    if (!bu || !m) {
      toast(t("usage.baseUrlModelRequired"), "warn");
      return;
    }
    // 目标条目:编辑态用被编辑的 label;否则按 (model, base_url) 命中已有(隐式 upsert)。
    // 用 ||(非 ??)让空串落空 → 当作新增并生成 label,绝不把空 original_label 发给后端。
    const target = editing || existing?.label || undefined;
    if (!apiKey.trim() && !editTarget(target)?.has_key) {
      toast(t("usage.apiKeyRequired"), "warn");
      return;
    }
    setSaving(true);
    try {
      const s = await updateOmniConfig({
        label: target ?? `${m} @ ${bu}`,
        model: m,
        base_url: bu,
        api_key: apiKey.trim() || undefined,
        original_label: target,
        activate: false, // 只入列表;启用由模型列表的「启用」负责
      });
      setState(s);
      setAdding(false);
      setEditing(null);
      toast(t("usage.saveSuccess"), "ok");
    } catch (e) {
      toast(e instanceof Error ? e.message : t("usage.saveFailed"), "danger");
    } finally {
      setSaving(false);
    }
  }

  // 按 label 找列表里的条目(用于「留空不改 key」的 has_key 判断)
  function editTarget(label: string | null | undefined): OmniProfile | undefined {
    return label ? profiles.find((p) => p.label === label) : undefined;
  }

  async function onTest() {
    const bu = baseUrl.trim();
    const m = model.trim();
    if (!bu || !m) {
      toast(t("usage.baseUrlModelRequired"), "warn");
      return;
    }
    const target = editing || existing?.label || undefined;
    if (!apiKey.trim() && !editTarget(target)?.has_key) {
      toast(t("usage.apiKeyRequiredBeforeTest"), "warn");
      return;
    }
    setTesting(true);
    setTestResult(null);
    try {
      const res = await testOmniConfig({
        label: target ?? "",
        model: m,
        base_url: bu,
        api_key: apiKey.trim() || undefined,
      });
      setTestResult(res);
    } catch (e) {
      setTestResult({ ok: false, message: e instanceof Error ? e.message : t("usage.testFailed") });
    } finally {
      setTesting(false);
    }
  }

  // 列表行内「测试」:对已存档的该条按 label 测(用存档 key,无需带 key)。结果由后端写入
  // 该档案的 last_verified,测完 load() 刷新一次即可让模型列的标记 + 气泡吃到最新结果。
  async function onTestRow(p: OmniProfile) {
    setRowTesting(p.label);
    try {
      const res = await testOmniConfig({ label: p.label, model: p.model, base_url: p.base_url });
      // 测通当前生效那套时,后端已主动清熔断;结果落在 last_verified/health,
      // 统一靠下面这次 load() 刷新即可让标记从"熔断红/黄"变回测试结果,
      // 不再额外派发 stale 事件(否则其 listener 会再触发一次 load(),一次点击两个并发 GET)。
      await load({ silent: true });
      // 部分失败(如 no_key)后端不落 last_verified,标记不会变化,靠这条 toast 兜底反馈;
      // 与 onActivate 的「不可启用」toast 共用同一套 testReason 文案。
      if (severityOf(res) !== "ok") {
        toast(testReason(res), severityOf(res) === "warn" ? "warn" : "danger");
      }
    } catch (e) {
      toast(e instanceof Error ? e.message : t("usage.testFailed"), "danger");
    } finally {
      setRowTesting(null);
    }
  }

  // 启用前先跑一次测试(用存档 key 真正探测模型):仅「连接正常」(✓绿)才放行启用;否则不启用,
  // 顶部 toast 给出原因。测试结果落在后端 last_verified,测完统一 load() 刷新,让模型列的
  // 标记 + 气泡跟上(无论最终是否放行启用)。
  async function onActivate(p: OmniProfile) {
    setActivating(p.label);
    try {
      const res = await testOmniConfig({ label: p.label, model: p.model, base_url: p.base_url });
      await load({ silent: true });
      if (severityOf(res) !== "ok") {
        toast(`${t("usage.cannotEnable")}：${testReason(res)}`, severityOf(res) === "warn" ? "warn" : "danger");
        return;
      }
      setState(await activateOmniConfig({ label: p.label }));
      toast(t("usage.activateSuccess"), "ok");
    } catch (e) {
      toast(e instanceof Error ? e.message : t("usage.activateFailed"), "danger");
    } finally {
      setActivating(null);
    }
  }

  // 停用当前生效模型:回未配态 + 软停感知,保留档案(可再启用)。与「启用」对称的反向操作。
  async function onDeactivate(p: OmniProfile) {
    setDeactivating(p.label);
    try {
      setState(await deactivateOmniConfig({ label: p.label }));
      toast(t("usage.deactivateSuccess"), "ok");
    } catch (e) {
      toast(e instanceof Error ? e.message : t("usage.deactivateFailed"), "danger");
    } finally {
      setDeactivating(null);
    }
  }

  // 实际删除(由弹窗「删除」按钮触发);删的若是当前生效项,后端会软停感知并回到未配态。
  async function confirmDelete() {
    const p = deleteTarget;
    if (!p) return;
    setDeleting(true);
    try {
      setState(await deleteOmniConfig({ label: p.label }));
      toast(t("usage.deleteSuccess"), "ok");
      setDeleteTarget(null);
    } catch (e) {
      toast(e instanceof Error ? e.message : t("usage.deleteFailed"), "danger");
    } finally {
      setDeleting(false);
    }
  }

  // 模型列状态标记的悬浮气泡:锚定元素底部的 fixed 浮层(避开表格 overflow 裁剪、无原生 title 延迟)。
  // 鼠标移上去就弹,不等文字被截断。
  function showTip(e: React.MouseEvent<HTMLElement> | React.FocusEvent<HTMLElement>, lines: string[]) {
    setTipAnchor({ rect: e.currentTarget.getBoundingClientRect(), lines });
  }
  function hideTip() {
    setTipAnchor(null);
    setTipPos(null);
  }

  // fixed 定位在滚动后会失锚(页面滚了,气泡不动)→ 直接关掉,不做跟随;
  // 与 UsageUrlChip 的落法一致,避免同一张表两处浮层滚动行为不一致。
  useEffect(() => {
    if (!tipAnchor) return;
    window.addEventListener("scroll", hideTip, true);
    window.addEventListener("resize", hideTip);
    return () => {
      window.removeEventListener("scroll", hideTip, true);
      window.removeEventListener("resize", hideTip);
    };
  }, [tipAnchor]);

  // 量出气泡真实尺寸之后再定位:下方装不下就翻到上方,两轴夹回视口内(placePopover)。
  useLayoutEffect(() => {
    if (!tipAnchor || !tipRef.current) return;
    const box = tipRef.current.getBoundingClientRect();
    const next = placePopover(
      tipAnchor.rect,
      { width: box.width, height: box.height },
      { width: window.innerWidth, height: window.innerHeight },
      { gap: 4, edge: 8, align: "left" },
    );
    setTipPos((p) => (p && p.left === next.left && p.top === next.top ? p : next));
  }, [tipAnchor]);

  // 该行模型列标记状态:先到先得,依次判断测试中 / active 行的熔断 error / warn /
  // last_verified 成功 / last_verified 失败 / 从未验证过。
  function markerStateOf(p: OmniProfile, health: OmniHealth | undefined): MarkerState {
    if (rowTesting === p.label || activating === p.label) return "testing";
    if (p.active && health && health.state !== "ok") {
      return health.state === "error" ? "err" : "warn";
    }
    if (p.last_verified && p.last_verified.ok) return "ok";
    if (p.last_verified && !p.last_verified.ok) {
      // 与 severityOf 共用同一套三档口径:鉴权过但探测被拒(rejected_authed)算 warn,不算 fail。
      return p.last_verified.code && TEST_WARN_CODES.has(p.last_verified.code) ? "warn" : "fail";
    }
    return "untested";
  }

  // relativeTime 只在一小时内返回真正的相对量(刚刚 / N 分钟前),再往前返回的
  // 是"11:32"、"昨天 11:32"、"9 月 8 日"这类绝对写法,与 smartTimeLabel 的
  // 输出重复,补进括号只是把同一个时刻换个格式再写一遍。
  function lastVerifiedLine(atMs: number): string {
    const abs = smartTimeLabel(atMs);
    const withinHour = Date.now() - atMs < 3600_000;
    return withinHour
      ? t("usage.tipLastVerified", { time: abs, rel: relativeTime(atMs) })
      : t("usage.tipLastVerifiedAbs", { time: abs });
  }

  // 该标记对应的悬浮气泡内容(只放表格里没有的信息:不重复 Base URL / 模型名 / API Key)。
  function tipLinesOf(p: OmniProfile, health: OmniHealth | undefined, st: MarkerState): string[] {
    if (st === "testing") return [t("usage.tipTesting")];
    if (st === "err" || (st === "warn" && health && health.state !== "ok")) {
      const h = health!;
      const reason =
        h.code && h.code !== "http_error"
          ? t(`omniHealth.codes.${h.code}`, { defaultValue: h.message })
          : h.message;
      const failures =
        h.consecutive_failures > 0
          ? ` · ${t("omniHealth.failuresCount", { n: h.consecutive_failures })}`
          : "";
      const lines = [reason + failures];
      if (h.last_probe_at_ms) {
        lines.push(
          t("usage.tipLastVerified", {
            time: smartTimeLabel(h.last_probe_at_ms),
            rel: relativeTime(h.last_probe_at_ms),
          }),
        );
      }
      if (st === "warn" && h.next_probe_in_seconds != null) {
        lines.push(t("usage.tipNextRetry", { n: h.next_probe_in_seconds }));
      }
      return lines;
    }
    if (st === "ok") {
      const lv = p.last_verified!;
      const latency = lv.latency_ms != null ? ` · ${lv.latency_ms}ms` : "";
      return [
        `${t("usage.tipAvailable")}${latency}`,
        lastVerifiedLine(lv.at_ms),
      ];
    }
    if (st === "fail" || st === "warn") {
      // warn 走到这里说明是 last_verified 的 rejected_authed(health 未处于 error/warn,
      // 即非 active 行,或 active 行熔断已 CLOSED),与上面 health 分支互斥。
      const lv = p.last_verified!;
      const k = lv.code ? OMNI_CODE_KEY[lv.code] : undefined;
      return [
        k ? t(k) : lv.message,
        lastVerifiedLine(lv.at_ms),
      ];
    }
    return [t("usage.tipUntested"), t("usage.tipUntestedHint")];
  }

  // 测试结果的本地化文案(无图标/延迟);供「不可启用」与行内「测试」失败 toast 共用。
  function testReason(res: OmniTestResult): string {
    const k = res.code ? OMNI_CODE_KEY[res.code] : undefined;
    return k ? t(k) : res.message;
  }

  // 测试结果统一展示文案(✓/⚠/✗ + 本地化 + 延迟);表单底部展示测试结果用。
  function testResultText(res: OmniTestResult): string {
    const lat = res.latency_ms != null ? ` · ${res.latency_ms}ms` : "";
    return `${SEV_GLYPH[severityOf(res)]} ${testReason(res)}${lat}`;
  }

  // 表单内拉模型/测试错误的就近显示:解析当前编辑/命中条目 + 错误归属字段。
  const keyProfile = editTarget(editing) ?? existing;
  const errField = modelsErr ? errFieldOf(modelsErrCode) : null;
  const urlErrHere = errField === "url";
  const keyErrHere = errField === "key";
  const modelErrHere = errField === "model";

  return (
    <section
      className="rounded-xl bg-bg-secondary border border-border shadow-sm p-5 md:p-6"
      aria-labelledby="usage-omni-config-title"
    >
      <button
        type="button"
        onClick={() => setCollapsed((c) => !c)}
        className="w-full flex items-center justify-between gap-3 text-left"
        aria-expanded={!collapsed}
      >
        <span className="flex items-baseline gap-3 flex-wrap">
          <span id="usage-omni-config-title" className="text-section-title">
            {t("usage.configTitle")}
          </span>
          {collapsed && active && (
            <span className="text-caption text-text-secondary num">
              {t("usage.currentPrefix")}
              {hasKey ? `${active.model} · ${hostOf(active.base_url)}` : t("usage.noApiKeyConfigured")}
            </span>
          )}
        </span>
        {/* 与 CollapsibleCard 保持一致（这张卡的头早于共用组件、单独实现了一份） */}
        <span className="text-text-tertiary text-caption shrink-0 inline-flex items-center gap-1">
          {collapsed ? t("common.expand") : t("common.collapse")}
          {collapsed ? (
            <IconChevronDown width={16} height={16} />
          ) : (
            <IconChevronUp width={16} height={16} />
          )}
        </span>
      </button>

      {!collapsed && (
        <div className="mt-4">
          {loadErr ? (
            <div className="text-error text-center py-6">{loadErr}</div>
          ) : !state || !active ? (
            <div className="text-text-secondary text-center py-6">{t("usage.loading")}</div>
          ) : (
            <>
              {/* 未配 key 才给警告;当前生效在列表里用橙色行 + 「当前模型」标记,不再单开字段 */}
              {!hasKey && (
                <div className="text-caption text-warning bg-warning-bg rounded-lg px-3 py-2 mb-3">
                  {t("usage.noKeyWarning")}
                </div>
              )}

              {/* ── 模型列表 ── */}
              <div className="overflow-x-auto -mx-5 md:-mx-6">
                <table className="w-full text-caption whitespace-nowrap">
                  <thead>
                    <tr className="text-text-secondary border-b border-border">
                      <th className="text-left px-5 md:px-6 py-2">{t("usage.colModel")}</th>
                      <th className="text-left px-3 py-2">{t("usage.baseUrlLabel")}</th>
                      <th className="text-left px-3 py-2">{t("usage.colApiKey")}</th>
                      <th className="text-left px-5 md:px-6 py-2">{t("usage.colAction")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {profiles.length === 0 ? (
                      <tr>
                        <td
                          colSpan={4}
                          className="px-5 md:px-6 py-5 text-center text-text-tertiary"
                        >
                          {t("usage.emptyProfiles")}
                        </td>
                      </tr>
                    ) : (
                      profiles.map((p) => {
                        // active 行才有熔断器 health,非 active 行只看 last_verified 快照。
                        const health = p.active ? state.active.health : undefined;
                        const st = markerStateOf(p, health);
                        return (
                          <tr
                            key={p.label}
                            className={`border-b border-border last:border-b-0 ${
                              p.active ? "bg-brand-soft" : ""
                            }`}
                          >
                            <td className="px-5 md:px-6 py-2.5 num text-text-primary">
                              {/* 状态标记(形状+颜色)+ 模型名:鼠标移上去就弹气泡,内容见 tipLinesOf */}
                              <span
                                className="inline-flex items-center gap-1.5"
                                onMouseEnter={(e) => showTip(e, tipLinesOf(p, health, st))}
                                onMouseLeave={hideTip}
                              >
                                <span
                                  role="img"
                                  tabIndex={0}
                                  className={`inline-block w-3 text-center leading-none rounded-sm focus-visible:ring-2 focus-visible:ring-brand-primary focus-visible:outline-none ${MARKER_CLASS[st]}`}
                                  aria-label={tipLinesOf(p, health, st).join(t("usage.ariaSep"))}
                                  onFocus={(e) => showTip(e, tipLinesOf(p, health, st))}
                                  onBlur={hideTip}
                                  onKeyDown={(e) => {
                                    if (e.key === "Escape") hideTip();
                                  }}
                                >
                                  {MARKER_GLYPH[st]}
                                </span>
                                {p.model}
                              </span>
                              {p.active && (
                                <span className="ml-2 align-middle inline-block rounded px-1.5 py-0.5 bg-brand-primary text-white text-caption">
                                  {t("usage.activeTag")}
                                </span>
                              )}
                            </td>
                            <td className="px-3 py-2.5 num text-text-tertiary">{p.base_url}</td>
                            <td className="px-3 py-2.5 num text-text-tertiary">
                              {p.has_key ? p.api_key_masked : t("usage.notConfigured")}
                            </td>
                            <td className="px-5 md:px-6 py-2.5 text-left whitespace-nowrap">
                              <div className="inline-flex items-center gap-3 align-middle">
                                {p.active ? (
                                  <button
                                    type="button"
                                    onClick={() => onDeactivate(p)}
                                    disabled={deactivating === p.label}
                                    className="hover:bg-error-bg text-error border border-error rounded-md px-2.5 py-1 disabled:opacity-60"
                                  >
                                    {deactivating === p.label ? t("usage.deactivating") : t("usage.deactivate")}
                                  </button>
                                ) : (
                                  <button
                                    type="button"
                                    onClick={() => onActivate(p)}
                                    disabled={activating === p.label}
                                    className="hover:bg-brand-soft text-brand-primary border border-brand-primary rounded-md px-2.5 py-1 disabled:opacity-60"
                                  >
                                    {activating === p.label ? t("usage.testing") : t("usage.activate")}
                                  </button>
                                )}
                                <button
                                  type="button"
                                  onClick={() => onTestRow(p)}
                                  disabled={rowTesting === p.label}
                                  className="text-text-secondary hover:text-brand-primary disabled:opacity-60"
                                >
                                  {rowTesting === p.label ? t("usage.testing") : t("usage.test")}
                                </button>
                                <button
                                  type="button"
                                  onClick={() => startEdit(p)}
                                  className="text-text-secondary hover:text-brand-primary"
                                >
                                  {t("usage.edit")}
                                </button>
                                <button
                                  type="button"
                                  onClick={() => setDeleteTarget(p)}
                                  className="text-text-tertiary hover:text-error"
                                >
                                  {t("usage.delete")}
                                </button>
                              </div>
                            </td>
                          </tr>
                        );
                      })
                    )}
                  </tbody>
                </table>
              </div>

              {/* 新增按钮放列表下方(新增即追加到列表末尾) */}
              {!adding && (
                <button
                  type="button"
                  onClick={startAdd}
                  className="mt-3 text-caption text-text-secondary hover:text-brand-primary inline-flex items-center gap-1"
                >
                  <span className="text-lg leading-none">＋</span> {t("usage.addModel")}
                </button>
              )}

              {/* ── 新增表单 ── */}
              {adding && (
                <div className="mt-4 rounded-lg bg-bg-primary border border-border p-4 grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-4 items-start">
                  <div className="md:col-span-2 text-caption text-text-secondary">
                    {editing ? t("usage.editFormHint") : t("usage.addFormHint")}
                  </div>
                  <Field label={t("usage.baseUrlLabel")} className="md:col-span-2">
                    <input
                      value={baseUrl}
                      onChange={(e) => {
                        setBaseUrl(e.target.value);
                        setTestResult(null);
                      }}
                      onBlur={() => fetchModels(baseUrl, apiKey, editing)}
                      placeholder={t("usage.baseUrlPlaceholder")}
                      className={INPUT_CLS}
                    />
                    {urlErrHere && (
                      <span className="text-caption mt-1 block text-error">✗ {modelsMsg}</span>
                    )}
                  </Field>
                  <Field label={t("usage.apiKeyLabel")}>
                    <div className="relative">
                      <input
                        type={showKey ? "text" : "password"}
                        value={apiKey}
                        onChange={(e) => {
                          setApiKey(e.target.value);
                          setTestResult(null);
                        }}
                        onBlur={() => fetchModels(baseUrl, apiKey, editing)}
                        placeholder={
                          keyProfile?.has_key
                            ? t("usage.apiKeyPlaceholderExisting")
                            : t("usage.apiKeyPlaceholderNew")
                        }
                        autoComplete="off"
                        className={INPUT_CLS + " pr-10"}
                      />
                      {/* 明文/密文切换:密文态睁眼(点击显示),明文态闭眼(点击隐藏) */}
                      <button
                        type="button"
                        onClick={() => setShowKey((s) => !s)}
                        className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-text-tertiary hover:text-text-secondary"
                        aria-label={showKey ? t("usage.hideKey") : t("usage.revealKey")}
                      >
                        {showKey ? <IconEyeOff /> : <IconEye />}
                      </button>
                    </div>
                    {keyErrHere ? (
                      <span className="text-caption mt-1 block text-error">✗ {modelsMsg}</span>
                    ) : keyProfile?.has_key ? (
                      <span className="text-caption mt-1 block text-text-tertiary num">
                        {t("usage.apiKeyCurrentHint", { masked: keyProfile.api_key_masked })}
                      </span>
                    ) : null}
                  </Field>
                  <Field label={t("usage.modelLabel")}>
                    <ComboBox
                      value={model}
                      onChange={(v) => {
                        setModel(v);
                        setTestResult(null);
                      }}
                      options={models}
                      placeholder={
                        modelsLoading
                          ? t("usage.modelComboPlaceholderLoading")
                          : t("usage.modelComboPlaceholder")
                      }
                      ariaLabel={t("usage.modelLabel")}
                    />
                    <span
                      className={`text-caption mt-1 block ${
                        modelErrHere ? "text-error" : "text-text-tertiary"
                      }`}
                    >
                      {modelsLoading
                        ? t("usage.modelsFetching")
                        : modelErrHere
                          ? `✗ ${modelsMsg}`
                          : !modelsErr && modelsMsg
                            ? modelsMsg
                            : models.length
                              ? t("usage.modelsCount", { n: models.length })
                              : t("usage.modelsHint")}
                    </span>
                  </Field>
                  <div className="md:col-span-2 pt-1 flex items-center gap-3 flex-wrap">
                    <button
                      type="button"
                      onClick={onSave}
                      disabled={saving || testing}
                      className="text-caption px-3 py-1.5 rounded-lg bg-brand-primary text-white hover:opacity-90 disabled:opacity-60"
                    >
                      {saving ? t("usage.saving") : t("usage.save")}
                    </button>
                    <button
                      type="button"
                      onClick={onTest}
                      disabled={saving || testing}
                      className="text-caption px-3 py-1.5 rounded-lg bg-bg-secondary border border-border text-text-primary hover:border-brand-primary disabled:opacity-60"
                    >
                      {testing ? t("usage.testing") : t("usage.testConnection")}
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setAdding(false);
                        setEditing(null);
                        setTestResult(null);
                      }}
                      disabled={saving || testing}
                      className="text-caption px-3 py-1.5 rounded-lg text-text-tertiary hover:text-text-primary"
                    >
                      {t("usage.cancel")}
                    </button>
                    {testResult && (
                      <span className={`text-caption ${SEV_CLASS[severityOf(testResult)]}`}>
                        {testResultText(testResult)}
                      </span>
                    )}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      )}

      {/* 模型列状态标记的悬浮气泡:fixed 锚定元素,鼠标移上去瞬时出现、不被表格 overflow 裁剪;
          落点未算出前先摆到锚点下方并隐形,量完真实高度后再用 placePopover 定最终位置 */}
      {tipAnchor && (
        <div
          ref={tipRef}
          className="fixed z-[70] max-w-xs rounded-md bg-bg-secondary border border-border shadow-md px-2.5 py-1.5 text-caption text-text-primary pointer-events-none space-y-0.5"
          style={{
            left: tipPos ? tipPos.left : tipAnchor.rect.left,
            top: tipPos ? tipPos.top : tipAnchor.rect.bottom + 4,
            visibility: tipPos ? undefined : "hidden",
          }}
        >
          {tipAnchor.lines.map((line, i) => (
            <div key={i}>{line}</div>
          ))}
        </div>
      )}

      {/* 删除确认弹窗(web 风格,代替浏览器原生 confirm)。删当前生效项时追加红色警告:会停感知。 */}
      {deleteTarget && (
        <div
          className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40"
          onClick={() => {
            if (!deleting) setDeleteTarget(null);
          }}
        >
          <div
            role="dialog"
            aria-modal="true"
            className="w-[90%] max-w-md bg-bg-secondary border border-border rounded-2xl shadow-lg p-6 anim-in"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-start justify-between mb-3">
              <h2 className="text-title font-semibold text-text-primary">
                {t("usage.deleteDialogTitle")}
              </h2>
              <button
                type="button"
                onClick={() => setDeleteTarget(null)}
                disabled={deleting}
                aria-label={t("usage.cancel")}
                className="rounded-full p-1 text-text-secondary hover:text-text-primary disabled:opacity-50"
              >
                <IconX />
              </button>
            </div>
            <p className="text-body text-text-secondary">
              {t("usage.deleteConfirm", {
                model: deleteTarget.model,
                host: hostOf(deleteTarget.base_url),
              })}
            </p>
            {deleteTarget.active && (
              <p className="text-caption text-error bg-error-bg rounded-lg px-3 py-2 mt-3">
                {t("usage.deleteActiveWarning")}
              </p>
            )}
            <div className="mt-6 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setDeleteTarget(null)}
                disabled={deleting}
                className="text-body px-4 py-2 rounded-lg bg-bg-primary border border-border text-text-primary hover:border-border-strong disabled:opacity-60"
              >
                {t("usage.cancel")}
              </button>
              <button
                type="button"
                onClick={confirmDelete}
                disabled={deleting}
                className="text-body px-4 py-2 rounded-lg bg-error text-white hover:brightness-95 disabled:opacity-60 transition-[filter,opacity]"
              >
                {deleting ? t("usage.deleting") : t("usage.delete")}
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
