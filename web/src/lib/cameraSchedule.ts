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
      occupied.push([0, end]);
    }
  }
  occupied.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const merged: [number, number][] = [];
  for (const [start, end] of occupied) {
    const last = merged[merged.length - 1];
    if (!last || start > last[1]) merged.push([start, end]);
    else last[1] = Math.max(last[1], end);
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
      end: minuteToScheduleTime(end),
    }));
    out.push({
      start: minuteToScheduleTime(eveningStart),
      end: minuteToScheduleTime(morningEnd),
    });
    return out.sort(
      (a, b) => scheduleMinuteOfDay(a.start) - scheduleMinuteOfDay(b.start),
    );
  }
  return merged.map(([start, end]) => ({
    start: minuteToScheduleTime(start),
    end: minuteToScheduleTime(end === DAY ? 0 : end),
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
