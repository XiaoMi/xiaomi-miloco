import type { Config } from "tailwindcss";

// 色板走 CSS variables（src/styles/theme.css）。命名严格对齐 xiaoai-design-system
// `_shared/design-token.md`。web 业务扩展（语义色 -bg、brand soft / ring、
// error solid 等）也统一在 theme.css 里加在标准 token 旁边。
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      screens: {},
      colors: {
        // 背景（_shared）
        "bg-primary": "var(--color-bg-primary)",
        "bg-secondary": "var(--color-bg-secondary)",
        "bg-tertiary": "var(--color-bg-tertiary)",
        "bg-elevated": "var(--color-bg-elevated)",
        "bg-stage": "var(--color-bg-stage)",

        // 边框（_shared）
        border: "var(--color-border)",
        "border-strong": "var(--color-border-strong)",

        // 文字（_shared）
        text: {
          primary: "var(--color-text-primary)",
          secondary: "var(--color-text-secondary)",
          tertiary: "var(--color-text-tertiary)",
          disabled: "var(--color-text-disabled)",
          link: "var(--color-text-link)",
          inverse: "var(--color-text-inverse)",
        },

        // 品牌（_shared）+ 业务扩展 soft / ring
        brand: {
          primary: "var(--color-brand-primary)",
          secondary: "var(--color-brand-secondary)",
          accent: "var(--color-brand-accent)",
          soft: "var(--color-brand-soft)",
          ring: "var(--color-brand-ring)",
        },

        // 语义（_shared）+ 业务扩展 -bg
        success: {
          DEFAULT: "var(--color-success)",
          bg: "var(--color-success-bg)",
        },
        warning: {
          DEFAULT: "var(--color-warning)",
          bg: "var(--color-warning-bg)",
        },
        error: {
          DEFAULT: "var(--color-error)",
          bg: "var(--color-error-bg)",
        },
        info: {
          DEFAULT: "var(--color-info)",
          bg: "var(--color-info-bg)",
        },
      },
      fontSize: {
        // 语义档(优先用):caption / body / title / display / display-lg
        // 同时附带 line-height,Tailwind 的 text-* 类会一次性给到 size + leading
        caption: ["var(--font-size-caption)", { lineHeight: "1.45" }],
        body: ["var(--font-size-body)", { lineHeight: "1.55" }],
        title: ["var(--font-size-title)", { lineHeight: "1.4", letterSpacing: "var(--tracking-tight)" }],
        display: ["var(--font-size-display)", { lineHeight: "1.2", letterSpacing: "var(--tracking-display)" }],
        "display-lg": ["var(--font-size-display-lg)", { lineHeight: "1.1", letterSpacing: "var(--tracking-display)" }],
        "page-title": ["var(--font-size-page-title)", { lineHeight: "1.3", letterSpacing: "var(--tracking-tight)" }],
        // 旧 alias(deprecated 但保留)
        "2xs": ["var(--font-size-caption)", { letterSpacing: "var(--tracking-mono)" }],
      },
      fontFamily: {
        sans: [
          "MiSans",
          "-apple-system",
          "BlinkMacSystemFont",
          "PingFang SC",
          "Microsoft YaHei",
          "Helvetica Neue",
          "Segoe UI",
          "Roboto",
          "Arial",
          "sans-serif",
          "Apple Color Emoji",
          "Segoe UI Emoji",
          "Segoe UI Symbol",
          "Noto Color Emoji",
        ],
        // 数字 / 英文 / IID / device_id / 时间戳——dev tool 风
        mono: [
          "Geist Mono",
          "JetBrains Mono",
          "SF Mono",
          "Menlo",
          "Consolas",
          "Liberation Mono",
          "monospace",
        ],
      },
      // 阴影（_shared）
      boxShadow: {
        sm: "var(--shadow-sm)",
        md: "var(--shadow-md)",
        lg: "var(--shadow-lg)",
      },
      // 圆角（_shared 5 档 + web 业务扩展 2xl）
      borderRadius: {
        sm: "var(--radius-sm)",
        md: "var(--radius-md)",
        lg: "var(--radius-lg)",
        xl: "var(--radius-xl)",
        "2xl": "var(--radius-2xl)",
        full: "var(--radius-full)",
      },
      spacing: {
        "tk-xs": "var(--spacing-xs)",
        "tk-sm": "var(--spacing-sm)",
        "tk-md": "var(--spacing-md)",
        "tk-lg": "var(--spacing-lg)",
        "tk-xl": "var(--spacing-xl)",
        "tk-2xl": "var(--spacing-2xl)",
      },
      transitionDuration: {
        fast: "var(--duration-fast)",
        normal: "var(--duration-normal)",
        slow: "var(--duration-slow)",
      },
      transitionTimingFunction: {
        default: "var(--easing-default)",
        emphasized: "var(--easing-emphasized)",
      },
    },
  },
  plugins: [],
} satisfies Config;
