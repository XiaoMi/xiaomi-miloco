# 布局 · 可访问性 · 反模式

> 设计 SSOT (布局 / a11y / 反模式)
> 这一份是验收 checklist —— 每条都对应一个上线前必须 self-check 的扣分项。

---

## 1. 布局 / 栅格

| 场景                       | 写法                                  |
| -------------------------- | ------------------------------------- |
| 主区最大宽度               | `max-w-[1200px] mx-auto px-4 md:px-8` |
| 卡片纵向间距               | `space-y-6`                           |
| 卡片内 grid                | 常用 `grid gap-3 md:grid-cols-2`      |
| mobile breakpoint(< 768px) | Sidebar 折叠为底部 TabBar             |

主框架结构(自 `App.tsx`):

```tsx
<div className="h-screen flex overflow-hidden bg-bg-primary text-text-primary">
  <Sidebar /> {/* 左面板,白色 */}
  <div className="flex-1 flex flex-col min-w-0 min-h-0">
    <TopBar /> {/* shrink-0 顶栏 */}
    <StatusRibbon /> {/* shrink-0 状态条 */}
    <main className="flex-1 overflow-y-auto min-h-0">
      <div className="max-w-[1200px] mx-auto px-4 md:px-8 pt-5 pb-12">
        {/* tab 内容 — 唯一可滚区域 */}
      </div>
    </main>
    <MobileTabBar /> {/* md:hidden 底部 tab bar */}
  </div>
</div>
```

---

## 2. 可访问性(a11y 必做项)

| 元素                | 必做                                                                                                                            |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| **每个交互元素**    | `aria-label`(icon-only)或可见文本                                                                                               |
| **dialog**          | `role="dialog"` + `aria-modal="true"` + `aria-labelledby` + ESC 关闭(用 `useEscClose` hook)                                     |
| **switch / toggle** | `role="switch"` + `aria-checked`                                                                                                |
| **可折叠区域**      | `aria-expanded`                                                                                                                 |
| **focus ring**      | `focus-visible:ring-2 focus-visible:ring-brand-primary focus-visible:outline-none`(全局已配 outline,但内嵌按钮覆盖时记得加回来) |
| **图标 only 装饰**  | `aria-hidden`                                                                                                                   |
| **状态点**          | `aria-hidden`,文字承担语义                                                                                                      |
| **loading 占位**    | `role="status"` + `aria-live="polite"`                                                                                          |
| **error 占位**      | `role="alert"`                                                                                                                  |

全局 focus 规则(已在 `theme.css` 配,**不要本地覆盖 `border-radius`**——会破坏 rounded-full chips):

```css
button:focus-visible,
a:focus-visible,
input:focus-visible {
  outline: 2px solid var(--color-brand-primary);
  outline-offset: 2px;
}
.no-focus-ring:focus-visible {
  outline: none;
}
```

---

## 3. 命名约定(代码层面)

| 类型            | 规则                                                                                      |
| --------------- | ----------------------------------------------------------------------------------------- |
| 组件文件        | `PascalCase.tsx`                                                                          |
| 纯逻辑文件      | `camelCase.ts`                                                                            |
| 组件声明        | `function MyComp(props: Props)` —— **不写 `React.FC`**                                    |
| props 接口      | `interface Props {}`(同文件单组件)/ `interface MyCompProps {}`(多组件)                    |
| 路径别名        | `@/` → `src/`(已在 `vite.config.ts` 配)                                                   |
| Tailwind 类顺序 | **布局 → 间距 → 边框 → 颜色 → 排版 → 状态**(参考已有组件;遵循 Tailwind/Prettier 默认即可) |

---

## 4. 常见反模式(LLM 容易踩,扣分项)

| ❌ 不要                                                                     | ✅ 改成                                                                   |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| `style={{ color: '#FF6700' }}`                                              | `text-brand-primary` 或 `style={{ color: 'var(--color-brand-primary)' }}` |
| `bg-orange-500` / `text-red-600`                                            | `bg-brand-primary` / `text-error`                                         |
| `rounded-2xl` 用在普通卡片                                                  | `rounded-xl`(2xl 留给 modal)                                              |
| 状态用 `<Chip color="green">在线</Chip>`                                    | Status Dot(component-patterns §3)                                         |
| 数字 / ID 不加 mono                                                         | `class="num"` 或 `class="mono"` / `text-caption-mono`                     |
| 一处发明 `breakpoint: 900px`                                                | 用 `md:` / `lg:`                                                          |
| 卡片 hover 加 `shadow-lg` 抬升                                              | 仅 `lift` class 改 border                                                 |
| Drawer 写死 `w-[400px]`                                                     | `max-w-md`                                                                |
| `<div onClick>` 当按钮                                                      | 改 `<button type="button">`                                               |
| 多个 modal 抢 `z-50`                                                        | 都用 `z-50`,最多再 +1 + 注释                                              |
| 装饰 emoji `🏠` 写在文案里                                                  | 用 lucide 风 icon 组件(`src/lib/icons.tsx`,如 `IconPerson`/`IconCamera`)  |
| 直接 `console.log` 加 emoji 标记                                            | 走 `toast()`(见 `Toast.tsx`)                                              |
| `style={{ fontSize: 14 }}` 或 `style={{ fontSize: "var(--font-size-md)" }}` | `className="text-title"`(design-tokens §3)                                |
| `style={{ fontSize: 11, letterSpacing: "0.02em" }}`                         | `className="text-caption-mono"`                                           |
| 同一卡片混 10/11/12/13/14/15 各种字号                                       | 4 档以内:caption(12) + body(14) + title(16),其它降级或升级到这 3 档       |
| `font-medium` (500)                                                         | `font-semibold` (600) — 14px 上 500 跟 400 几乎不可见                     |
| `style={{ fontSize: 10 }}`(非 SVG)                                          | 升到 caption(12)— 10px 在桌面屏密度下太小,无障碍不友好                    |
| **新建** `theme-extra.css`                                                  | 直接改 `theme.css` 文末的「业务扩展」段                                   |
| Sidebar 用 `bg-bg-primary`(灰)                                              | 用 `bg-bg-secondary`(白)—— 违反"白 - 灰 - 白"三段式                       |
| `outline: none` 不补 focus 替代                                             | 如果不要默认 outline,加 `class="no-focus-ring"` 或自己上 ring             |

---

## 5. 调试 / 验证

写完新组件,对照原型确认无视觉异常:

- 切 `light` ↔ `dark`(顶栏 ☀/🌙 按钮)
- 模拟 mobile(浏览器 DevTools < 768px)
- 如有 `_mock/` 目录,可用 `?mock=1&mockcase=empty/crowded/error` 等场景逐一走查
