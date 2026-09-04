/**
 * 米家账号绑定向导（§5 居中 Dialog）。
 *
 * 流程跟 cli/.../commands/account.py 同源——backend `redirect_uri` 写死在
 * `https://mico.api.mijia.tech/login_redirect`（manager.py:64，小米 OAuth
 * whitelist 限制不能改非小米域名），webUI 拿不到回调 query，必须靠用户
 * 手动复制小米回调页给的 base64 payload 粘进来。
 *
 * 三步状态机：
 *   ① 调 POST /api/miot/bind 拿 oauth_url → window.open 新标签授权
 *   ② 用户从「授权成功」页复制 payload 粘到 textarea
 *   ③ 前端 base64 解码 → POST /api/miot/authorize {code, state}
 *
 * 视觉规格严格按 §5 Dialog（居中 max-w-md bg-bg-secondary border rounded-2xl
 * shadow-lg p-6） + §2 按钮族（Primary CTA / Secondary / 错误 inline warn caption）。
 */

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { authorizeMiot, bindMiot, listScopeHomes, switchScopeHome } from "@/api";
import { useEscClose } from "@/hooks/useEscClose";
import i18n from "@/i18n";
import { IconX } from "@/lib/icons";
import type { ScopeHome } from "@/lib/types";

interface Props {
  open: boolean;
  onClose: () => void;
  onDone: () => void;
}

type Step = "open" | "paste" | "select-home";

interface ParsedPayload {
  code: string;
  state: string;
}

// 跟 cli/.../account.py::_parse_auth_payload 同款：base64 → JSON → 取 code/state
// 导出供 vitest 测各种边界 case
export function parsePayload(raw: string): ParsedPayload | { error: string } {
  const trimmed = raw.trim();
  if (!trimmed) return { error: i18n.t("account.payloadEmpty") };
  let decoded: string;
  try {
    // cli/.../account.py::_parse_auth_payload 走 base64.urlsafe_b64decode 兼容
    // 标准 + URL-safe 两种 base64;前端 atob 只接受标准,把 -_ → +/ + 补 = padding
    // 兼容 URL-safe(典型 OAuth 实现)。fatal:true 让非法 UTF-8 走 catch。
    const normalized = trimmed.replace(/-/g, "+").replace(/_/g, "/");
    const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
    const bytes = Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
    decoded = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return { error: i18n.t("account.payloadNotBase64") };
  }
  let data: unknown;
  try {
    data = JSON.parse(decoded);
  } catch {
    return { error: i18n.t("account.payloadParseFail") };
  }
  if (
    !data ||
    typeof data !== "object" ||
    typeof (data as { code?: unknown }).code !== "string" ||
    typeof (data as { state?: unknown }).state !== "string"
  ) {
    return { error: i18n.t("account.payloadMissingFields") };
  }
  const { code, state } = data as ParsedPayload;
  if (!code.trim() || !state.trim()) {
    return { error: i18n.t("account.payloadEmptyFields") };
  }
  return { code: code.trim(), state: state.trim() };
}

