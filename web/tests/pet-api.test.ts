import { afterEach, describe, expect, it, vi } from "vitest";

import {
  realCreatePet,
  realListPets,
  realObservePet,
  realUpdatePet,
  realUploadPetAvatar,
} from "@/api/real";

const originalFetch = globalThis.fetch;

afterEach(() => {
  vi.restoreAllMocks();
  globalThis.fetch = originalFetch;
});

function mockJson(body: unknown, status = 200) {
  globalThis.fetch = vi.fn(
    async () =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
  ) as unknown as typeof fetch;
}

describe("pet api client", () => {
  it("realListPets maps BackendPet[] (avatar_ext → avatarExt)", async () => {
    mockJson({
      code: 0,
      message: "OK",
      data: { pets: [{ id: "pet_a", name: "小黑", species: "猫", avatar_ext: "jpg" }] },
    });
    const pets = await realListPets();
    expect(pets).toEqual([
      {
        id: "pet_a",
        name: "小黑",
        species: "猫",
        avatarExt: "jpg",
        createdAt: undefined,
        updatedAt: undefined,
      },
    ]);
  });

  it("realCreatePet posts and maps null avatar_ext", async () => {
    mockJson({
      code: 0,
      message: "Pet created",
      data: { id: "pet_x", name: "小白", species: "狗", avatar_ext: null },
    });
    const p = await realCreatePet({ name: "小白", species: "狗" });
    expect(p.id).toBe("pet_x");
    expect(p.avatarExt).toBeNull();
  });

  it("realUpdatePet returns mapped pet", async () => {
    mockJson({
      code: 0,
      message: "Pet updated",
      data: { id: "pet_x", name: "旺财", species: "狗" },
    });
    const p = await realUpdatePet("pet_x", { name: "旺财" });
    expect(p.name).toBe("旺财");
  });

  it("realObservePet maps snake_case → camelCase", async () => {
    mockJson({
      code: 0,
      message: "OK",
      data: {
        detected: true,
        description: { species: "猫", summary: "黑猫" },
        head_bbox: [0.1, 0.1, 0.2, 0.2],
        primary_crop_b64: "abc",
        candidates: [
          {
            track_id: 1,
            species_guess: "猫",
            crop_b64: "c1",
            conf: 0.9,
            sharpness: 12.5,
            area_ratio: 0.3,
            bbox: [10, 10, 50, 50],
            frame_idx: 4,
          },
        ],
      },
    });
    const blob = new Blob(["x"], { type: "image/jpeg" });
    const r = await realObservePet(blob, "p.jpg", true);
    expect(r.detected).toBe(true);
    expect(r.primaryCropB64).toBe("abc");
    expect(r.headBbox).toEqual([0.1, 0.1, 0.2, 0.2]);
    // P0 契约质量分随候选映射（snake_case → camelCase）
    expect(r.candidates[0]).toEqual({
      trackId: 1,
      speciesGuess: "猫",
      cropB64: "c1",
      conf: 0.9,
      sharpness: 12.5,
      areaRatio: 0.3,
      bbox: [10, 10, 50, 50],
      frameIdx: 4,
    });
    expect(r.description).toEqual({ species: "猫", summary: "黑猫" });
  });

  it("realUploadPetAvatar returns mapped pet", async () => {
    mockJson({
      code: 0,
      message: "Avatar updated",
      data: { id: "pet_x", name: "小黑", species: "猫", avatar_ext: "png" },
    });
    const blob = new Blob(["img"], { type: "image/png" });
    const p = await realUploadPetAvatar("pet_x", blob, "a.png");
    expect(p.avatarExt).toBe("png");
  });
});
