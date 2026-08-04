/**
 * 宠物条目归属谓词：保证每条 profile 条目**恰好**出现在一处，且不会失去改/删入口。
 * 分界线：只有外观（member_persona）由宠物档案卡独占承载（卡右上「编辑」能改），
 * 其余宠物条目（作息/健康…）必须留在家庭档案面板里——宠物档案卡是只读展示。
 */
import { describe, expect, it } from "vitest";
import { hostedByPetCard, isPetSubject } from "@/components/HomeProfileParts";
import type { HomeEntry } from "@/lib/types";

function entry(over: Partial<HomeEntry>): HomeEntry {
  return {
    id: "e1",
    type: "member_persona",
    subjectId: "pet_1",
    content: "白色长毛，右耳有黑点",
    confidence: 0.9,
    evidenceCount: 1,
    firstSeen: "2026-01-01T00:00:00Z",
    lastSeen: "2026-01-01T00:00:00Z",
    source: "user_told",
    ...over,
  } as HomeEntry;
}

const roster = [{ id: "pet_1" }];

describe("hostedByPetCard", () => {
  it("宠物外观归宠物档案卡（家庭档案面板排除）", () => {
    expect(hostedByPetCard(entry({}), roster)).toBe(true);
  });

  it("宠物的非外观条目留在家庭档案面板——否则只读的宠物卡会让它改不了也删不掉", () => {
    for (const type of ["member_routine", "member_health", "member_pref"]) {
      expect(
        hostedByPetCard(entry({ type: type as HomeEntry["type"] }), roster),
      ).toBe(false);
    }
  });

  it("人的外观不归宠物卡", () => {
    expect(hostedByPetCard(entry({ subjectId: "person_1" }), roster)).toBe(
      false,
    );
  });

  it("花名册未加载（undefined）时按前缀先当宠物，避免外观在共享信息里闪现", () => {
    expect(hostedByPetCard(entry({}), undefined)).toBe(true);
  });

  it("前缀是 pet_ 但花名册里没有（孤儿条目）→ 留在家庭档案面板，那是唯一清理入口", () => {
    expect(hostedByPetCard(entry({ subjectId: "pet_gone" }), roster)).toBe(
      false,
    );
  });
});

describe("isPetSubject", () => {
  it("纯前缀判定，不查花名册：花名册未到时也要拦住「改派给家人」", () => {
    expect(isPetSubject(entry({ subjectId: "pet_gone" }))).toBe(true);
    expect(isPetSubject(entry({ subjectId: "person_1" }))).toBe(false);
    expect(isPetSubject(entry({ subjectId: null }))).toBe(false);
  });
});
