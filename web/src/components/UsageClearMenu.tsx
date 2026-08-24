/**
 * 「清空数据」入口：垃圾桶图标 → 气泡里选清理范围 → 交给确认弹窗。
 *
 * 为什么不是一个写着「清空数据」的文字按钮（原做法）：它当时紧贴「刷新」，
 * 间距 12px、视觉重量完全相同——一个安全高频、一个不可逆，误触代价不对称，
 * 而且没有任何「这一步不可逆」的暗示。改成无框图标后两者不再是同一类东西；
 * 多出来的一次展开也把「手滑就点到」变成「要先找到再点」。
 *
 * 顺带补了一个原先没有的能力：**按时间范围清**。后端此前只有全清。
 *
 * 同一个组件带两种作用域：工具条上那个清所有模型，明细每行那个只清该行的
 * 「模型名 + endpoint」。同类动作用同一个图标、同一套范围档位，只差作用域——
 * 差异全部写在气泡标题、作用域徽记与确认窗里。
 */

import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useTranslation } from "react-i18next";
import { IconTrash } from "@/lib/icons";

/** 一次清除的完整作用域：时间范围 × 目标。 */
export interface ClearScope {
  key: "24h" | "7d" | "all";
  /** 这一档的可读名，确认窗里要复述它。 */
  label: string;
  /** 该时刻及其之后；null = 不限时间。 */
  sinceMs: number | null;
  /**
   * 限定到某一个「模型名 + endpoint」；省略 = 所有模型。
   * base_url 允许是空串——那是 schema v3 之前的老数据（来源未记录），
   * 是个有意义的目标，不是「未指定」。
   */
  target?: { model: string; baseUrl: string };
}

