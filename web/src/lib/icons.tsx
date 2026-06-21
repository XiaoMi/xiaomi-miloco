/**
 * 内联 SVG 图标——不引 lucide-react，第一版手维护一组家庭场景常用的。
 * 风格：1.6px stroke / round join / 24x24 viewBox，与品牌色搭配看起来温和。
 */

import type { SVGProps } from "react";

type Props = SVGProps<SVGSVGElement>;

const base = (p: Props) => ({
  width: 18,
  height: 18,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.6,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  ...p,
});

export const IconCamera = (p: Props) => (
  <svg {...base(p)}>
    <path d="M3 8a2 2 0 0 1 2-2h2.5l1-2h7l1 2H19a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8z" />
    <circle cx="12" cy="13" r="3.5" />
  </svg>
);

export const IconLightbulb = (p: Props) => (
  <svg {...base(p)}>
    <path d="M9 18h6M10 21h4M12 3a6 6 0 0 1 4 10.5c-1 .8-1.5 1.7-1.5 2.5h-5c0-.8-.5-1.7-1.5-2.5A6 6 0 0 1 12 3z" />
  </svg>
);

export const IconAircon = (p: Props) => (
  <svg {...base(p)}>
    <rect x="3" y="5" width="18" height="9" rx="2" />
    <path d="M7 14v2c0 1 .5 2 2 2M12 14v3M17 14v2c0 1-.5 2-2 2" />
    <path d="M7 9h10" />
  </svg>
);

export const IconWind = (p: Props) => (
  <svg {...base(p)}>
    <path d="M3 8h11a3 3 0 1 0-3-3M3 12h17M3 16h11a3 3 0 1 1-3 3" />
  </svg>
);

export const IconCurtain = (p: Props) => (
  <svg {...base(p)}>
    <path d="M3 4h18M5 4v16c1.5-1 3-2 3-5s-1.5-4-3-5V4M19 4v16c-1.5-1-3-2-3-5s1.5-4 3-5V4" />
  </svg>
);

export const IconLock = (p: Props) => (
  <svg {...base(p)}>
    <rect x="5" y="11" width="14" height="9" rx="2" />
    <path d="M8 11V8a4 4 0 0 1 8 0v3" />
  </svg>
);

export const IconTV = (p: Props) => (
  <svg {...base(p)}>
    <rect x="3" y="5" width="18" height="13" rx="2" />
    <path d="M8 21h8" />
  </svg>
);

// 通用电器/插头占位（用于音箱/牙刷/烤箱/料理锅 等无专属图标的 category）
export const IconPlug = (p: Props) => (
  <svg {...base(p)}>
    <path d="M6 8V4M18 8V4M5 8h14v3a5 5 0 0 1-5 5h-4a5 5 0 0 1-5-5V8zM12 16v4" />
  </svg>
);

export const IconChevronRight = (p: Props) => (
  <svg {...base(p)}>
    <path d="M9 6l6 6-6 6" />
  </svg>
);

export const IconChevronDown = (p: Props) => (
  <svg {...base(p)}>
    <path d="M6 9l6 6 6-6" />
  </svg>
);

export const IconChevronLeft = (p: Props) => (
  <svg {...base(p)}>
    <path d="M15 6l-6 6 6 6" />
  </svg>
);

export const IconX = (p: Props) => (
  <svg {...base(p)}>
    <path d="M5 5l14 14M19 5L5 19" />
  </svg>
);

export const IconPlus = (p: Props) => (
  <svg {...base(p)}>
    <path d="M12 5v14M5 12h14" />
  </svg>
);

export const IconPencil = (p: Props) => (
  <svg {...base(p)}>
    <path d="M12 20h9" />
    <path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5z" />
  </svg>
);

export const IconEye = (p: Props) => (
  <svg {...base(p)}>
    <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z" />
    <circle cx="12" cy="12" r="3" />
  </svg>
);

export const IconMoon = (p: Props) => (
  <svg {...base(p)}>
    <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
  </svg>
);

export const IconClock = (p: Props) => (
  <svg {...base(p)}>
    <circle cx="12" cy="12" r="8" />
    <path d="M12 7v5l3 2" />
  </svg>
);

export const IconAlert = (p: Props) => (
  <svg {...base(p)}>
    <path d="M12 9v4M12 17h.01" />
    <path d="M10.3 3.9L2.6 17a2 2 0 0 0 1.7 3h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
  </svg>
);

export const IconCheck = (p: Props) => (
  <svg {...base(p)}>
    <path d="M5 13l4 4L19 7" />
  </svg>
);

export const IconShield = (p: Props) => (
  <svg {...base(p)}>
    <path d="M12 3l8 3v6c0 4.5-3.5 8-8 9-4.5-1-8-4.5-8-9V6l8-3z" />
  </svg>
);

// 太阳 —— light 模式标识
export const IconSun = (p: Props) => (
  <svg {...base(p)}>
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" />
  </svg>
);

// 横向三点 —— 「更多」菜单触发
export const IconMore = (p: Props) => (
  <svg {...base(p)}>
    <circle cx="5" cy="12" r="1" />
    <circle cx="12" cy="12" r="1" />
    <circle cx="19" cy="12" r="1" />
  </svg>
);

// 垃圾桶 —— 删除
export const IconTrash = (p: Props) => (
  <svg {...base(p)}>
    <path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13" />
  </svg>
);

/** 人物剪影（填充）—— PersonAvatar 占位 */
export const IconPerson = (p: Props) => (
  <svg
    viewBox="0 0 1024 1024"
    fill="currentColor"
    xmlns="http://www.w3.org/2000/svg"
    aria-hidden
    {...p}
  >
    <path d="M512 538.1c130.9 0 237-106.1 237-237s-106.1-237-237-237-237 106.1-237 237 106.1 237 237 237zm0 110.6c-218.2 0-395.1 69.7-395.1 155.6S293.8 960 512 960s395.1-69.7 395.1-155.6S730.2 648.7 512 648.7z" />
  </svg>
);