export function MiotBindDialog({ open, onClose, onDone }: Props) {
  const { t } = useTranslation();
  const [step, setStep] = useState<Step>("open");
  const [oauthUrl, setOauthUrl] = useState<string | null>(null);
  const [opening, setOpening] = useState(false);
  const [payload, setPayload] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [homes, setHomes] = useState<ScopeHome[]>([]);
  const [selectedHomeId, setSelectedHomeId] = useState<string | null>(null);
  const [switching, setSwitching] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // submitting 期间不响应 ESC——authorizeMiot Promise 还在跑,dialog 提前关闭
  // 会让住户看不到成功 toast / onDone reload,跟同仓其它 dialog
  // (ConfirmUnbindDialog) 同口径。
  useEscClose(open && !submitting && !switching, () => {
    if (step === "select-home") { onDone(); onClose(); } else { onClose(); }
  });

  // open 切换时重置状态。useLayoutEffect 而非 useEffect:在浏览器 paint 之前
  // 同步 reset,避免住户上次关 dialog 时 step="paste" 残留,下次再开时第一帧
  // 闪一下旧 paste 表单才切回 open 步骤。
  useLayoutEffect(() => {
    if (!open) return;
    setStep("open");
    setOauthUrl(null);
    setOpening(false);
    setPayload("");
    setSubmitting(false);
    setError(null);
    setHomes([]);
    setSelectedHomeId(null);
    setSwitching(false);
  }, [open]);

  // 进 paste 步骤时 focus textarea
  useEffect(() => {
    if (step === "paste") textareaRef.current?.focus();
  }, [step]);

  if (!open) return null;

  const handleOpenAuthPage = async () => {
    setError(null);
    // 已经从 paste 步骤"上一步"返回的场景：复用首次 bindMiot() 拿到的 oauth_url，
    // **不重新 POST /api/miot/bind**——后者会让 backend session 生成新的 OAuth state，
    // 把之前住户复制好的 payload(state1) 跟新 state(state2) 拼不上，submit 时 backend
    // /api/miot/authorize 拒绝且只能看到笼统的"绑定失败"。
    let url = oauthUrl;
    if (!url) {
      setOpening(true);
      try {
        const resp = await bindMiot();
        url = resp.oauthUrl;
        setOauthUrl(url);
      } catch (e) {
        setError(e instanceof Error ? e.message : t("account.getOauthUrlFail"));
        setOpening(false);
        return;
      } finally {
        setOpening(false);
      }
    }
    // 用 anchor + 程序 click：HTML spec 规定 `window.open(url, _blank, noopener)`
    // **始终返回 null**（不管是否被拦截），用返回值判拦截会 100% 误报。anchor +
    // user gesture click 在主流浏览器（Chrome/Safari/Firefox/Edge）下不会被
    // popup blocker 拦掉，且 `rel=noopener noreferrer` 切断 opener。
    // 万一新标签真没出来（极端策略 / iframe 沙箱），paste 步骤的 fallback 链接
    // 「再打开一次授权页」给住户兜底，不弹误报"被拦截"。
    // append 到 body 再 click：游离 anchor 在部分浏览器（Safari 旧版）click 行为
    // 未定义；append + click + remove 是稳妥惯用做法。
    const a = document.createElement("a");
    a.href = url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.style.display = "none";
    document.body.append(a);
    try {
      a.click();
    } finally {
      a.remove();
    }
    setStep("paste");
  };

  const handleSubmit = async () => {
    setError(null);
    const parsed = parsePayload(payload);
    if ("error" in parsed) {
      setError(parsed.error);
      return;
    }
    setSubmitting(true);
    // 第一阶段：OAuth 绑定（不可逆，消费 code）
    try {
      await authorizeMiot(parsed.code, parsed.state);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("account.bindFail"));
      setSubmitting(false);
      return;
    }
    // 第二阶段：获取并启用家庭（绑定已成功，失败不应阻断用户）
    try {
      const homeList = await listScopeHomes();
      if (homeList.length === 1) {
        await switchScopeHome(homeList[0].homeId);
        onDone();
        onClose();
      } else if (homeList.length > 1) {
        setHomes(homeList);
        setSelectedHomeId(homeList[0].homeId);
        setStep("select-home");
      } else {
        onDone();
        onClose();
      }
    } catch {
      // 绑定成功但家庭选择失败 → 正常关闭，让用户从 HomeSwitcher 选择
      onDone();
      onClose();
    } finally {
      setSubmitting(false);
    }
  };

  const handleConfirmHome = async () => {
    if (!selectedHomeId) return;
    setSwitching(true);
    try {
      await switchScopeHome(selectedHomeId);
      onDone();
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : t("account.switchHomeFail"));
    } finally {
      setSwitching(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40"
      onClick={submitting || switching ? undefined : () => {
        if (step === "select-home") { onDone(); onClose(); } else { onClose(); }
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="miot-bind-title"
        className="w-[90%] max-w-md bg-bg-secondary border border-border rounded-2xl shadow-lg p-6 anim-in"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between mb-3">
          <h2
            id="miot-bind-title"
            className="text-title font-semibold text-text-primary"
          >
            {t("account.bindTitle")}
          </h2>
          <button
            type="button"
            onClick={() => {
              if (step === "select-home") { onDone(); onClose(); } else { onClose(); }
            }}
            disabled={submitting || switching}
            aria-label={t("account.close")}
            className="rounded-full p-1 text-text-secondary hover:text-text-primary disabled:opacity-50"
          >
            <IconX />
          </button>
        </div>

        {step === "open" ? (
          <>
            <p className="text-body text-text-secondary mb-1">
              {t("account.step1Title")}
            </p>
            <p className="text-caption text-text-tertiary mb-5">
              {t("account.step1Hint")}
            </p>
            {error && (
              <div className="text-caption text-warning mb-3">{error}</div>
            )}
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={onClose}
                className="text-body px-4 py-2 rounded-lg bg-bg-primary border border-border text-text-primary hover:border-border-strong"
              >
                {t("account.cancel")}
              </button>
              <button
                type="button"
                onClick={handleOpenAuthPage}
                disabled={opening}
                className="text-body px-4 py-2 rounded-lg bg-brand-primary text-white hover:bg-brand-accent disabled:opacity-60 disabled:cursor-not-allowed"
              >
                {opening ? t("account.gettingOauthUrl") : t("account.openAuthPage")}
              </button>
            </div>
          </>
        ) : step === "paste" ? (
          <>
            <p className="text-body text-text-secondary mb-1">
              {t("account.step2Title")}
            </p>
            <p className="text-caption text-text-tertiary mb-3">
              {t("account.step2Hint")}
            </p>
            <textarea
              ref={textareaRef}
              value={payload}
              onChange={(e) => setPayload(e.target.value)}
              placeholder={t("account.payloadPlaceholder")}
              rows={4}
              className="w-full px-3 py-2 rounded-lg bg-bg-primary border border-border focus:border-brand-primary focus:outline-none text-body text-text-primary resize-none mb-2"
            />
            {error && (
              <div className="text-caption text-warning mb-2">{error}</div>
            )}
            {oauthUrl && (
              <div className="text-caption text-text-tertiary mb-3">
                {t("account.noNewTab")}{" "}
                <a
                  href={oauthUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-brand-primary hover:underline"
                >
                  {t("account.reopenAuthPage")}
                </a>
              </div>
            )}
            <div className="flex items-center gap-2 justify-end">
              <button
                type="button"
                onClick={() => setStep("open")}
                className="text-body px-4 py-2 rounded-lg bg-bg-primary border border-border text-text-primary hover:border-border-strong"
              >
                {t("account.prevStep")}
              </button>
              <button
                type="button"
                onClick={handleSubmit}
                disabled={submitting || !payload.trim()}
                className="text-body px-4 py-2 rounded-lg bg-brand-primary text-white hover:bg-brand-accent disabled:opacity-60 disabled:cursor-not-allowed"
              >
                {submitting ? t("account.binding") : t("account.finishBind")}
              </button>
            </div>
          </>
        ) : step === "select-home" ? (
          <>
            <p className="text-body text-text-secondary mb-1">
              {t("account.step3Title")}
            </p>
            <p className="text-caption text-text-tertiary mb-3">
              {t("account.step3Hint")}
            </p>
            {error && (
              <div className="text-caption text-warning mb-2">{error}</div>
            )}
            <div className="flex flex-col gap-2 mb-4">
              {homes.map((h) => (
                <label
                  key={h.homeId}
                  className={`flex items-center gap-3 px-3 py-2 rounded-lg border cursor-pointer transition-colors ${
                    selectedHomeId === h.homeId
                      ? "border-brand-primary bg-brand-soft"
                      : "border-border hover:border-border-strong"
                  }`}
                >
                  <input
                    type="radio"
                    name="home"
                    value={h.homeId}
                    checked={selectedHomeId === h.homeId}
                    onChange={() => setSelectedHomeId(h.homeId)}
                    className="accent-brand-primary"
                  />
                  <span className="text-body text-text-primary">
                    {h.homeName}
                  </span>
                </label>
              ))}
            </div>
            <div className="flex justify-end">
              <button
                type="button"
                onClick={handleConfirmHome}
                disabled={switching || !selectedHomeId}
                className="text-body px-4 py-2 rounded-lg bg-brand-primary text-white hover:bg-brand-accent disabled:opacity-60 disabled:cursor-not-allowed"
              >
                {switching ? t("account.switching") : t("account.confirmSelect")}
              </button>
            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}
