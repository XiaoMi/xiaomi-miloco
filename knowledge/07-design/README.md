# 设计规范（总览）

> Miloco Family UI 设计系统知识库
> 本目录是 web 子工程的设计 SSOT — Mi Console v3 视觉契约。代码侧的 src/styles/theme.css + tailwind.config.ts 与本目录互为引用。

视觉血脉:**Mi Console v3** = mi.com 克制专业 × Stripe / Vercel / Linear 的 dev tool 密度感 × **小米橙作功能色(不作面色)**。

---

## 目录

| 文档                                                    | 说明                                                                              |
| ------------------------------------------------------- | --------------------------------------------------------------------------------- |
| [设计原则](design-principles.md)                        | 设计哲学与七条铁律,LLM 写新模块前必读                                             |
| [设计 Token](design-tokens.md)                          | 颜色 / 字体 / 字号 / 间距 / 圆角 / 阴影 / 动效 / z-index 全量规范                 |
| [组件原型](component-patterns.md)                       | Card / Button / Status Dot / Drawer / Dialog / Toast / Chip / Switch 复制即用模板 |
| [布局 · 可访问性 · 反模式](layout-a11y-antipatterns.md) | 栅格、断点、a11y 必做项、命名约定、25 条反模式扣分项                              |
| [小米品牌设计语言](xiaomi-brand-language.md)            | 小米全套品牌系统(色彩 / 字体 / 版式 / 数据可视化 / Voice)                         |

---

## 关键事实速查

- **单一事实源**: 本目录的 6 篇文档 + `web/src/styles/theme.css` + `web/tailwind.config.ts` (token 冲突时以 theme.css 为准，新增 token 必须同步到 design-tokens.md)
- **预览页**:`web/_mock/style-preview/console-preview.html`(`_mock/` 已被 `web/.gitignore` 忽略，从未入仓、本地私有；没有该目录时预览页不存在，文档以本目录 6 篇规范为准)
- **品牌色**: `#FF6700`（小米橙，web 实际落地值；小米 PPT/品牌体系用 `#FF6900`，1 度偏差）
- **基底字号**:14px(标准正文),阶梯 12 / 14 / 16 / 24 / 32
- **暗色主题自动镜像**,所有颜色走 `var(--color-*)` 或 Tailwind 语义类,**禁止 hex / rgb 内联**
- **响应式断点只用 Tailwind 标配**:`md:` (768px) / `lg:` (1024px) / `xl:` (1280px)
- **z-index 7 档**(详见 design-tokens.md::z-index):`z-base (1)` / `z-sidebar (10)` / `z-50 (媒体灯箱,坐 dialog 基线下)` / `z-[60] (dialog 基线)` / `z-[70] (抽屉上的次级弹层)` / `z-[80] (双层 modal 预留)` / `z-[100] (Toast)`,**禁止重新发明**

---

## LLM 写新组件前的决策树

| 问题                  | 答案                                                                     |
| --------------------- | ------------------------------------------------------------------------ |
| 这是页面级一块卡片吗? | `<section>` + Card 模板(component-patterns §1)                           |
| 主操作还是次操作?     | Primary CTA / Secondary(component-patterns §2)                           |
| 危险操作?             | Danger Solid + 二次确认                                                  |
| 状态信号?             | 5px 状态点 + `bg-{tone}-bg` 光环,不要色块 chip                           |
| ID / 时间戳 / 数字?   | 加 `class="num"` 或 `class="mono"`                                       |
| 抽屉 vs 全屏 dialog?  | 右侧 max-w-md drawer / 居中 dialog                                       |
| 弹层 / overlay?       | dialog/drawer 基线 `z-[60]` + `bg-black/40`(Toast 走 `z-[100]` 顶层兜底) |
| 暗色态?               | 不动手 — 所有 token 自带 dark 镜像,只要别硬编码颜色就 OK                 |
