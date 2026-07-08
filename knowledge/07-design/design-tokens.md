# 设计 Token

> 设计 SSOT (token 表，跟 web/src/styles/theme.css + tailwind.config.ts 同步)
> 命名严格对齐 `xiaoai-design-system _shared/design-token.md`,业务扩展见文末。

所有 token 走 CSS variables,**dark 主题自动镜像**。新代码请用语义 Tailwind 类(`bg-bg-secondary`、`text-text-primary`),**不要**写 `style={{ color: '#FF6700' }}`。

---

## 1. 颜色

### 1.1 背景层级(画布灰 / 卡片白)

| token          | light     | dark      | 用途                                                |
| -------------- | --------- | --------- | --------------------------------------------------- |
| `bg-primary`   | `#F4F5F7` | `#0E0E0E` | 画布 / chrome 框 / 主区 / TopBar / StatusRibbon     |
| `bg-secondary` | `#FFFFFF` | `#161616` | 内容卡片 / Modal / Drawer / Sidebar 内表面 / 输入框 |
| `bg-tertiary`  | `#EBEDEF` | `#1F1F1F` | hover 态 / chip 容器 / segmented control 底         |
| `bg-elevated`  | `#DDE1E6` | `#2A2A2A` | 罕用,nested 强调底                                  |
| `bg-stage`     | `#1F1F1F` | `#000000` | 摄像头黑舞台                                        |

### 1.2 文字 6 阶

| token            | light     | dark      | 用途             |
| ---------------- | --------- | --------- | ---------------- |
| `text-primary`   | `#1F1F1F` | `#F5F5F5` | 标题 / 正文      |
| `text-secondary` | `#6B6B6B` | `#B5B5B5` | 副标题 / meta    |
| `text-tertiary`  | `#9A9A9A` | `#888888` | caption / hint   |
| `text-disabled`  | `#C5C5C5` | `#555555` | disabled         |
| `text-link`      | `#FF6700` | `#FF8533` | 文字链           |
| `text-inverse`   | `#FFFFFF` | `#1F1F1F` | 在深色按钮上的字 |

### 1.3 品牌(小米橙,作功能色)

| token               | light                 | dark                   | 用途                                          |
| ------------------- | --------------------- | ---------------------- | --------------------------------------------- |
| `brand-primary`     | `#FF6700`             | `#FF8533`              | CTA bg / active text / link                   |
| `brand-secondary`   | `#FF8533`             | `#FFA86B`              | 偶尔 hover 强化                               |
| `brand-accent`      | `#FF5C00`             | `#FF6700`              | pressed                                       |
| `brand-soft`        | `rgba(255,103,0,.08)` | `rgba(255,133,51,.10)` | 浅底背景(active tab / hover state / 用户气泡) |
| `brand-soft-strong` | `rgba(...,.14)`       | `rgba(...,.18)`        | 比 soft 强一档,极少用                         |
| `brand-ring`        | `rgba(...,.20)`       | `rgba(...,.24)`        | focus ring / focus-within border              |

### 1.4 语义色(只用于状态点 + 极小面积)

| token     | light     | bg(8% 透明) | 用途                                  |
| --------- | --------- | ----------- | ------------------------------------- |
| `success` | `#16A34A` | `#16A34A14` | ok 状态点 / 成功 toast                |
| `warning` | `#D97706` | `#D9770614` | 注意状态点 / 警告 chip                |
| `error`   | `#DC2626` | `#DC262614` | danger 状态点 / 错误 toast / 离线设备 |
| `info`    | `#2563EB` | `#2563EB14` | 中性提示                              |

> `-bg` 后缀**只**用于 status dot 光环 / toast 软底 / chip 软底。**禁止**用 `bg-success` 实色给整张卡片刷底。

### 1.5 边框

| token           | light     | dark      | 用途                    |
| --------------- | --------- | --------- | ----------------------- |
| `border`        | `#E5E5E5` | `#2A2A2A` | 默认 hairline           |
| `border-strong` | `#CCCCCC` | `#3A3A3A` | hover / active / 强分隔 |

---

## 2. 字体

```
font-sans  MiSans → -apple-system → 苹方 → 雅黑   — 中文 / 正文
font-mono  Geist Mono → JetBrains Mono → SF Mono — 数字 / ID / 时间戳
```

**font-mono 触发条件**(任意命中即用):

- 任何**纯数字**显示(token 数 / 价格 / 百分比 / 计数 / 时间 HH:MM)
- **设备 ID / IID / cam_did / task_id**(开发者面向的标识)
- **rule_id 类**面包屑(`rule_living_off` 这种)
- **dev tool 风 caption**(`now`, `42 events`, `mockcase=crowded`)

实现:

- 单数字加 `class="num"`(`font-variant-numeric: tabular-nums; letter-spacing: -.01em`)
- 一段英文 ID 加 `class="mono"`(同 family,语义自解释)

---

## 3. 字号(4 档黄金阶梯)

**铁律:不允许写 inline `style={{ fontSize: ... }}`,一律用 `text-*` Tailwind 类。**

