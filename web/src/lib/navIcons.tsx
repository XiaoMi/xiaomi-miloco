/**
 * 左侧 Tab 导航图标 — 来自设计提供的 outline / filled 双态 SVG。
 * 每个 Icon 接收 active 决定填色态;颜色用 currentColor，由父级 text-* 控制。
 * 高亮缺口固定白色(#fff),与 active 态橙色填充形成对比。
 */

import type { SVGProps } from "react";

type Props = SVGProps<SVGSVGElement> & { active?: boolean };

const baseSvg = (p: SVGProps<SVGSVGElement>, viewBox = "0 0 48 48") => ({
  width: 24,
  height: 24,
  viewBox,
  fill: "none",
  xmlns: "http://www.w3.org/2000/svg",
  ...p,
});

/** 此刻 — 房子 + 弧线 */
export const IconNow = ({ active, ...p }: Props) =>
  active ? (
    <svg {...baseSvg(p)}>
      <path
        d="M4 20L24 6L44 20V42H4V20Z"
        fill="currentColor"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M12.6865 26.6863C14.1723 25.2006 15.9361 24.022 17.8773 23.2179C19.8185 22.4139 21.8991 22 24.0002 22C26.1014 22 28.182 22.4139 30.1232 23.2179C32.0644 24.022 33.8282 25.2006 35.314 26.6863"
        stroke="#fff"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M18.3428 32.3431C19.0856 31.6003 19.9676 31.011 20.9382 30.609C21.9088 30.2069 22.9491 30 23.9996 30C25.0502 30 26.0905 30.2069 27.0611 30.609C28.0317 31.011 28.9136 31.6003 29.6565 32.3431"
        stroke="#fff"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  ) : (
    <svg {...baseSvg(p)}>
      <path
        d="M4 20L24 6L44 20V42H4V20Z"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M12.6865 26.6863C14.1723 25.2006 15.9361 24.022 17.8773 23.2179C19.8185 22.4139 21.8991 22 24.0002 22C26.1014 22 28.182 22.4139 30.1232 23.2179C32.0644 24.022 33.8282 25.2006 35.314 26.6863"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M18.3428 32.3431C19.0856 31.6003 19.9676 31.011 20.9382 30.609C21.9088 30.2069 22.9491 30 23.9996 30C25.0502 30 26.0905 30.2069 27.0611 30.609C28.0317 31.011 28.9136 31.6003 29.6565 32.3431"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );

/** 设备 — 量表/仪表盘 */
export const IconDevices = ({ active, ...p }: Props) =>
  active ? (
    <svg {...baseSvg(p, "0 0 49 48")}>
      <path
        d="M24.7778 8C13.7321 8 4.77783 16.9543 4.77783 28H44.7778C44.7778 16.9543 35.8235 8 24.7778 8Z"
        fill="currentColor"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M24.7778 4V8" stroke="currentColor" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" />
      <path
        d="M24.7778 38C19.255 38 14.7778 33.5228 14.7778 28H34.7778C34.7778 33.5228 30.3007 38 24.7778 38Z"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M40.8118 38.9766L38.7437 36.0231"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M11.0525 36.2251L8.50298 38.7746"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M34.7778 42L33.6307 40.3617"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M15.9114 40.4736L14.4972 41.8878"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  ) : (
    <svg {...baseSvg(p, "0 0 49 48")}>
      <path
        d="M24.7778 8C13.7321 8 4.77783 16.9543 4.77783 28H44.7778C44.7778 16.9543 35.8235 8 24.7778 8Z"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M24.7778 4V8" stroke="currentColor" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" />
      <path
        d="M24.7778 38C19.255 38 14.7778 33.5228 14.7778 28H34.7778C34.7778 33.5228 30.3007 38 24.7778 38Z"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M40.8118 38.9766L38.7437 36.0231"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M11.0525 36.2251L8.50298 38.7746"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M34.7778 42L33.6307 40.3617"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M15.9114 40.4736L14.4972 41.8878"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );

