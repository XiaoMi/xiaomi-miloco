import { describe, it, expect } from "vitest";
import {
  ALL_ROOMS,
  actionMatchesRoom,
  buildRoomIndex,
  eventMatchesRoom,
  filterPerceptionLogs,
  listRooms,
} from "@/lib/roomFilter";
import type { PerceptionLogEntry } from "@/lib/types";

const roomOf = buildRoomIndex([
  { did: "cam1", room: "客厅" },
  { did: "cam2", room: "卧室" },
  { did: "lamp", room: "客厅" },
  { did: "noroom", room: "" },
]);

describe("roomFilter", () => {
  it("builds index and sorted unique rooms", () => {
    expect(roomOf).toEqual({ cam1: "客厅", cam2: "卧室", lamp: "客厅" });
    expect(listRooms(roomOf)).toEqual(["客厅", "卧室"].sort((a, b) => a.localeCompare(b, "zh")));
  });

  it("all rooms keeps everything", () => {
    expect(eventMatchesRoom({ device_ids: [] }, ALL_ROOMS, roomOf)).toBe(true);
    expect(actionMatchesRoom({ did: "x", room: null }, ALL_ROOMS, roomOf)).toBe(true);
  });

  it("matches events by any device in the room", () => {
    expect(eventMatchesRoom({ device_ids: ["cam2", "cam1"] }, "客厅", roomOf)).toBe(true);
    expect(eventMatchesRoom({ device_ids: ["cam2"] }, "客厅", roomOf)).toBe(false);
    expect(eventMatchesRoom({ device_ids: [] }, "客厅", roomOf)).toBe(false);
  });

  it("matches actions by did, falling back to row room", () => {
    expect(actionMatchesRoom({ did: "lamp", room: "卧室" }, "客厅", roomOf)).toBe(true);
    expect(actionMatchesRoom({ did: "unknown", room: "卧室" }, "卧室", roomOf)).toBe(true);
    expect(actionMatchesRoom({ did: "unknown", room: null }, "卧室", roomOf)).toBe(false);
  });

  it("filters perception descriptions per source and drops empty rows", () => {
    const logs: PerceptionLogEntry[] = [
      { id: "1", timestamp: 1, descriptions: { cam1: "a", cam2: "b" } },
      { id: "2", timestamp: 2, descriptions: { cam2: "c" } },
    ];
    expect(filterPerceptionLogs(logs, "客厅", roomOf)).toEqual([
      { id: "1", timestamp: 1, descriptions: { cam1: "a" } },
    ]);
    expect(filterPerceptionLogs(logs, ALL_ROOMS, roomOf)).toBe(logs);
  });

  it("accepts perception keys that are room names", () => {
    const logs: PerceptionLogEntry[] = [
      { id: "1", timestamp: 1, descriptions: { 客厅: "a", 卧室: "b" } },
      { id: "2", timestamp: 2, descriptions: { 阳台: "c" } },
    ];
    expect(filterPerceptionLogs(logs, "卧室", roomOf)).toEqual([
      { id: "1", timestamp: 1, descriptions: { 卧室: "b" } },
    ]);
  });
});
