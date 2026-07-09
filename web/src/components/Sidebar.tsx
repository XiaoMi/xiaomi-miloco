/**
 * 左侧 Tab 侧栏(v3 Mi Console 视觉,2026-05 v5 精简)
 *
 * 视觉规格:
 * - 顶部 Miloco 品牌行——只有 M logo + 字样,高度对齐主区 TopBar(64px)
 * - 5 个 nav tab(概览/设备/家庭/日志/模型):name 16px / hint 12px / active 左侧 2px 橙色竖条
 * - 底部 MiotAccountButton（米家账号头像 + 状态点 + popover）—— v5 替代了原"设置"齿轮，
 *   设置抽屉已删；popover 向上展开（原 TopBar 头像是向下）
 *
 * 家庭切换器和时间已搬走/移除:
 *   - HomeSwitcher 移到主区 TopBar(替换原 homeName 标题)
 *   - 时间/weekday 信息删除(原本是 dev tool 风提示,实际信息冗余)
 *
 * mobile 下由 App 切换为底部 nav,本组件只渲染桌面形态。
 */

import { useRef } from "react";
import type { ComponentType, SVGProps } from "react";
import { useTranslation } from "react-i18next";
import type { HomeStatus } from "@/lib/types";
import { MiotAccountButton } from "./MiotAccountButton";
import {
  IconNow,
  IconDevices,
  IconFamily,
  IconTasks,
  IconActivity,
  IconUsage,
} from "@/lib/navIcons";

export type TabKey =
  | "now"
  | "devices"
  | "family"
  | "tasks"
  | "activity"
  | "usage";

type NavIcon = ComponentType<SVGProps<SVGSVGElement> & { active?: boolean }>;

export interface TabDef {
  key: TabKey;
  /** i18n key —— 渲染时用 t() 翻；不存字面量,避免模块级常量绑死语言。 */
  labelKey: string;
  hintKey: string;
  Icon: NavIcon;
}

// label / hint 对齐 product owner @lichao61 维护的飞书 wiki §一
// （Family UI 模块职责与边界）—— 改 label / hint 前**先核对 wiki 现状**，
// 不然这条注释半年后又会跟现实分叉。wiki URL / token 跟 PO 拿。
export const TABS: TabDef[] = [
  {
    key: "now",
    labelKey: "nav.home",
    hintKey: "nav.homeHint",
    Icon: IconNow,
  },
  {
    key: "devices",
    labelKey: "nav.devices",
    hintKey: "nav.devicesHint",
    Icon: IconDevices,
  },
  {
    key: "family",
    labelKey: "nav.family",
    hintKey: "nav.familyHint",
    Icon: IconFamily,
  },
  {
    key: "tasks",
    labelKey: "nav.tasks",
    hintKey: "nav.tasksHint",
    Icon: IconTasks,
  },
  {
    key: "activity",
    labelKey: "nav.activity",
    hintKey: "nav.activityHint",
    Icon: IconActivity,
  },
  {
    key: "usage",
    labelKey: "nav.usage",
    hintKey: "nav.usageHint",
    Icon: IconUsage,
  },
  // 性能 tab 不再列入主导航 — 改为通过 URL hash "#perf" 进入独立调试视图,
  // 普通用户看不到入口。详见 App.tsx 的 PerfView。
];

interface Props {
  active: TabKey;
  onChange: (key: TabKey) => void;
  miot?: HomeStatus["miot"];
  onOpenMiotBind: () => void;
  onMiotChanged: () => void;
}

