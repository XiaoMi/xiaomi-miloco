/**
 * 设备台账（did / 名称 / 房间）的本地缓存。
 *
 * Activity 的房间下拉与设备名映射先用上次成功拉到的台账渲染，避免 /api/miot/home
 * 偶尔变慢时下拉框在页面加载完后才突然出现；拉取成功后再用新数据替换缓存。
 * 只存 did / name / room，不存设备状态或任何凭据。
 */
import type { DeviceDirectoryEntry } from "@/api/real";

export const DEVICE_DIRECTORY_KEY_PREFIX = "web:deviceDirectory:";

function isEntry(v: unknown): v is DeviceDirectoryEntry {
  if (!v || typeof v !== "object") return false;
  const e = v as Record<string, unknown>;
  return typeof e.did === "string" && typeof e.name === "string" && typeof e.room === "string";
}

export function readDeviceDirectory(homeId: string): DeviceDirectoryEntry[] | undefined {
  try {
    const raw = localStorage.getItem(DEVICE_DIRECTORY_KEY_PREFIX + homeId);
    if (!raw) return undefined;
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed) || !parsed.every(isEntry)) return undefined;
    return parsed;
  } catch {
    return undefined; // 缓存损坏或 localStorage 不可用：当作没有缓存
  }
}

export function saveDeviceDirectory(homeId: string, entries: DeviceDirectoryEntry[]): void {
  try {
    const slim = entries.map(({ did, name, room }) => ({ did, name, room }));
    localStorage.setItem(DEVICE_DIRECTORY_KEY_PREFIX + homeId, JSON.stringify(slim));
  } catch {
    /* 写不了就只在本会话生效 */
  }
}

/** 换家或换米家账号前调用：整页刷新后不能拿上一个家的台账先渲染，退回"等 fetch"。 */
export function clearDeviceDirectory(homeId: string): void {
  try {
    localStorage.removeItem(DEVICE_DIRECTORY_KEY_PREFIX + homeId);
  } catch {
    /* 删不掉最多是 reload 后多渲染一次旧台账，拉取成功后即被替换 */
  }
}
