import { describe, expect, it } from "vitest";
import {
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

  it("maps until-midnight same-day tails to 23:59 (no overnight 00:00)", () => {
    expect(
      mergeScheduleWindows([{ start: "22:00", end: "00:00" }]),
    ).toEqual([{ start: "22:00", end: "23:59" }]);
  });

  it("drops zero-length windows", () => {
    expect(
      mergeScheduleWindows([
        { start: "08:00", end: "08:00" },
        { start: "09:00", end: "10:00" },
      ]),
    ).toEqual([{ start: "09:00", end: "10:00" }]);
  });

  it("encodes full-day coverage as 00:00-23:59 (not zero-length / no (0,0))", () => {
    expect(
      mergeScheduleWindows([
        { start: "08:00", end: "20:00" },
        { start: "20:00", end: "08:00" },
      ]),
    ).toEqual([{ start: "00:00", end: "23:59" }]);
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
    ).toEqual([{ start: "00:00", end: "23:59" }]);
  });
});

describe("isCrossMidnightWindow", () => {
  it("detects overnight ranges", () => {
    expect(isCrossMidnightWindow({ start: "22:00", end: "07:00" })).toBe(true);
    expect(isCrossMidnightWindow({ start: "08:00", end: "20:00" })).toBe(false);
    expect(isCrossMidnightWindow({ start: "08:00", end: "08:00" })).toBe(false);
    expect(isCrossMidnightWindow({ start: "22:00", end: "23:59" })).toBe(false);
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
