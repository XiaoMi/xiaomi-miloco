/**
 * Activity 页面的房间筛选。
 *
 * 纯前端过滤：事件按 device_ids、动作按 did（缺映射时退回行内 room）、
 * 感知记录按 descriptions 的 key（房间名或 did）。room 为 ALL_ROOMS 时不过滤。
 */
import type { ActivityEvent, PerceptionLogEntry } from "./types";
import { buildFoldRows, feedLowerBound, type BuildFoldRowsInput, type FoldRow } from "./feedFold";

export const ALL_ROOMS = "";

/** did -> 房间名；没有房间信息的设备不进入索引。 */
export function buildRoomIndex(
  devices: ReadonlyArray<{ did: string; room: string }>,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const d of devices) {
    if (d.did && d.room) out[d.did] = d.room;
  }
  return out;
}

/** 去重并排序的房间列表，用于下拉选项。 */
export function listRooms(roomOf: Record<string, string>): string[] {
  return Array.from(new Set(Object.values(roomOf))).sort((a, b) =>
    a.localeCompare(b, "zh"),
  );
}

export function eventMatchesRoom(
  event: Pick<ActivityEvent, "device_ids">,
  room: string,
  roomOf: Record<string, string>,
): boolean {
  if (room === ALL_ROOMS) return true;
  return event.device_ids.some((did) => roomOf[did] === room);
}

export function actionMatchesRoom(
  action: { did: string; room: string | null },
  room: string,
  roomOf: Record<string, string>,
): boolean {
  if (room === ALL_ROOMS) return true;
  return (roomOf[action.did] ?? action.room) === room;
}

/** 房间先筛成员再折叠；保留所选动作已加载的宿主，避免误报为宿主未加载。
 * 分页地平线仍由未筛选的事件决定，不能因换房间而放出尚未加载区间的动作。 */
export function buildRoomFeedRows(
  input: BuildFoldRowsInput,
  room: string,
  roomOf: Record<string, string>,
): FoldRow[] {
  if (room === ALL_ROOMS) return buildFoldRows(input);
  const actions = input.actions.filter((a) => actionMatchesRoom(a, room, roomOf));
  const hosts = new Set(input.showActions ? actions.map((a) => a.trigger_event_id) : []);
  const events = input.events.filter((e) => eventMatchesRoom(e, room, roomOf) || hosts.has(e.id));
  return buildFoldRows({
    ...input,
    events,
    actions,
    sinceMs: feedLowerBound(input.events, input.showEvents, input.sinceMs, input.hasMoreEvents),
    hasMoreEvents: false,
  });
}

/** 只保留属于该房间的来源描述；一条记录的来源都不属于该房间时整条去掉。 */
export function filterPerceptionLogs(
  logs: PerceptionLogEntry[],
  room: string,
  roomOf: Record<string, string>,
): PerceptionLogEntry[] {
  if (room === ALL_ROOMS) return logs;
  const out: PerceptionLogEntry[] = [];
  for (const log of logs) {
    // 感知记录的 key 可能是房间名（按房间聚合）也可能是 did，两种都认。
    const entries = Object.entries(log.descriptions).filter(
      ([source]) => source === room || roomOf[source] === room,
    );
    if (entries.length > 0) {
      out.push({ ...log, descriptions: Object.fromEntries(entries) });
    }
  }
  return out;
}