| 类名                 | px     | line-height | letter-spacing | 用途                                                                                              |
| -------------------- | ------ | ----------- | -------------- | ------------------------------------------------------------------------------------------------- |
| `text-caption`       | **12** | 1.45        | —              | 辅助文字:元数据 / 计数 / hint / 角标 / source 标签                                                |
| `text-caption-mono`  | 12     | 1.45        | `0.02em`       | mono caption(cam_did / 时间戳 / rule_id / events 计数)                                            |
| `text-body`          | **14** | 1.55        | —              | 标准正文 / 列表项 / chip / dropdown / 按钮文字 / 输入框                                           |
| `text-title`         | **16** | 1.4         | `-0.005em`     | 卡片标题 / dialog 标题 / 列表主字 / nav label                                                     |
| `text-display`       | **24** | 1.2         | `-0.01em`      | Hero 数字 / 醒目计数 / 头像首字                                                                   |
| `text-display-lg`    | **32** | 1.1         | `-0.01em`      | Usage Hero 大数字 — 全项目仅 1 处                                                                 |
| `text-section-title` | **20** | 1.35        | `-0.005em`     | 分区大标题(20px + 加粗):比 `text-title` 更显著,用于「模型配置 / Token 用量 / 性能监测」等页内分区 |
| `text-page-title`    | **20** | 1.3         | `-0.005em`     | 20px 档 token(`--font-size-page-title`),现由 `text-section-title` 复用承载分区标题                |

**设计原则**:

- 基底 14px(标准正文),12 辅助 / 16 标题 / 24 大数字 / 32 极大字
- **10px 尽量不用**:仅 SVG 轴标这种空间极紧的图表场景豁免(且必须加注释)
- 相邻档 Δ ≥ 2px,确保视觉层级清晰
- 字号类**已经包含** line-height 与必要的 letter-spacing,不要再 inline override

**字号锁定**:字号统一锁在上表黄金阶梯,无运行时缩放档(不提供动态字号调节)。

**deprecated 别名**(渐进迁移期保留,实际 px 自动指向新阶梯):

| 旧 token          | 现行映射    |
| ----------------- | ----------- |
| `--font-size-2xs` | caption(12) |
| `--font-size-xs`  | caption(12) |
| `--font-size-sm`  | body(14)    |
| `--font-size-md`  | title(16)   |
| `--font-size-lg`  | title(16)   |
| `--font-size-xl`  | display(24) |

> **新代码请直接用 `text-*` 类**,不要再写 `style={{ fontSize: "var(--font-size-md)" }}`。

---

## 4. 字重(只用 2 档)

```
regular  400  — 正文 / hint
semibold 600  — 标题 / 数字 / 强调 / 按钮文字
```

> ❌ **不要用 `font-medium` (500)**:14px 字上跟 400 几乎不可见,白增加层级数。
> 老的 `font-medium` 写法仍能跑(token 还在),但新代码一律 `font-semibold` 或省略。

---

## 5. 间距(分两套并存)

### 5.1 标准 spacing token

```
xs  4px / sm  8px / md 12px / lg 16px / xl 24px / 2xl 32px / 3xl 48px
```

Tailwind 暴露为 `tk-xs / tk-sm / ... / tk-2xl`(注意前缀)。日常更常用 Tailwind 自带数值类(`p-5` / `gap-3`),两套等价时**优先 Tailwind 自带**。

### 5.2 实战默认间距(写新组件直接抄)

| 场景           | 间距                                                 |
| -------------- | ---------------------------------------------------- |
| 卡片之间       | `space-y-6`(24px)                                    |
| 卡片内 padding | `p-5 md:p-6`(20→24)                                  |
| 卡片标题区     | `px-5 pt-4 pb-3`,内容区独立 padding                  |
| 列表项垂直     | `py-2.5`(10px)                                       |
| 按钮内边距     | 小 `px-2 py-0.5` / 中 `px-3 py-1.5` / 大 `px-5 py-2` |
| chip / pill    | `px-2.5 py-1`                                        |
| icon button    | `w-8 h-8`(32×32)/ `w-6 h-6`(24×24,极小)              |

---

## 6. 圆角

| token          | px   | 用途                        |
| -------------- | ---- | --------------------------- |
| `rounded-sm`   | 4px  | 内嵌徽章 / 极小元素         |
| `rounded-md`   | 6px  | 按钮 / chip / source 标签   |
| `rounded-lg`   | 8px  | 内嵌容器 / quick action     |
| `rounded-xl`   | 12px | **卡片(主流)**              |
| `rounded-2xl`  | 16px | modal / drawer / sheet 顶角 |
| `rounded-full` | 9999 | 头像 / status dot / pill    |

> **统一规则**:**业务卡片一律 `rounded-xl`**,modal/drawer 才用 `rounded-2xl`/`rounded-t-2xl`。混用会破坏视觉层级。

---

## 7. 阴影

