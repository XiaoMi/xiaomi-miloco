import { describe, expect, it } from "vitest";
import {
  isCrossMidnightWindow,
  mergeScheduleWindows,
  normalizeTimeValue,
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

  it("drops zero-length windows", () => {
    expect(
      mergeScheduleWindows([
        { start: "08:00", end: "08:00" },
        { start: "09:00", end: "10:00" },
      ]),
    ).toEqual([{ start: "09:00", end: "10:00" }]);
  });
});

describe("isCrossMidnightWindow", () => {
  it("detects overnight ranges", () => {
    expect(isCrossMidnightWindow({ start: "22:00", end: "07:00" })).toBe(true);
    expect(isCrossMidnightWindow({ start: "08:00", end: "20:00" })).toBe(false);
    expect(isCrossMidnightWindow({ start: "08:00", end: "08:00" })).toBe(false);
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