export function Sidebar({ active, onChange, miot, onOpenMiotBind, onMiotChanged }: Props) {
  const { t } = useTranslation();
  // 整个底部账号 row 当 hit area:onClick 转发给内部 MiotAccountButton 的
  // button DOM 触发同款交互(已绑 toggle popover / 未绑 onBind)。住户不用瞄准
  // 32x32 头像那一小点,点 row 任意位置都行。wrapperRef 传给 MiotAccountButton
  // 让它把"点外面关 menu"的 contains 判断扩到整 row,避免点文字栏 mousedown
  // 被误判"点外面"导致 menu 闪关再开。
  const accountBtnRef = useRef<HTMLButtonElement>(null);
  const accountRowRef = useRef<HTMLDivElement>(null);
  return (
    <aside
      className="hidden md:flex flex-col shrink-0 border-r border-border bg-bg-secondary w-[15%] min-w-[172px]"
    >
      {/* 品牌区:M logo + Miloco,高度跟主区 TopBar 一致(64px)*/}
      <header
        className="flex items-center gap-2.5 shrink-0"
        style={{ minHeight: 64, paddingLeft: 18, paddingRight: 18 }}
      >
        <svg
          className="shrink-0"
          width="28"
          height="28"
          viewBox="0 0 239 239"
          xmlns="http://www.w3.org/2000/svg"
          aria-hidden
        >
          <path
            d="M214.029 24.969C191.478 2.512 159.075 0 119.464 0 79.853 0 47.349 2.543 24.814 25.055 2.278 47.566 0 79.969 0 119.588s2.286 72.038 24.821 94.55c22.535 22.511 54.993 24.797 94.643 24.797s72.084-2.27 94.635-24.79c22.55-22.519 24.829-54.938 24.829-94.549 0-39.611-2.279-72.076-24.899-94.627Z"
            fill="#FF6900"
          />
          <path
            d="M110.677 163.756a1.49 1.49 0 0 1-1.493 1.462H88.811a1.494 1.494 0 0 1-1.517-1.462V110.506a1.494 1.494 0 0 1 1.517-1.47h20.373a1.49 1.49 0 0 1 1.493 1.47v53.25Z"
            fill="#fff"
          />
          <path
            d="M150.568 163.756a1.491 1.491 0 0 1-1.501 1.462h-19.401a1.494 1.494 0 0 1-1.509-1.462V117.776c0-8.024-.474-16.275-4.619-20.42-3.561-3.569-10.21-4.393-17.107-4.564H71.26a1.5 1.5 0 0 0-1.501 1.47v69.494a1.494 1.494 0 0 1-1.517 1.462H48.803a1.494 1.494 0 0 1-1.501-1.462V75.21a1.5 1.5 0 0 1 1.501-1.47h55.987c14.627 0 29.93.669 37.473 8.22 7.543 7.55 8.258 22.862 8.258 37.512l.047 44.284Z"
            fill="#fff"
          />
          <path
            d="M190.521 163.756a1.494 1.494 0 0 1-1.508 1.462h-19.401a1.494 1.494 0 0 1-1.501-1.462V75.21a1.5 1.5 0 0 1 1.501-1.47h19.401a1.494 1.494 0 0 1 1.508 1.47v88.546Z"
            fill="#fff"
          />
        </svg>
        <span className="text-title text-text-primary">Miloco</span>
        {/* 构建版本号（vite define 注入）：生产为 vYYYY.M.D，dev 为 git describe */}
        <span className="text-caption text-text-tertiary self-end mb-1 truncate" title={__APP_VERSION__}>
          {__APP_VERSION__}
        </span>
      </header>

      {/* Tab 列表 */}
      <nav className="flex-1 px-2 py-2.5 space-y-0.5 overflow-y-auto" aria-label={t("nav.aria")}>
        {TABS.map((tab) => {
          const on = active === tab.key;
          const Icon = tab.Icon;
          return (
            <button
              key={tab.key}
              type="button"
              aria-label={`${t(tab.labelKey)} · ${t(tab.hintKey)}`}
              aria-current={on ? "true" : undefined}
              onClick={() => onChange(tab.key)}
              className={`w-full flex items-center rounded-md transition-colors text-left gap-3 px-3 py-2.5 ${
                on
                  ? "bg-brand-soft text-text-primary"
                  : "text-text-primary hover:bg-brand-soft hover:text-text-primary"
              }`}
            >
              {/* 图标:激活时 brand-primary 橙 + 填充态,默认 inherit currentColor + 描边态 */}
              <span
                className={`shrink-0 ${on ? "text-brand-primary" : ""}`}
              >
                <Icon active={on} width={24} height={24} />
              </span>
              <span className="flex flex-col leading-tight">
                <span className="text-title">{t(tab.labelKey)}</span>
                {/* 副标题用 tertiary 灰阶,自动适配 light/dark */}
                <span
                  className="text-caption text-text-tertiary"
                >
                  {t(tab.hintKey)}
                </span>
              </span>
            </button>
          );
        })}
      </nav>

      {/* 底部米家账号 —— popover 向上展开避免被屏幕底裁。
          status.data 加载失败时 miot=undefined，这里用 fallback {bound:false} 兜底
          让住户至少看到"未绑定"黄状态点 + 点击仍能调 onOpenMiotBind 触发绑定流程；
          否则 sidebar 底部空白 + TopBar HomeSwitcher 也没显，整页等于无入口。
          头像 + 账号文字双区:已绑显 accountName / uid 让住户一眼知道当前
          身份;未绑显"点击登录米家"提示 + 整行可点(头像 + 文字 hit area 都
          指向 onOpenMiotBind)。文字字号严格按 design-tokens §3:账号名
          text-body(14, 主信息),uid text-caption-mono(12, mono 元数据),
          未登录提示 text-caption(12, hint)。 */}
      <div
        ref={accountRowRef}
        className="px-3 py-3 border-t border-border flex items-center gap-2 cursor-pointer hover:bg-bg-tertiary transition-colors"
        onClick={(e) => {
          // target 在 button 内时(点头像或 .click() 派的合成 click bubble 到这里)
          // 不重复转发 — 让 button 自己 onClick 处理。其它点击(文字栏 / padding
          // 空白) → ref.click() 把交互转到头像 button,跟点头像同款。
          // a11y 走头像内嵌 button 自己:键盘 Tab 聚焦头像 button → Enter/Space
          // 已能触发 onBind/popover。div 不接 keydown(没 tabIndex 永远不聚焦,
          // 加 keydown 是死代码),也不加 role=button(嵌套交互元素违反 WAI-ARIA)。
          // 同时排除 popover 内点击 — popover 在 accountRowRef 子树内,点 popover
          // padding 空白不该 fall through 到 row 转发把 popover 关掉。
          const t = e.target as HTMLElement;
          if (t.closest("button") || t.closest('[role="menu"]')) return;
          accountBtnRef.current?.click();
        }}
      >
        <MiotAccountButton
          ref={accountBtnRef}
          anchorWrapperRef={accountRowRef}
          miot={
            miot ?? {
              bound: false,
              devicesCount: 0,
              roomsCount: 0,
            }
          }
          onBind={onOpenMiotBind}
          onChanged={onMiotChanged}
          popoverPlacement="top"
        />
        {/* 文字栏不再嵌套 button(避免嵌套交互元素)。a11y 走头像本身的 button:
            屏幕阅读器 / 键盘用户用 Tab 聚焦头像 button,Enter/Space 触发原本逻辑;
            外层 div 只是视觉 hit area 扩大,鼠标点哪都通过 ref 转发到头像 button。 */}
        <div className="min-w-0 flex-1">
          {miot?.bound ? (
            <>
              <div className="text-body text-text-primary truncate">
                {miot.accountName ?? t("nav.bound")}
              </div>
              {miot.userUid && (
                <div className="text-caption-mono text-text-tertiary truncate">
                  uid {miot.userUid}
                </div>
              )}
            </>
          ) : (
            <div className="text-caption text-text-tertiary truncate">
              {t("nav.loginMiot")}
            </div>
          )}
        </div>
      </div>
    </aside>
  );
}

