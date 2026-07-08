# 设计原则

> 设计 SSOT (Mi Console v3 哲学铁律)
> 所有新模块上线前必须满足这七条铁律,违反任何一条都会导致视觉系统崩塌。

---

## 视觉血脉(定位)

**Mi Console v3** = `mi.com/global/support` 的克制专业 × Stripe / Vercel / Linear 的 dev tool 密度感 × **小米橙作功能色(不作面色)**。

类比:**实验室仪器面板**——白色背板承载黑色刻度与读数,橙色用于指示关键状态。信息组织遵循 **数据密度 > 装饰** 的硬约束,任何不传递信息的视觉元素(渐变、阴影、装饰图案、立体透视)都被剔除。

---

## 七条铁律

### 1. 白底主线 + 小米橙作功能色

`#FF6700` 只用于 **link / active / CTA / focus**,**不允许大面积铺底**。

视觉口诀:**白面板 → 灰画布 → 白卡片**,层次靠灰白交替。

- 左 Sidebar + TopBar:`bg-bg-secondary`(白,顶栏与左面板齐平)
- StatusRibbon + 主区 canvas:`bg-bg-primary`(灰)
- 内容卡片:`<section>` 用 `bg-bg-secondary`(白)浮在灰 canvas 上
- panel 内的差异化元素:用 `bg-bg-primary`(灰,inset 视觉)或 `bg-bg-tertiary`(更深灰,hover 态)

> dark 主题已经是这个结构(canvas 暗 / 面板和卡片亮),light 与 dark 现已对齐。

### 2. 状态用点不用块

**5px 圆点 + 3px 半透明光环** (`box-shadow: 0 0 0 3px var(--color-{tone}-bg)`) 替代色块 badge / chip。

色映射:`ok=success` / `info=info` / `warn=warning` / `danger=error` / `brand=brand-primary`。

> chip 是给"可点击的预设/筛选",**不是状态信号**——别混。

### 3. 数字字体强制走 mono

任何 **IID / device_id / token / 时间戳 / 计数 / 百分比 / 价格**,**必须**:

- 单数字 → `class="num"`(强调 tabular-nums)
- 一段英文 ID → `class="mono"`(同 family,语义自解释)
- dev tool 风 caption(`now`、`42 events`、`mockcase=crowded`)→ `class="text-caption-mono"`

### 4. dark 自动生效

所有颜色走 `var(--color-*)` 或 Tailwind 语义类(`bg-bg-secondary` 这种)。**禁止 hex / rgb 内联**(资产级 logo 例外)。

### 5. 响应式断点只用 Tailwind 标配

`md:` (768px) / `lg:` (1024px) / `xl:` (1280px)。**禁止再发明 breakpoint**(诸如 900px、840px 这种)。

### 6. 动效克制

- hover 仅改 border 颜色 + 极小 transform(`scale(0.99)`)
- **不上 box-shadow 抬升**
- 过渡时长 ≤ 320ms

通用动画类:

- `class="anim-in"` — fade-in-up 8px / 320ms / emphasized,卡片 / dialog 进入用
- `class="lift"` — 极克制 hover:`border` 强化 + active:`scale(.99)`

### 7. 没有 emoji(代码注释除外)

正文用 `IconPerson` / `IconCamera` 等 lucide 风 SVG(`src/lib/icons.tsx`),**不要**直接把 `🏠`、`✨` 写进文案。

---

## "白 - 灰 - 白" 三段式应用规则

| 区域                                 | 颜色 token                          | 备注                          |
| ------------------------------------ | ----------------------------------- | ----------------------------- |
| 左 Sidebar                           | `bg-bg-secondary` (白)              | 应用左面板                    |
| TopBar                               | `bg-bg-secondary`                   | 与左面板齐平                  |
| StatusRibbon / 主 canvas             | `bg-bg-primary` (灰)                | 工作区底色                    |
| 内容卡片 `<section>`                 | `bg-bg-secondary` (白)              | 浮在灰 canvas 上              |
| Modal / Drawer 内表面                | `bg-bg-secondary` (白)              | 同卡片                        |
| chip / hover 态 / 输入框(在白面板内) | `bg-bg-primary` 或 `bg-bg-tertiary` | inset 视觉                    |
| 摄像头黑舞台                         | `bg-bg-stage`                       | 与上述四档无关,独立强对比黑底 |

---

## 修改设计 token 的决策权

LLM 默认**不允许**改 design token / 改铁律。改动流程:

1. **加 token**:先写到 `src/styles/theme.css` light + dark 双轨,然后回 `design-tokens.md` 加一行
2. **加组件原型**:先在某个页面落地一次,等被另一页复用时,再抽到 `component-patterns.md`
3. **改铁律(本文档)**:必须在 Tech RFC 留痕,人类负责拍板
