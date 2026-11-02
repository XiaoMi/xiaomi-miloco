import { describe, expect, it } from "vitest";
import { buildRoomFeedRows } from "@/lib/roomFilter";
import { foldCounts, type BuildFoldRowsInput, type FoldActionLike } from "@/lib/feedFold";

const rooms = { camera: "卧室", lamp: "客厅", other: "卧室" };
const action = (id: string, did: string, success: 0 | 1 = 1): FoldActionLike => ({
  id, did, timestamp: 120, device_name: did, room: null,
  action_type: "set_property", iid: "2.1", value_json: "true",
  success, result_msg: null, error: null, phase: "enter", trigger_event_id: "host",
});
const input = (): BuildFoldRowsInput => ({
  events: [{ id: "host", timestamp: 100, text: "motion", device_ids: ["camera"], snapshot_count: 0 }],
  actions: [action("selected", "lamp"), action("foreign", "other", 0)],
  showEvents: true, showActions: true, strength: "strong", folded: true,
  hasMoreEvents: true,
});

describe("room filter with main feed folding", () => {
  it.each(["strong", "weak"] as const)("filters members before %s folding, retaining their loaded host", (strength) => {
    const rows = buildRoomFeedRows({ ...input(), strength }, "客厅", rooms);
    expect(foldCounts(rows)).toEqual({ events: 1, actions: 1 });
    const host = rows.find((r) => r.kind === "event");
    expect(host?.kind === "event" && host.branches[0].actions.map((a) => a.id)).toEqual(["selected"]);
    expect(host?.kind === "event" && host.branches[0].failed).toBe(false);
  });

  it("keeps counts across unfolded and folded views", () => {
    const rows = buildRoomFeedRows({ ...input(), folded: false }, "客厅", rooms);
    expect(foldCounts(rows)).toEqual({ events: 1, actions: 1 });
    expect(rows.find((r) => r.kind === "action")?.hostRendered).toBe(true);
  });

  it("does not retain foreign hosts when actions are hidden", () => {
    expect(buildRoomFeedRows({ ...input(), showActions: false }, "客厅", rooms)).toEqual([]);
  });

  it("preserves the loaded event horizon even when no event matches the room", () => {
    const old = { ...action("old", "lamp"), timestamp: 90, trigger_event_id: null };
    expect(buildRoomFeedRows({ ...input(), actions: [old] }, "客厅", rooms)).toEqual([]);
  });

  it("keeps all-room members and their failure status", () => {
    const rows = buildRoomFeedRows(input(), "", rooms);
    expect(foldCounts(rows)).toEqual({ events: 1, actions: 2 });
    const host = rows.find((r) => r.kind === "event");
    expect(host?.kind === "event" && host.branches[0].failed).toBe(true);
  });
});
