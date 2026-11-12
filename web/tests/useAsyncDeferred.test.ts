import { beforeEach, describe, expect, it, vi } from "vitest";
const h = vi.hoisted(() => ({ effects: [] as Array<() => unknown>, setters: [] as ReturnType<typeof vi.fn>[] }));
vi.mock("react", () => ({
 useCallback: (fn: unknown) => fn,
 useRef: (value: unknown) => ({ current: value }),
 useState: (value: unknown) => { const setter = vi.fn(); h.setters.push(setter); return [value, setter]; },
 useEffect: (fn: () => unknown) => { h.effects.push(fn); },
}));
vi.mock("@/components/Toast", () => ({ toast: vi.fn() }));
vi.mock("@/i18n", () => ({ default: { t: (s: string) => s } }));
import { useAsync } from "@/hooks/useAsync";
describe("deferred page requests", () => {
 beforeEach(() => { h.effects = []; h.setters = []; });
 it("does not fetch or fabricate empty results when disabled", () => {
  const fetch = vi.fn().mockResolvedValue([]);
  const state = useAsync(fetch, [], { enabled: false });
  h.effects[0]();
  expect(fetch).not.toHaveBeenCalled();
  expect(state.data).toBeUndefined();
  expect(h.setters[0]).not.toHaveBeenCalled();
 });
 it("disabled reload settles without starting a request", async () => {
  const fetch = vi.fn().mockResolvedValue([]);
  const state = useAsync(fetch, [], { enabled: false });
  await state.reload();
  expect(fetch).not.toHaveBeenCalled();
  expect(h.setters[3]).not.toHaveBeenCalled();
 });
 it("retains default fetching behavior", async () => {
  const fetch = vi.fn().mockResolvedValue(["device"]);
  useAsync(fetch);
  h.effects[0]();
  await new Promise(resolve => setTimeout(resolve, 0));
  expect(fetch).toHaveBeenCalledOnce();
  expect(h.setters[0]).toHaveBeenCalledWith(["device"]);
 });
});
