/**
 * 把全屏遮罩 / 抽屉挂到 document.body。
 *
 * 为什么必须 portal：`fixed inset-0` 只是把 top/left/bottom/right 设为 0，**父容器给子元素
 * 加的 margin 依然会把盒子推走**。页面根容器普遍用 `space-y-6`，它会给除首个子元素以外的
 * 每个子元素加 `margin-top: 24px`；于是直接挂在页面里的抽屉遮罩会从 y=24 开始、高度少 24px，
 * 表现就是「遮罩盖不住页面顶部，留了一条边」。
 *
 * 实测（App 内置窗口 1280x860，场景任务抽屉，修复前）：遮罩 rect = `0,24 1280x836`。
 * 挂到 body 之后，遮罩不再受任何祖先的 margin / space-y / transform 影响。
 *
 * 用法（保持原有缩进，不用重排 JSX）：
 *     return modalPortal(
 *       <div className="fixed inset-0 ...">…</div>
 *     );
 */
import { createPortal } from "react-dom";
import type { ReactElement, ReactNode } from "react";

export function modalPortal(node: ReactNode): ReactElement | null {
  if (typeof document === "undefined") return null;
  return createPortal(node, document.body);
}