export function UsageClearMenu({
  onPick,
  target,
  placement = "absolute",
}: {
  onPick: (s: ClearScope) => void;
  /** 给了就是「只清这一项」，菜单标题会带上它；不给就是「所有模型」。 */
  target?: { model: string; baseUrl: string; label: ReactNode };
  /**
   * 浮层定位方式。表格里的那个必须用 fixed：明细表在 overflow-x:auto 容器内，
   * 而该属性会让纵向溢出也变成滚动——绝对定位浮层实测会超出容器并催出一条
   * 竖滚动条（锚在末行时超出 82px）。工具条不在任何 overflow 容器里，用 absolute 即可。
   */
  placement?: "absolute" | "fixed";
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  /** fixed 定位的锚点（按钮的视口坐标），开菜单那一刻记下。 */
  const [anchor, setAnchor] = useState<DOMRect | null>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const menuId = useId();

  // 点外面 / Esc 关闭。Esc 同时把焦点还给触发按钮，否则键盘用户会掉到 body。
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        wrapRef.current?.querySelector("button")?.focus();
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    // fixed 定位在滚动后会失锚（页面滚了、浮层不动）→ 直接关掉，不做跟随
    const onScroll = () => setOpen(false);
    if (placement === "fixed") {
      window.addEventListener("scroll", onScroll, true);
      window.addEventListener("resize", onScroll);
    }
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
      window.removeEventListener("resize", onScroll);
    };
  }, [open, placement]);

  /**
   * fixed 浮层的落点：**量出菜单真实尺寸后**再算，不用估值。
   * 默认贴按钮下方；下方装不下就翻到上方（明细表常常就在视口底部，
   * 末几行若不翻转，菜单会掉到视口外，最后一档「全部数据」根本点不到）。
   * 两轴都夹回视口内，窄屏横滚时也不会跑出去。
   */
  useLayoutEffect(() => {
    if (!open || placement !== "fixed" || !anchor || !menuRef.current) return;
    const m = menuRef.current.getBoundingClientRect();
    const GAP = 8;
    const EDGE = 8;
    const below = anchor.bottom + GAP;
    const above = anchor.top - GAP - m.height;
    const top =
      below + m.height <= window.innerHeight - EDGE || above < EDGE
        ? Math.min(below, Math.max(EDGE, window.innerHeight - EDGE - m.height))
        : above;
    const left = Math.max(
      EDGE,
      Math.min(anchor.right - m.width, window.innerWidth - m.width - EDGE),
    );
    setPos((p) => (p && p.left === left && p.top === top ? p : { left, top }));
  }, [open, placement, anchor]);

  // 时刻在点击那一下才取，不在渲染时取——渲染可能发生在几分钟前
  const scopes = (): ClearScope[] => {
    const now = Date.now();
    const tg = target ? { model: target.model, baseUrl: target.baseUrl } : undefined;
    return [
      { key: "24h", label: t("usage.clearRange24h"), sinceMs: now - 24 * 3600_000, target: tg },
      { key: "7d", label: t("usage.clearRange7d"), sinceMs: now - 7 * 24 * 3600_000, target: tg },
      {
        key: "all",
        label: target ? t("usage.clearTargetAll") : t("usage.clearAllData"),
        sinceMs: null,
        target: tg,
      },
    ];
  };

  return (
    <div className="relative inline-flex" ref={wrapRef}>
      <button
        type="button"
        onClick={(e) => {
          const next = !open;
          if (next && placement === "fixed") {
            // 先记锚点、落点交给上面的 layout effect（那时才量得到菜单尺寸）
            setAnchor((e.currentTarget as HTMLElement).getBoundingClientRect());
            setPos(null);
          }
          setOpen(next);
        }}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={target ? t("usage.clearTargetAria") : t("usage.clearData")}
        title={target ? t("usage.clearTargetAria") : t("usage.clearData")}
        // 30×30：两处是同类动作，不该一个大一个小；也高于 24×24 的最小点击目标
        className={`inline-flex items-center justify-center w-[30px] h-[30px] rounded-md
                    border transition-colors ${
                      open
                        ? "border-error text-error bg-error-bg"
                        : "border-transparent text-text-tertiary hover:text-error hover:border-error"
                    }`}
      >
        <IconTrash width={15} height={15} />
      </button>

      {open && (
        <div
          id={menuId}
          role="menu"
          ref={menuRef}
          style={
            placement === "fixed"
              ? {
                  position: "fixed",
                  width: 216,
                  // 落点未算出前先摆到锚点下方并隐形：避免第一帧闪在 (0,0)
                  left: pos ? pos.left : (anchor?.right ?? 0) - 216,
                  top: pos ? pos.top : (anchor?.bottom ?? 0) + 8,
                  visibility: pos ? undefined : "hidden",
                }
              : undefined
          }
          // text-left 是必须的：行内那个浮层在 DOM 上仍是 text-right 单元格的后代，
          // 不显式左对齐，标题与作用域徽记会跟着右对齐（菜单项自带 text-left 才没露出来）。
          className={`z-50 min-w-[196px] rounded-xl border border-border text-left
                      bg-bg-secondary shadow-lg p-2 ${
                        placement === "fixed"
                          ? ""
                          : "absolute right-0 top-[calc(100%+8px)]"
                      }`}
        >
          {/* 定点清除时先摆出作用域，样式与明细的模型列一致——菜单是哪一项打开的
              必须一眼可见，否则「这一项的全部数据」指的是谁就要靠记忆。 */}
          {target && (
            <div className="px-1.5 pb-2 mb-1 border-b border-border flex items-center
                            gap-1.5 flex-wrap text-caption">
              {target.label}
            </div>
          )}
          <div className="px-1.5 pb-1.5 text-caption text-text-tertiary">
            {target ? t("usage.clearTargetMenuTitle") : t("usage.clearMenuTitle")}
          </div>
          {scopes().map((s, i) => (
            <div key={s.key}>
              {/* 「全部」与另两档不是一类，用分隔线划开并上警示色 */}
              {i === 2 && <div className="h-px bg-border my-1.5 mx-1" />}
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setOpen(false);
                  onPick(s);
                }}
                className={`w-full text-left text-body px-2.5 py-1.5 rounded-md transition-colors ${
                  s.key === "all"
                    ? "text-error hover:bg-error-bg"
                    : "text-text-secondary hover:text-text-primary hover:bg-bg-primary"
                }`}
              >
                {s.label}
                {/* 工具条那个要把「所有模型」写出来：明细里多了逐行清除之后，
                    「清近 24 小时」这几个字本身已不足以说明清的是谁。 */}
                {!target && (
                  <span className="block text-caption text-text-tertiary">
                    {t(
                      s.key === "all"
                        ? "usage.clearScopeAllModelsAllTime"
                        : "usage.clearScopeAllModels",
                    )}
                  </span>
                )}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
