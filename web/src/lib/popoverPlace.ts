/**
 * 固定定位浮层的落点计算（纯函数，便于回归）。
 *
 * 为什么要抽出来：明细表同一个单元格里并排着两个 fixed 浮层——URL 气泡与清理菜单。
 * 各写一份落点算法的结果是，一个会在视口底部翻转、另一个不会：明细末几行常常正好
 * 贴着视口底，不翻转的那个整体落到视口外，而滚动会关掉浮层（fixed 会失锚），
 * 于是住户连滚下去看一眼都做不到，观感就是「这个按钮坏了」。同一份认知只落实一半，
 * 是因为它散在两个组件里；收成一个函数后，改一处就是改所有处。
 */

export interface PopoverAnchor {
  top: number;
  bottom: number;
  left: number;
  right: number;
}

export interface PopoverSize {
  width: number;
  height: number;
}

export interface PopoverViewport {
  width: number;
  height: number;
}

export interface PlaceOptions {
  /** 浮层与锚点之间的间隙。 */
  gap: number;
  /** 与视口边缘至少留多少。 */
  edge: number;
  /** 水平对齐到锚点的哪一边：菜单贴右、URL 气泡贴左。 */
  align: "left" | "right";
}

/**
 * 默认贴锚点下方；下方装不下就翻到上方；两轴都夹回视口内。
 *
 * 上下都装不下时（浮层比视口还高）选择贴下方并夹住，而不是翻上——翻上会把浮层的
 * **头部**推出视口，头部往往是标题与作用域说明；夹住至少保证从头开始可读。
 */
export function placePopover(
  anchor: PopoverAnchor,
  box: PopoverSize,
  viewport: PopoverViewport,
  { gap, edge, align }: PlaceOptions,
): { left: number; top: number } {
  const below = anchor.bottom + gap;
  const above = anchor.top - gap - box.height;
  const fitsBelow = below + box.height <= viewport.height - edge;
  const top =
    fitsBelow || above < edge
      ? Math.min(below, Math.max(edge, viewport.height - edge - box.height))
      : above;
  const wanted = align === "right" ? anchor.right - box.width : anchor.left;
  const left = Math.max(edge, Math.min(wanted, viewport.width - box.width - edge));
  return { left, top };
}