| token       | css                          | 用途                     |
| ----------- | ---------------------------- | ------------------------ |
| `shadow-sm` | `0 1px 2px rgba(0,0,0,.04)`  | 卡片默认(轻到几乎看不见) |
| `shadow-md` | `0 2px 8px rgba(0,0,0,.06)`  | hover / 浮起按钮         |
| `shadow-lg` | `0 8px 24px rgba(0,0,0,.08)` | 悬浮 popover             |

dark 主题阴影自动加深(0.4 / 0.5 / 0.6)。

---

## 8. 动效

| token                 | 值                             | 用途                 |
| --------------------- | ------------------------------ | -------------------- |
| `--duration-fast`     | 120ms                          | 颜色 / 边框          |
| `--duration-normal`   | 200ms                          | 大多数 transition    |
| `--duration-slow`     | 320ms                          | 抽屉 / dialog 进退场 |
| `--easing-default`    | `cubic-bezier(0.4, 0, 0.2, 1)` | 通用                 |
| `--easing-emphasized` | `cubic-bezier(0.2, 0, 0, 1)`   | 抽屉 / 强调          |

通用动画类:

```html
class="anim-in"
<!-- fade-in-up 8px,320ms,emphasized;卡片 / dialog 进入用 -->
class="lift"
<!-- 极克制 hover:border 强化 + active:scale(.99) -->
```

---

## 9. z-index 层级

```
z-base     1     — 默认
z-sidebar  10    — Sidebar / TopBar
z-50      50    — 早期 Toast 兜底值已上移到 z-[100];当前仅 ActivityFeed 媒体灯箱(Lightbox 全屏视频浮层,独立于 dialog 栈、故意坐在 z-[60] 基线下)仍用 z-50
z-[60]    60    — 所有顶层 dialog / drawer / modal(PersonDrawer / EnrollFlow / MiotBindDialog / ConfirmUnbindDialog 等);LivePlayer expanded 用 z-[61] 叠在其 scrim 之上
z-[70]    70    — drawer 之上的次级弹层:家庭记忆详情卡(HomeProfileParts 的 EntryDetailSheet,叠在 HomeKnowledgePanel 抽屉上)、模型配置连通性 tooltip(UsageOmniConfig)
z-[80]    80    — 在 z-[70] 之上的次级确认预留,当前无活跃组件,留作未来双层 modal 场景
z-[100]   100   — Toast 顶层兜底,必须高于所有 modal stack;dialog onConfirm catch 抛 toast 时不被 modal scrim 盖住
```

> **不要发明新 z**。要叠 modal-on-modal 时按上述 stacking 规则递增 10，并写注释解释跟谁叠 + 为什么必须更高。

---

## 10. 工具类(`theme.css` 末尾业务扩展)

### 10.1 状态点工具类

```html
<span class="status-dot status-dot-ok"></span>
<!-- 5px 绿点 + 3px 8% 光环 -->
<span class="status-dot status-dot-info"></span>
<!-- 蓝 -->
<span class="status-dot status-dot-warn"></span>
<!-- 黄 -->
<span class="status-dot status-dot-danger"></span>
<!-- 红 -->
<span class="status-dot status-dot-brand"></span>
<!-- 橙 -->
```

### 10.2 数字 / mono

```css
.num,
.mono {
  font-family: var(--font-family-mono);
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.01em;
}
```

### 10.3 字号工具类

`.text-caption` / `.text-caption-mono` / `.text-body` / `.text-title` / `.text-section-title` / `.text-display` / `.text-display-lg` / `.text-page-title` —— 见 §3 表格。

### 10.4 modal 表面

```css
.modal-surface {
  background-color: var(--color-bg-secondary);
}
```

### 10.5 滚动条工具

```css
.scrollbar-none {
  scrollbar-width: none;
}
.scrollbar-none::-webkit-scrollbar {
  display: none;
}
```

---

## 11. 全局基底(`theme.css` body)

```css
body {
  background: var(--color-bg-primary);
  color: var(--color-text-primary);
  font-family: var(--font-family-sans);
  line-height: 1.6;
}

/* focus-visible:橙色 outline 自动跟随元素 border-radius */
button:focus-visible,
a:focus-visible,
input:focus-visible {
  outline: 2px solid var(--color-brand-primary);
  outline-offset: 2px;
}
.no-focus-ring:focus-visible {
  outline: none;
}

/* 滚动条 — mi.com 系硬细 */
::-webkit-scrollbar {
  width: 10px;
  height: 10px;
}
::-webkit-scrollbar-thumb {
  background: var(--color-border-strong);
  border-radius: var(--radius-sm);
  border: 2px solid var(--color-bg-primary);
}
```

---

## 12. dark 主题切换

通过根元素 `data-theme` 属性控制:

- `:root[data-theme="light"]` — light 主线
- `:root[data-theme="dark"]` — dark 显式
- `@media (prefers-color-scheme: dark)` 配 `:root:not([data-theme="light"])` — 跟随系统

实现挂在 `useTheme` hook,顶栏 `<IconSun /> ↔ <IconMoon />` 按钮切换;设置抽屉支持 auto / light / dark 三档。
