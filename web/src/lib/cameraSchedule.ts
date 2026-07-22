/** Pure helpers for camera sensing schedule windows (HH:MM, half-open). */

export function scheduleMinuteOfDay(value: string): number {
  const [hour, minute] = value.split(":").map((part) => Number(part));
  return hour * 60 + minute;
}

export function minuteToScheduleTime(minute: number): string {
  const normalized = ((minute % (24 * 60)) + 24 * 60) % (24 * 60);
  const hour = Math.floor(normalized / 60);
  const mins = normalized % 60;
  return `${String(hour).padStart(2, "0")}:${String(mins).padStart(2, "0")}`;
}

export function isCrossMidnightWindow(window: {
  start: string;
  end: string;
}): boolean {
  if (!window.start || !window.end) return false;
  const start = scheduleMinuteOfDay(window.start);
  const end = scheduleMinuteOfDay(window.end);
  return start !== end && start > end;
}

export function scheduleWindowsEqual(
  a: readonly { start: string; end: string }[],
  b: readonly { start: string; end: string }[],
): boolean {
  if (a.length !== b.length) return false;
  return a.every(
    (window, index) =>
      window.start === b[index]?.start && window.end === b[index]?.end,
  );
}

/**
 * HH:MM half-open intervals cannot encode a true end-of-day (24:00) or a
 * full 24h window (00:00-00:00 is zero-length). Use 23:59 as the latest
 * expressible end so backend `_as_day_intervals` never emits a degenerate
 * (0, 0) slice from overnight windows that end at midnight.
 *
 * Trade-off: full-day / until-midnight coverage is [0, 1439); the local
 * minute 23:59 (60s) is outside the window and counts as schedule-paused.
 */
const END_OF_DAY = "23:59";

/** Merge overlapping / adjacent windows; stitch midnight wrap into one overnight window. */
export function mergeScheduleWindows(
  windows: Array<{ start: string; end: string }>,
): Array<{ start: string; end: string }> {
  const DAY = 24 * 60;
  const occupied: [number, number][] = [];
  for (const window of windows) {
    const start = scheduleMinuteOfDay(window.start);
    const end = scheduleMinuteOfDay(window.end);
    if (
      Number.isNaN(start) ||
      Number.isNaN(end) ||
      start === end ||
      start < 0 ||
      end < 0
    ) {
      continue;
    }
    if (start < end) occupied.push([start, end]);
    else {
      occupied.push([start, DAY]);
      // end===0 means "until midnight" with no next-morning slice; skip (0,0).
      if (end > 0) occupied.push([0, end]);
    }
  }
  occupied.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const merged: [number, number][] = [];
  for (const [start, end] of occupied) {
    if (start >= end) continue;
    const last = merged[merged.length - 1];
    if (!last || start > last[1]) merged.push([start, end]);
    else last[1] = Math.max(last[1], end);
  }
  // 半开区间无法用单个 HH:MM 表达完整 24h；用 00:00-23:59 近似全天
  //（缺最后 1 分钟），避免 00:00-00:00 零长，也不引入跨午夜的 (0,0) 退化片。
  if (merged.length === 1 && merged[0][0] === 0 && merged[0][1] === DAY) {
    return [{ start: "00:00", end: END_OF_DAY }];
  }
  if (
    merged.length >= 2 &&
    merged[0][0] === 0 &&
    merged[merged.length - 1][1] === DAY
  ) {
    const morningEnd = merged[0][1];
    const eveningStart = merged[merged.length - 1][0];
    const middle = merged.slice(1, -1);
    const out = middle.map(([start, end]) => ({
      start: minuteToScheduleTime(start),
      end: minuteToScheduleTime(end === DAY ? DAY - 1 : end),
    }));
    if (morningEnd === 0) {
      out.push({
        start: minuteToScheduleTime(eveningStart),
        end: END_OF_DAY,
      });
    } else {
      out.push({
        start: minuteToScheduleTime(eveningStart),
        end: minuteToScheduleTime(morningEnd),
      });
    }
    return out.sort(
      (a, b) => scheduleMinuteOfDay(a.start) - scheduleMinuteOfDay(b.start),
    );
  }
  return merged.map(([start, end]) => ({
    start: minuteToScheduleTime(start),
    end: end === DAY ? END_OF_DAY : minuteToScheduleTime(end),
  }));
}

export function normalizeTimeValue(value: string): string {
  return value.trim().slice(0, 5);
}

export function weekdaysEqual(
  a: readonly number[],
  b: readonly number[],
): boolean {
  if (a.length !== b.length) return false;
  return a.every((value, index) => value === b[index]);
}
