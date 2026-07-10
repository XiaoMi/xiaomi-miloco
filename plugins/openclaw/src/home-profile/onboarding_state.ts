import path from "node:path";
import { milocoHome } from "../miloco/paths.js";
import { readJsonFileSync, writeJsonFileSync } from "../utils/io.js";

const ONBOARDING_LOCK_TTL_MS = 12 * 60 * 60 * 1000;

type OnboardingState = {
  invitedSessionKeys: string[];
  invitedAt: string;
  expiresAt: string;
  lockedSessionKey?: string;
  lockedAt?: string;
};

function onboardingStatePath(): string {
  return path.join(milocoHome(), "home-profile", "onboarding-session-lock.json");
}

function nowIso(nowMs = Date.now()): string {
  return new Date(nowMs).toISOString();
}

export function readOnboardingState(nowMs = Date.now()): OnboardingState | null {
  const state = readJsonFileSync<OnboardingState>(onboardingStatePath());
  if (!state || !Array.isArray(state.invitedSessionKeys) || !state.expiresAt) {
    return null;
  }
  if (Date.parse(state.expiresAt) <= nowMs) {
    clearOnboardingState();
    return null;
  }
  return state;
}

export function writeOnboardingInviteState(
  invitedSessionKeys: string[],
  nowMs = Date.now(),
): void {
  const unique = invitedSessionKeys.filter(
    (key, idx, arr) => typeof key === "string" && key && arr.indexOf(key) === idx,
  );
  if (unique.length === 0) return;
  writeJsonFileSync(
    onboardingStatePath(),
    {
      invitedSessionKeys: unique,
      invitedAt: nowIso(nowMs),
      expiresAt: nowIso(nowMs + ONBOARDING_LOCK_TTL_MS),
    } satisfies OnboardingState,
    { pretty: true },
  );
}

export function lockOnboardingSession(
  sessionKey: string,
  nowMs = Date.now(),
): OnboardingState | null {
  const state = readOnboardingState(nowMs);
  if (!state) return null;
  if (!state.invitedSessionKeys.includes(sessionKey)) return state;
  if (state.lockedSessionKey === sessionKey) return state;
  if (!state.lockedSessionKey) {
    const next: OnboardingState = {
      ...state,
      lockedSessionKey: sessionKey,
      lockedAt: nowIso(nowMs),
    };
    writeJsonFileSync(onboardingStatePath(), next, { pretty: true });
    return next;
  }
  return state;
}

export function clearOnboardingState(): void {
  writeJsonFileSync(onboardingStatePath(), {}, { pretty: true });
}
