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

/** Merge overlapping / adjacent windows.
 *
 * ``wrapAcrossDays``：仅当规则**每天**生效时，才把「当天尾巴」和「次日开头」当成同一
 * 条环形区间缝合。星期受限时，跨午夜段的次日那半在后端归属 yesterday 分支
 * （filter.py camera_schedule_paused），环形缝合会把覆盖整体挪一天——例如
 * 「工作日 22:00-00:00 + 00:00-07:00」缝成「22:00-07:00」后，周一凌晨没了、周六凌晨多了。
 */
export function mergeScheduleWindows(
  windows: Array<{ start: string; end: string }>,
  opts: { wrapAcrossDays?: boolean } = {},
): Array<{ start: string; end: string }> {
  const wrapAcrossDays = opts.wrapAcrossDays ?? true;
  const DAY = 24 * 60;
  const occupied: [number, number][] = [];
  const overnight: Array<{ start: string; end: string }> = [];
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
    if (start < end) {
      occupied.push([start, end]);
    } else if (!wrapAcrossDays) {
      // 星期受限：跨午夜段整条原样保留，不切片、不参与同日合并
      overnight.push({
        start: minuteToScheduleTime(start),
        end: minuteToScheduleTime(end),
      });
    } else {
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
  if (!wrapAcrossDays) {
    return [
      ...merged.map(([start, end]) => ({
        start: minuteToScheduleTime(start),
        end: minuteToScheduleTime(end === DAY ? 0 : end),
      })),
      ...overnight,
    ].sort(
      (a, b) => scheduleMinuteOfDay(a.start) - scheduleMinuteOfDay(b.start),
    );
  }
  // 半开区间无法用单个 HH:MM 表达完整 24h（00:00-00:00 为零长）。
  // 拆成两段相邻半日，后端 _as_day_intervals 对 …-00:00 不再产出 (0,0)。
  if (merged.length === 1 && merged[0][0] === 0 && merged[0][1] === DAY) {
    return [
      { start: "00:00", end: "12:00" },
      { start: "12:00", end: "00:00" },
    ];
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
      end: minuteToScheduleTime(end === DAY ? 0 : end),
    }));
    // merged[0] 恒为 [0, morningEnd] 且 morningEnd >= 1（合并循环已滤零长），
    // 直接拼 eveningStart-morningEnd；minuteToScheduleTime 已覆盖 0 → "00:00"。
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

/** Overlap check under backend semantics: every window is split at midnight first.
 *
 * 星期受限时跨午夜段不参与合并（见 mergeScheduleWindows），但后端的重叠校验仍是
 * 环形切片的——形如「22:00-02:00 + 01:00-03:00（仅周一）」在真实时间里不冲突，
 * 切片后 (0,120) 与 (60,180) 却相压，后端会 400。保存前按同口径预检，给出中文提示。
 */
export function hasCircularOverlap(
  windows: Array<{ start: string; end: string }>,
): boolean {
  const DAY = 24 * 60;
  const intervals: [number, number][] = [];
  for (const window of windows) {
    const start = scheduleMinuteOfDay(window.start);
    const end = scheduleMinuteOfDay(window.end);
    if (Number.isNaN(start) || Number.isNaN(end) || start === end) continue;
    if (start < end) intervals.push([start, end]);
    else {
      intervals.push([start, DAY]);
      if (end > 0) intervals.push([0, end]);
    }
  }
  intervals.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  return intervals.some((curr, i) => i > 0 && curr[0] < intervals[i - 1][1]);
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
