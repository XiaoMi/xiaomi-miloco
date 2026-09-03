/**
 * 米家账号 icon button —— Sidebar 底部账号入口（v3 Mi Console 视觉）。
 *
 * 未绑：圆形灰底 + "?" 占位 + warning 黄状态点 → 点击直接打开 MiotBindDialog
 * 已绑：圆形真头像（user_info.icon）+ success 绿状态点 → 点击弹 AccountMenu
 *       popover（重新绑定 / 解绑），解绑走 §5 二次确认
 *
 * popoverPlacement 默认 "bottom"（向下展开，预留给将来非 Sidebar 调用方用）；
 * 当前唯一调用方 Sidebar.tsx 传 "top"（按钮在 sidebar 底部，向上弹避免溢出屏外）。
 *
 * 视觉规格严格按：
 *   §2.6 Icon Button：32×32 + rounded-full（不是 rounded-md，因为头像约定圆形）
 *   §3 Status Dot：5px + 3px 光环，绝对定位右下角
 *   §5 Dialog：解绑二次确认
 *   配色全 var(--color-*)，0 hex 内联
 */

import { unbindMiot } from "@/api";
import { useEscClose } from "@/hooks/useEscClose";
import { IconX } from "@/lib/icons";
import type { HomeStatus } from "@/lib/types";
import { forwardRef, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { miotTone } from "@/lib/miotTone";
import { toast } from "./Toast";

interface Props {
  miot: HomeStatus["miot"];
  onBind: () => void;
  onChanged: () => void;
  /** popover 展开方向：默认 "bottom"（向下展开）；"top"（向上展开，当前 Sidebar 底部用,
   *  避免被屏幕底裁掉） */
  popoverPlacement?: "top" | "bottom";
  /** 调用方把整个 hit area(比如 Sidebar 整 row)的 ref 传进来,扩展"点外面关 menu"
   *  的 contains 判断 — 否则在 hit area 内的非 button 区域 mousedown 会被误判为
   *  "点外面"导致 menu 闪关再开。 */
  anchorWrapperRef?: React.RefObject<HTMLElement | null>;
}

// forwardRef:Sidebar 把整行做成可点 hit area,onClick 内部调
// `accountBtnRef.current?.click()` 转发到这里的 button,触发跟原本"点头像"
// 相同的交互(已绑 toggle popover / 未绑 onBind)。
export const MiotAccountButton = forwardRef<HTMLButtonElement, Props>(
  function MiotAccountButton(
    { miot, onBind, onChanged, popoverPlacement = "bottom", anchorWrapperRef },
    forwardedBtnRef,
  ) {
    const { t } = useTranslation();
    const [menuOpen, setMenuOpen] = useState(false);
    const [confirmUnbind, setConfirmUnbind] = useState(false);
    const [iconError, setIconError] = useState(false);
    const wrapRef = useRef<HTMLDivElement>(null);

    // 点外面收起 menu。用 mousedown(早于 click)+ 双 ref 判断:
    //   · 内部 wrapRef:本组件最外层,默认 hit area
    //   · anchorWrapperRef:调用方传进来的更大 hit area(比如 Sidebar 整 row)
    // 任一 contains target 都不算"点外面"。这样 Sidebar 把整行做成可点时,点文字栏
    // 的 mousedown 不会被误判误关,click 阶段 row onClick 调 ref.click() toggle 才
    // 是用户预期的交互。click listener 不行:它会跟"用户原始 click 继续 bubble 到
    // document"撞车导致 effect 挂上的 listener 立刻被同一帧的 click 触发。
    useEffect(() => {
      if (!menuOpen) return;
      const onDoc = (e: MouseEvent) => {
        const t = e.target as Node;
        if (wrapRef.current?.contains(t)) return;
        if (anchorWrapperRef?.current?.contains(t)) return;
        setMenuOpen(false);
      };
      document.addEventListener("mousedown", onDoc);
      return () => document.removeEventListener("mousedown", onDoc);
    }, [menuOpen, anchorWrapperRef]);

    // ESC 关闭 popover——跟同 PR 其它弹层（ConfirmStopWatch / ConfirmDelete /
    // ConfirmDiscard / ConfirmUnbind / MiotBindDialog）的 a11y 行为对齐。
    useEscClose(menuOpen, () => setMenuOpen(false));

    // 重新绑米家账号(uid 换)时重置 iconError;不用 userIcon 当 deps —— 米家 CDN
    // 偶尔同头像带 query timestamp(?t=...),userIcon 字符串变但实质是同一头像,
    // 用 userUid(同账号期间稳定)更准。
    useEffect(() => {
      setIconError(false);
    }, [miot.userUid]);

    const handleClick = (e: React.MouseEvent) => {
      // stopPropagation 防回环:Sidebar 外层 div 把 click hit area 扩到整 row,
      // 它的 onClick 会调 accountBtnRef.click() 触发本 button 的 onClick;若不阻止
      // 这次 click 冒泡回外层 div,外层 onClick 又调 .click() → 死循环。
      e.stopPropagation();
      if (miot.bound) setMenuOpen((v) => !v);
      else onBind();
    };

    // 授权失效比「未绑」更需要住户注意，走 error 档。判据与状态条共用一份，
    // 避免两处各写一遍再分头退化——其中一处曾把它门控在 bound 上。
    const authDegraded = miotTone(miot) === "degraded";
    const dotToneClass = authDegraded
      ? "bg-error"
      : miot.bound
        ? "bg-success"
        : "bg-warning";
    const dotRingVar = authDegraded
      ? "--color-error-bg"
      : miot.bound
        ? "--color-success-bg"
        : "--color-warning-bg";

    return (
      <div ref={wrapRef} className="relative shrink-0">
        <button
          ref={forwardedBtnRef}
          type="button"
          onClick={handleClick}
          aria-haspopup={miot.bound ? "menu" : "dialog"}
          aria-expanded={miot.bound ? menuOpen : undefined}
          aria-label={
            miot.bound
              ? t("account.ariaBound", {
                  name: miot.accountName ?? t("account.ariaBoundFallback"),
                })
              : t("account.ariaUnbound")
          }
          title={
            miot.bound
              ? (miot.accountName ?? t("account.ariaBoundFallback"))
              : t("account.titleUnbound")
          }
          className="relative inline-flex items-center justify-center rounded-full bg-bg-tertiary hover:bg-bg-elevated transition-colors focus-visible:ring-2 focus-visible:ring-brand-primary focus-visible:outline-none overflow-hidden"
          style={{ width: 32, height: 32 }}
        >
          {miot.bound && miot.userIcon && !iconError ? (
            // 真头像（CDN 不可达时回退到下面占位 "米"，避免按钮空白）。
            // referrerPolicy=no-referrer：浏览器默认会带 Referer: http://<lan-ip>:1810/
            // 给小米 CDN，等于把住户 backend 部署 IP/端口暴露给小米服务器。隐私层面
            // 属"轻微数据泄露"，加这条头干净止血。
            <img
              src={miot.userIcon}
              alt=""
              referrerPolicy="no-referrer"
              className="w-full h-full object-cover"
              onError={() => setIconError(true)}
            />
          ) : (
            <span aria-hidden className="text-caption text-text-tertiary">
              {/* 已绑但 CDN 不可达 → 用 accountName 首字(更贴真实绑定语义),没
                 nickname 退到 "米"。未绑显 "?"。 */}
              {miot.bound ? (miot.accountName?.[0] ?? "米") : "?"}
            </span>
          )}
          {/* 右下角状态点（§3 5px + 3px 光环）—— 3 态。
              授权失效时 bound 仍是 true，只看 bound 的话这里会显示绿点，
              等于在说一切正常；这是住户在侧栏能察觉异常的唯一信号。 */}
          <span
            aria-hidden
            className={`absolute rounded-full ${dotToneClass}`}
            style={{
              width: 5,
              height: 5,
              right: 0,
              bottom: 0,
              boxShadow: `0 0 0 3px var(--color-bg-secondary), 0 0 0 4px var(${dotRingVar})`,
            }}
          />
        </button>

        {/* AccountMenu popover —— 已绑且打开时显示 */}
        {menuOpen && miot.bound && (
          <div
            role="menu"
            className={`absolute left-0 z-20 min-w-[220px] rounded-lg bg-bg-secondary border border-border shadow-md overflow-hidden ${
              popoverPlacement === "top" ? "bottom-full mb-1" : "top-full mt-1"
            }`}
          >
            <div className="px-3 py-2 border-b border-border">
              <div className="text-body text-text-primary truncate">
                {miot.accountName ?? t("account.menuAccountFallback")}
              </div>
              {miot.userUid && (
                <div className="text-caption-mono text-text-tertiary truncate">
                  uid {miot.userUid}
                </div>
              )}
            </div>
            {/* 授权失效时把状态挂在「重新绑定」这一项上——住户点开菜单本来就是
                为了找解决办法，状态与解法同处一行，不用自己把两件事连起来。
                账号行的 uid 保持原样：uid 是账号身份的常驻展示位，不该被会来回
                变的临时状态占用。 */}
            <button
              type="button"
              onClick={() => {
                setMenuOpen(false);
                onBind();
              }}
              className={`block w-full text-left text-body px-3 py-2 text-text-primary hover:bg-bg-tertiary transition-colors ${
                authDegraded ? "flex items-start gap-2" : ""
              }`}
            >
              {authDegraded ? (
                <>
                  <span
                    aria-hidden
                    className="shrink-0 rounded-full bg-error"
                    style={{
                      width: 6,
                      height: 6,
                      marginTop: 7,
                      boxShadow: "0 0 0 2px var(--color-error-bg)",
                    }}
                  />
                  <span className="min-w-0">
                    <span className="block">{t("account.rebind")}</span>
                    <span className="block text-caption text-text-tertiary">
                      {t("account.authDegradedReason")}
                    </span>
                  </span>
                </>
              ) : (
                t("account.rebind")
              )}
            </button>
            <button
              type="button"
              onClick={() => {
                setMenuOpen(false);
                setConfirmUnbind(true);
              }}
              className="block w-full text-left text-body px-3 py-2 text-error hover:bg-error-bg transition-colors"
            >
              {t("account.unbind")}
            </button>
          </div>
        )}

        {confirmUnbind && (
          // ConfirmUnbindDialog mounted 时 stopPropagation 防 sidebar 外层 onClick
          // 误判用户点对话框背景灰区 = 点 sidebar row(背景灰 div 是 portal 不是
          // sidebar row 的子元素,实际不会冒泡到 sidebar,但保险起见)。
          <ConfirmUnbindDialog
            accountName={miot.accountName}
            onCancel={() => setConfirmUnbind(false)}
            onConfirm={async () => {
              try {
                await unbindMiot();
                // onChanged → App.tsx::onMiotChanged → window.location.reload()
                // 立即 unmount ToastHost,直接 toast() 反馈被吞。跟 MiotBindDialog
                // onDone 同口径走 sessionStorage cross-reload 通道。
                try {
                  sessionStorage.setItem(
                    "miloco_pending_toast",
                    JSON.stringify({
                      text: t("account.toastUnbound"),
                      tone: "ok",
                    }),
                  );
                } catch {
                  /* sessionStorage 不可用降级 */
                }
                onChanged();
              } catch (e) {
                toast(
                  e instanceof Error ? e.message : t("account.unbindFail"),
                  "warn",
                );
              } finally {
                setConfirmUnbind(false);
              }
            }}
          />
        )}
      </div>
    );
  },
);

// 解绑二次确认（§5 居中弹窗 + §2.4 Danger Solid 红色解绑按钮）。
// onConfirm 是 async：按钮在等期间 disabled + 文案"正在解绑…"，避免用户感到点了无反应。
function ConfirmUnbindDialog({
  accountName,
  onCancel,
  onConfirm,
}: {
  accountName?: string;
  onCancel: () => void;
  onConfirm: () => void | Promise<void>;
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  // mounted ref 守 finally 里 setBusy(false)：onConfirm 通常会让父组件
  // setConfirmUnbind(false) → 本组件卸载，再回到 finally 是 unmounted 状态写。
  // React 18 不再警告但仍是无效写入；ref 守一下更稳。
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);
  useEscClose(!busy, onCancel);
  const handleConfirm = async () => {
    setBusy(true);
    try {
      await onConfirm();
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  };
  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40"
      onClick={(e) => {
        // stopPropagation:dialog 不走 portal,挂在 MiotAccountButton wrapRef 子树
        // 内,backdrop click 默认会冒泡到 sidebar accountRow.onClick 触发 ref.click()
        // 反向把 popover 弹开。stop 了不影响内层 dialog onClick(stopPropagation)。
        e.stopPropagation();
        if (!busy) onCancel();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-unbind-title"
        className="w-[90%] max-w-md bg-bg-secondary border border-border rounded-2xl shadow-lg p-6 anim-in"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-start justify-between mb-3">
          <h2
            id="confirm-unbind-title"
            className="text-title font-semibold text-text-primary"
          >
            {t("account.confirmUnbindTitle")}
          </h2>
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            aria-label={t("account.close")}
            className="rounded-full p-1 text-text-secondary hover:text-text-primary disabled:opacity-50"
          >
            <IconX />
          </button>
        </div>
        <p className="text-body text-text-secondary">
          {t("account.confirmUnbindBodyPrefix")}
          {accountName
            ? t("account.confirmUnbindBodyName", { name: accountName })
            : ""}
          {t("account.confirmUnbindBodySuffix")}
        </p>
        <div className="mt-6 flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="text-body px-4 py-2 rounded-lg bg-bg-primary border border-border text-text-primary hover:border-border-strong disabled:opacity-60 disabled:cursor-not-allowed"
          >
            {t("account.cancel")}
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={busy}
            className="text-body px-4 py-2 rounded-lg bg-error text-white hover:bg-error/90 disabled:opacity-60 disabled:cursor-not-allowed"
          >
            {busy ? t("account.unbinding") : t("account.unbind")}
          </button>
        </div>
      </div>
    </div>
  );
}