/** mobile 底部横向 tab bar */
export function MobileTabBar({
  active,
  onChange,
  miot,
  onOpenMiotBind,
  onMiotChanged,
}: {
  active: TabKey;
  onChange: (key: TabKey) => void;
  /** 米家账号信息——尾部加 MiotAccountButton 让 mobile 也能解绑/重绑。 */
  miot?: HomeStatus["miot"];
  onOpenMiotBind?: () => void;
  onMiotChanged?: () => void;
}) {
  const { t } = useTranslation();
  return (
    <nav
      aria-label={t("nav.aria")}
      className="md:hidden flex border-t border-border bg-bg-secondary overflow-x-auto items-center"
      style={{
        // h-[72px] 旧值;改成 min-h 让 iOS Safari 跟 PWA 下 home indicator
        // (≈34pt safe-area-inset-bottom) 不会盖掉 tab 文字。pb 走 env() 兜底,
        // 不支持的浏览器照旧 0 padding。
        minHeight: 72,
        paddingBottom: "env(safe-area-inset-bottom, 0)",
      }}
    >
      {TABS.map((tab) => {
        const on = active === tab.key;
        const Icon = tab.Icon;
        return (
          <button
            key={tab.key}
            type="button"
            aria-current={on ? "true" : undefined}
            aria-label={t(tab.labelKey)}
            onClick={() => onChange(tab.key)}
            className={`flex-1 min-w-[60px] flex flex-col items-center justify-center py-2 gap-0.5 transition-colors ${
              on ? "text-brand-primary" : "text-text-secondary"
            }`}
          >
            <Icon active={on} width={24} height={24} />
            <span className="text-caption">{t(tab.labelKey)}</span>
          </button>
        );
      })}
      {onOpenMiotBind && onMiotChanged && (
        <div className="px-3 shrink-0">
          <MiotAccountButton
            miot={miot ?? { bound: false, devicesCount: 0, roomsCount: 0 }}
            onBind={onOpenMiotBind}
            onChanged={onMiotChanged}
            popoverPlacement="top"
          />
        </div>
      )}
    </nav>
  );
}
