import { beforeEach, describe, expect, it, vi } from "vitest";

const milocoHomeMock = vi.fn(() => "/fake/miloco-home");
const readJsonFileSyncMock = vi.fn();
const writeJsonFileSyncMock = vi.fn();

vi.mock("../src/miloco/paths.js", () => ({
  milocoHome: () => milocoHomeMock(),
}));

vi.mock("../src/utils/io.js", () => ({
  readJsonFileSync: (...args: unknown[]) => readJsonFileSyncMock(...args),
  writeJsonFileSync: (...args: unknown[]) => writeJsonFileSyncMock(...args),
}));

describe("onboarding session lock state", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
  });

  it("写入邀请状态后可以读回", async () => {
    const {
      readOnboardingState,
      writeOnboardingInviteState,
    } = await import("../src/home-profile/onboarding_state.js");
    const nowMs = Date.parse("2026-07-10T00:00:00.000Z");

    writeOnboardingInviteState(["wechat:a", "wechat:a", "telegram:b"], nowMs);

    expect(writeJsonFileSyncMock).toHaveBeenCalledTimes(1);
    const [path, payload, options] = writeJsonFileSyncMock.mock.calls[0];
    expect(String(path)).toMatch(/[\\/]home-profile[\\/]onboarding-session-lock\.json$/);
    expect(options).toEqual({ pretty: true });
    expect(payload).toMatchObject({
      invitedSessionKeys: ["wechat:a", "telegram:b"],
      invitedAt: "2026-07-10T00:00:00.000Z",
      expiresAt: "2026-07-10T01:00:00.000Z",
    });

    readJsonFileSyncMock.mockReturnValue(payload);
    expect(readOnboardingState(nowMs)).toEqual(payload);
  });

  it("首个会话回复后写入 lockedSessionKey", async () => {
    const { lockOnboardingSession } = await import(
      "../src/home-profile/onboarding_state.js"
    );
    const nowMs = Date.parse("2026-07-10T01:00:00.000Z");
    readJsonFileSyncMock.mockReturnValue({
      invitedSessionKeys: ["wechat:a", "telegram:b"],
      invitedAt: "2026-07-10T00:00:00.000Z",
      expiresAt: "2026-07-10T02:00:00.000Z",
    });

    const result = lockOnboardingSession("telegram:b", nowMs);

    expect(result).toMatchObject({
      invitedSessionKeys: ["wechat:a", "telegram:b"],
      lockedSessionKey: "telegram:b",
      lockedAt: "2026-07-10T01:00:00.000Z",
    });
    expect(writeJsonFileSyncMock).toHaveBeenCalledTimes(1);
    expect(writeJsonFileSyncMock.mock.calls[0][1]).toMatchObject({
      lockedSessionKey: "telegram:b",
    });
  });

  it("已锁定到别处时不会被后续会话覆盖", async () => {
    const { lockOnboardingSession } = await import(
      "../src/home-profile/onboarding_state.js"
    );
    const lockedState = {
      invitedSessionKeys: ["wechat:a", "telegram:b"],
      invitedAt: "2026-07-10T00:00:00.000Z",
      expiresAt: "2026-07-10T02:00:00.000Z",
      lockedSessionKey: "wechat:a",
      lockedAt: "2026-07-10T00:10:00.000Z",
    };
    readJsonFileSyncMock.mockReturnValue(lockedState);

    const result = lockOnboardingSession(
      "telegram:b",
      Date.parse("2026-07-10T01:00:00.000Z"),
    );

    expect(result).toEqual(lockedState);
    expect(writeJsonFileSyncMock).not.toHaveBeenCalled();
  });

  it("过期后返回 null 并清空状态", async () => {
    const { readOnboardingState } = await import(
      "../src/home-profile/onboarding_state.js"
    );
    const nowMs = Date.parse("2026-07-10T01:00:00.001Z");
    readJsonFileSyncMock.mockReturnValue({
      invitedSessionKeys: ["wechat:a"],
      invitedAt: "2026-07-10T00:00:00.000Z",
      expiresAt: "2026-07-10T01:00:00.000Z",
    });

    const result = readOnboardingState(nowMs);

    expect(result).toBeNull();
    expect(String(writeJsonFileSyncMock.mock.calls[0][0])).toMatch(
      /[\\/]home-profile[\\/]onboarding-session-lock\.json$/,
    );
    expect(writeJsonFileSyncMock.mock.calls[0][1]).toEqual({});
    expect(writeJsonFileSyncMock.mock.calls[0][2]).toEqual({ pretty: true });
  });
});
