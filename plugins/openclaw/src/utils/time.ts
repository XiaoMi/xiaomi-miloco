/**
 * 部署时区视角的本地时间 helper。
 *
 * 时区来源优先级（deployTimezone,与 backend deploy_timezone 单一真源对齐）:
 *   1. 显式传入的 tz 参数（IANA 名,如 "America/Los_Angeles"）
 *   2. 环境变量 MILOCO_TIMEZONE（IANA 名；backend pydantic env 优先级同样最高）
 *   3. backend config.json 的 `timezone`（用户可编辑的权威设置,== MilocoSettings.timezone）
 *   4. 系统时区（Intl.DateTimeFormat().resolvedOptions().timeZone）
 *   5. 兜底 Asia/Shanghai（开发机无系统时区时也能跑）
 *
 * 加入第 3 步是为了让插件与 backend 认到同一个"部署时区"：backend settings 以
 * env > config.json 的顺序解析 timezone,插件读同一份 config.json 保持一致,避免
 * 只凭 env/系统时区在宿主机 TZ≠部署时区时给 agent 注入错误的时段。
 *
 * 除 deployTimezone 读一次 config.json 外,toLocalParts / nowLocalIso 在显式传 tz
 * 时仍是纯函数,无 IO。用 Intl.DateTimeFormat 拿 IANA tz-aware parts,正确处理 DST。
 */

import { milocoConfigFile } from "../miloco/paths.js";
import { readJsonFileSync } from "./io.js";

const FALLBACK_TZ = "Asia/Shanghai";

const WEEKDAY_TO_MON1: Record<string, number> = {
  Mon: 1,
  Tue: 2,
  Wed: 3,
  Thu: 4,
  Fri: 5,
  Sat: 6,
  Sun: 7,
};

/** config.json 里的 `timezone`（用户权威设置）；缺失 / 非法 / 空串 → undefined。 */
function timezoneFromConfig(): string | undefined {
  const cfg = readJsonFileSync<{ timezone?: unknown }>(milocoConfigFile());
  const tz = cfg?.timezone;
  return typeof tz === "string" && tz.length > 0 ? tz : undefined;
}

/** 部署时区。优先 env > config.json > 系统时区,兜底 Asia/Shanghai。 */
export function deployTimezone(): string {
  const fromEnv = process.env.MILOCO_TIMEZONE;
  if (fromEnv) return fromEnv;
  const fromConfig = timezoneFromConfig();
  if (fromConfig) return fromConfig;
  const fromSystem = Intl.DateTimeFormat().resolvedOptions().timeZone;
  if (fromSystem) return fromSystem;
  return FALLBACK_TZ;
}

export type LocalParts = {
  y: number;
  m: number; // 1-12
  d: number; // 1-31
  h: number; // 0-23
  mi: number;
  s: number;
  dayMon1: number; // 1=Mon ... 7=Sun
};

/** ISO 字符串 → 部署时区视角 parts。 */
export function toLocalParts(iso: string, tz?: string): LocalParts | null {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return null;
  const zone = tz ?? deployTimezone();
  const fmt = new Intl.DateTimeFormat("en-US", {
    timeZone: zone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    weekday: "short",
  });
  const parts: Record<string, string> = {};
  for (const p of fmt.formatToParts(new Date(ms))) {
    parts[p.type] = p.value;
  }
  // hour12=false 下,Node 在午夜会返回 "24",规整到 0-23
  const h = Number(parts.hour) % 24;
  const dayMon1 = WEEKDAY_TO_MON1[parts.weekday];
  if (dayMon1 === undefined) return null;
  return {
    y: Number(parts.year),
    m: Number(parts.month),
    d: Number(parts.day),
    h,
    mi: Number(parts.minute),
    s: Number(parts.second),
    dayMon1,
  };
}

/** 部署时区在给定 Date 的偏移字符串 "+08:00" / "-08:00" / "+00:00"。 */
function tzOffsetString(date: Date, tz: string): string {
  const fmt = new Intl.DateTimeFormat("en-US", {
    timeZone: tz,
    timeZoneName: "longOffset",
  });
  const part = fmt
    .formatToParts(date)
    .find((p) => p.type === "timeZoneName");
  if (!part) return "+00:00";
  // longOffset 形如 "GMT+08:00" / "GMT-05:00" / "GMT"
  const m = /GMT([+-]\d{1,2}(?::\d{2})?)?/.exec(part.value);
  if (!m || !m[1]) return "+00:00";
  let o = m[1];
  if (!o.includes(":")) o += ":00";
  // "+8:00" → "+08:00"
  if (/^[+-]\d:/.test(o)) o = o[0] + "0" + o.slice(1);
  return o;
}

/** 当前时刻的部署时区 ISO 字符串,后缀带动态偏移(如 "+08:00" / "-05:00")。 */
export function nowLocalIso(tz?: string): string {
  const zone = tz ?? deployTimezone();
  const now = new Date();
  const p = toLocalParts(now.toISOString(), zone);
  if (!p) {
    throw new Error("nowLocalIso: failed to format current Date");
  }
  const offset = tzOffsetString(now, zone);
  const pad2 = (n: number) => String(n).padStart(2, "0");
  const pad4 = (n: number) => String(n).padStart(4, "0");
  return `${pad4(p.y)}-${pad2(p.m)}-${pad2(p.d)}T${pad2(p.h)}:${pad2(p.mi)}:${pad2(p.s)}${offset}`;
}
