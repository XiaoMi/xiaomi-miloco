import { describe, expect, it } from "vitest";
import {
  hasCircularOverlap,
  isCrossMidnightWindow,
  mergeScheduleWindows,
  normalizeTimeValue,
  scheduleWindowsEqual,
  weekdaysEqual,
} from "@/lib/cameraSchedule";

describe("mergeScheduleWindows", () => {
  it("merges overlapping same-day windows", () => {
    expect(
      mergeScheduleWindows([
        { start: "08:00", end: "12:00" },
        { start: "11:00", end: "14:00" },
      ]),
    ).toEqual([{ start: "08:00", end: "14:00" }]);
  });

  it("merges adjacent windows", () => {
    expect(
      mergeScheduleWindows([
        { start: "08:00", end: "10:00" },
        { start: "10:00", end: "12:00" },
      ]),
    ).toEqual([{ start: "08:00", end: "12:00" }]);
  });

  it("stitches overnight wrap into one cross-midnight window", () => {
    expect(
      mergeScheduleWindows([
        { start: "22:00", end: "00:00" },
        { start: "00:00", end: "07:00" },
      ]),
    ).toEqual([{ start: "22:00", end: "07:00" }]);
  });

  it("keeps a single overnight window as-is", () => {
    expect(
      mergeScheduleWindows([{ start: "22:00", end: "07:00" }]),
    ).toEqual([{ start: "22:00", end: "07:00" }]);
  });

  it("keeps until-midnight tails as 00:00", () => {
    expect(
      mergeScheduleWindows([{ start: "22:00", end: "00:00" }]),
    ).toEqual([{ start: "22:00", end: "00:00" }]);
  });

  it("drops zero-length windows", () => {
    expect(
      mergeScheduleWindows([
        { start: "08:00", end: "08:00" },
        { start: "09:00", end: "10:00" },
      ]),
    ).toEqual([{ start: "09:00", end: "10:00" }]);
  });

  it("encodes full-day coverage as two abutting halves ending at 00:00", () => {
    expect(
      mergeScheduleWindows([
        { start: "08:00", end: "20:00" },
        { start: "20:00", end: "08:00" },
      ]),
    ).toEqual([
      { start: "00:00", end: "12:00" },
      { start: "12:00", end: "00:00" },
    ]);
  });

  it("encodes stacked windows that fill the day the same way", () => {
    expect(
      mergeScheduleWindows([{ start: "00:00", end: "00:00" }]),
    ).toEqual([]);
    expect(
      mergeScheduleWindows([
        { start: "00:00", end: "06:00" },
        { start: "06:00", end: "18:00" },
        { start: "18:00", end: "00:00" },
      ]),
    ).toEqual([
      { start: "00:00", end: "12:00" },
      { start: "12:00", end: "00:00" },
    ]);
  });

  it("does not stitch across midnight when weekdays are restricted", () => {
    // 后端把跨午夜段的次日那半归属 yesterday；星期受限时缝合会把覆盖挪一天。
    expect(
      mergeScheduleWindows(
        [
          { start: "22:00", end: "00:00" },
          { start: "00:00", end: "07:00" },
        ],
        { wrapAcrossDays: false },
      ),
    ).toEqual([
      { start: "00:00", end: "07:00" },
      { start: "22:00", end: "00:00" },
    ]);
  });

  it("keeps overnight windows verbatim when weekdays are restricted", () => {
    // 08:00-20:00 + 20:00-08:00 不再被改写成「全天两段」，用户填的跨午夜段原样保留。
    expect(
      mergeScheduleWindows(
        [
          { start: "08:00", end: "20:00" },
          { start: "20:00", end: "08:00" },
        ],
        { wrapAcrossDays: false },
      ),
    ).toEqual([
      { start: "08:00", end: "20:00" },
      { start: "20:00", end: "08:00" },
    ]);
  });

  it("still merges same-day overlaps when weekdays are restricted", () => {
    expect(
      mergeScheduleWindows(
        [
          { start: "08:00", end: "12:00" },
          { start: "11:00", end: "14:00" },
        ],
        { wrapAcrossDays: false },
      ),
    ).toEqual([{ start: "08:00", end: "14:00" }]);
  });
});

describe("hasCircularOverlap", () => {
  it("flags overnight tail pressing a same-day window", () => {
    // 22:00-02:00 的次日片 (0,120) 与 01:00-03:00 的 (60,180) 相压。
    expect(
      hasCircularOverlap([
        { start: "22:00", end: "02:00" },
        { start: "01:00", end: "03:00" },
      ]),
    ).toBe(true);
  });

  it("accepts disjoint and abutting windows", () => {
    expect(
      hasCircularOverlap([
        { start: "22:00", end: "00:00" },
        { start: "00:00", end: "07:00" },
      ]),
    ).toBe(false);
    expect(
      hasCircularOverlap([
        { start: "08:00", end: "12:00" },
        { start: "12:00", end: "20:00" },
      ]),
    ).toBe(false);
  });
});

describe("isCrossMidnightWindow", () => {
  it("detects overnight ranges", () => {
    expect(isCrossMidnightWindow({ start: "22:00", end: "07:00" })).toBe(true);
    expect(isCrossMidnightWindow({ start: "08:00", end: "20:00" })).toBe(false);
    expect(isCrossMidnightWindow({ start: "08:00", end: "08:00" })).toBe(false);
    expect(isCrossMidnightWindow({ start: "22:00", end: "00:00" })).toBe(true);
  });
});

describe("scheduleWindowsEqual", () => {
  it("compares window lists by value", () => {
    const a = [{ start: "08:00", end: "20:00" }];
    expect(scheduleWindowsEqual(a, [{ start: "08:00", end: "20:00" }])).toBe(
      true,
    );
    expect(scheduleWindowsEqual(a, a)).toBe(true);
    expect(
      scheduleWindowsEqual(a, [{ start: "08:00", end: "21:00" }]),
    ).toBe(false);
    expect(scheduleWindowsEqual(a, [])).toBe(false);
  });
});

describe("normalizeTimeValue / weekdaysEqual", () => {
  it("truncates seconds", () => {
    expect(normalizeTimeValue("08:30:45")).toBe("08:30");
  });

  it("compares weekday lists", () => {
    expect(weekdaysEqual([0, 1, 2], [0, 1, 2])).toBe(true);
    expect(weekdaysEqual([0, 1], [0, 1, 2])).toBe(false);
  });
});