/** 家人 — 双人 */
export const IconFamily = ({ active, ...p }: Props) =>
  active ? (
    <svg {...baseSvg(p)}>
      <path
        d="M19 20C22.866 20 26 16.866 26 13C26 9.13401 22.866 6 19 6C15.134 6 12 9.13401 12 13C12 16.866 15.134 20 19 20Z"
        fill="currentColor"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M32.6077 7C34.6405 8.2249 36.0001 10.4537 36.0001 13C36.0001 15.5463 34.6405 17.7751 32.6077 19"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M4 40.8V42H34V40.8C34 36.3196 34 34.0794 33.1281 32.3681C32.3611 30.8628 31.1372 29.6389 29.6319 28.8719C27.9206 28 25.6804 28 21.2 28H16.8C12.3196 28 10.0794 28 8.36808 28.8719C6.86278 29.6389 5.63893 30.8628 4.87195 32.3681C4 34.0794 4 36.3196 4 40.8Z"
        fill="currentColor"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M43.9999 42.0001V40.8001C43.9999 36.3197 43.9999 34.0795 43.128 32.3682C42.361 30.8629 41.1371 29.6391 39.6318 28.8721"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  ) : (
    <svg {...baseSvg(p)}>
      <path
        d="M19 20C22.866 20 26 16.866 26 13C26 9.13401 22.866 6 19 6C15.134 6 12 9.13401 12 13C12 16.866 15.134 20 19 20Z"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M32.6077 7C34.6405 8.2249 36.0001 10.4537 36.0001 13C36.0001 15.5463 34.6405 17.7751 32.6077 19"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M4 40.8V42H34V40.8C34 36.3196 34 34.0794 33.1281 32.3681C32.3611 30.8628 31.1372 29.6389 29.6319 28.8719C27.9206 28 25.6804 28 21.2 28H16.8C12.3196 28 10.0794 28 8.36808 28.8719C6.86278 29.6389 5.63893 30.8628 4.87195 32.3681C4 34.0794 4 36.3196 4 40.8Z"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M43.9999 42.0001V40.8001C43.9999 36.3197 43.9999 34.0795 43.128 32.3682C42.361 30.8629 41.1371 29.6391 39.6318 28.8721"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );

/** 今天 — 时钟 */
export const IconActivity = ({ active, ...p }: Props) =>
  active ? (
    <svg {...baseSvg(p)}>
      <path
        d="M24 44C35.0457 44 44 35.0457 44 24C44 12.9543 35.0457 4 24 4C12.9543 4 4 12.9543 4 24C4 35.0457 12.9543 44 24 44Z"
        fill="currentColor"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinejoin="round"
      />
      <path
        d="M24.0084 12.0001L24.0072 24.0089L32.4866 32.4883"
        stroke="#fff"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  ) : (
    <svg {...baseSvg(p)}>
      <path
        d="M24 44C35.0457 44 44 35.0457 44 24C44 12.9543 35.0457 4 24 4C12.9543 4 4 12.9543 4 24C4 35.0457 12.9543 44 24 44Z"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinejoin="round"
      />
      <path
        d="M24.0084 12.0001L24.0072 24.0089L32.4866 32.4883"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );

/** 性能 — 折线 + 数据点(仪表风格) */
export const IconPerf = ({ active, ...p }: Props) =>
  active ? (
    <svg {...baseSvg(p)}>
      <rect
        x="6"
        y="6"
        width="36"
        height="36"
        rx="4"
        fill="currentColor"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinejoin="round"
      />
      <path
        d="M12 32L20 22L26 28L36 14"
        stroke="#fff"
        strokeWidth="3"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
      />
      <circle cx="20" cy="22" r="2" fill="#fff" />
      <circle cx="26" cy="28" r="2" fill="#fff" />
      <circle cx="36" cy="14" r="2" fill="#fff" />
    </svg>
  ) : (
    <svg {...baseSvg(p)}>
      <rect
        x="6"
        y="6"
        width="36"
        height="36"
        rx="4"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinejoin="round"
        fill="none"
      />
      <path
        d="M12 32L20 22L26 28L36 14"
        stroke="currentColor"
        strokeWidth="3"
        strokeLinecap="round"
        strokeLinejoin="round"
        fill="none"
      />
      <circle cx="20" cy="22" r="2" fill="currentColor" />
      <circle cx="26" cy="28" r="2" fill="currentColor" />
      <circle cx="36" cy="14" r="2" fill="currentColor" />
    </svg>
  );

/** 用量 — 六边形 + 柱状条 */
export const IconUsage = ({ active, ...p }: Props) =>
  active ? (
    <svg {...baseSvg(p)}>
      <path
        d="M41 13.9997L24 4L7 13.9997V33.9998L24 44L41 33.9998V13.9997Z"
        fill="currentColor"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinejoin="round"
      />
      <path d="M24 22V30" stroke="#fff" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M32 18V30" stroke="#fff" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M16 26V30" stroke="#fff" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  ) : (
    <svg {...baseSvg(p)}>
      <path
        d="M41 13.9997L24 4L7 13.9997V33.9998L24 44L41 33.9998V13.9997Z"
        stroke="currentColor"
        strokeWidth="4"
        strokeLinejoin="round"
      />
      <path d="M24 22V30" stroke="currentColor" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M32 18V30" stroke="currentColor" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M16 26V30" stroke="currentColor" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );

export const IconSettings = ({ active, ...p }: Props) =>
  active ? (
    <svg {...baseSvg(p)}>
      <path
        d="M24 31C27.866 31 31 27.866 31 24C31 20.134 27.866 17 24 17C20.134 17 17 20.134 17 24C17 27.866 20.134 31 24 31Z"
        fill="currentColor"
      />
      <path
        d="M28.6 8.4L26.2 10.2C25.8 10.1 25.4 10 25 10H23C22.6 10 22.2 10.1 21.8 10.2L19.4 8.4C18.6 7.8 17.4 8.2 17.2 9.2L18.2 12.2C17.5 12.9 17 13.8 16.6 14.8L13.4 14.2C12.4 14 11.6 14.8 11.8 15.8L13.2 18.2C13.1 18.6 13 19 13 19.4V21.4C13 21.8 13.1 22.2 13.2 22.6L11.8 25C11.6 26 12.4 26.8 13.4 26.6L16.6 26C17 27 17.5 27.9 18.2 28.6L17.2 31.6C17 32.6 18.2 33.4 19 32.8L21.4 31C21.8 31.1 22.2 31.2 22.6 31.2H24.6C25 31.2 25.4 31.1 25.8 31L28.2 32.8C29 33.4 30.2 32.6 30 31.6L29 28.6C29.7 27.9 30.2 27 30.6 26L33.8 26.6C34.8 26.8 35.6 26 35.4 25L34 22.6C34.1 22.2 34.2 21.8 34.2 21.4V19.4C34.2 19 34.1 18.6 34 18.2L35.4 15.8C35.6 14.8 34.8 14 33.8 14.2L30.6 14.8C30.2 13.8 29.7 12.9 29 12.2L30 9.2C30.2 8.2 29 7.4 28.2 8L28.6 8.4Z"
        stroke="currentColor"
        strokeWidth="3"
        strokeLinejoin="round"
        fill="none"
      />
    </svg>
  ) : (
    <svg {...baseSvg(p)}>
      <path
        d="M28.6 8.4L26.2 10.2C25.8 10.1 25.4 10 25 10H23C22.6 10 22.2 10.1 21.8 10.2L19.4 8.4C18.6 7.8 17.4 8.2 17.2 9.2L18.2 12.2C17.5 12.9 17 13.8 16.6 14.8L13.4 14.2C12.4 14 11.6 14.8 11.8 15.8L13.2 18.2C13.1 18.6 13 19 13 19.4V21.4C13 21.8 13.1 22.2 13.2 22.6L11.8 25C11.6 26 12.4 26.8 13.4 26.6L16.6 26C17 27 17.5 27.9 18.2 28.6L17.2 31.6C17 32.6 18.2 33.4 19 32.8L21.4 31C21.8 31.1 22.2 31.2 22.6 31.2H24.6C25 31.2 25.4 31.1 25.8 31L28.2 32.8C29 33.4 30.2 32.6 30 31.6L29 28.6C29.7 27.9 30.2 27 30.6 26L33.8 26.6C34.8 26.8 35.6 26 35.4 25L34 22.6C34.1 22.2 34.2 21.8 34.2 21.4V19.4C34.2 19 34.1 18.6 34 18.2L35.4 15.8C35.6 14.8 34.8 14 33.8 14.2L30.6 14.8C30.2 13.8 29.7 12.9 29 12.2L30 9.2C30.2 8.2 29 7.4 28.2 8L28.6 8.4Z"
        stroke="currentColor"
        strokeWidth="3"
        strokeLinejoin="round"
        fill="none"
      />
      <circle cx="24" cy="24" r="4" stroke="currentColor" strokeWidth="3" fill="none" />
    </svg>
  );
