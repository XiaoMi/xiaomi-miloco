import { describe, expect, it } from "vitest";
import { createConfigResponseGate } from "@/lib/configResponseGate";
describe("configuration response ordering", () => {
  it("rejects a delayed read after saving a rename", async () => {
    const gate = createConfigResponseGate();
    let resolve!: (value: string) => void;
    const old = new Promise<string>(done => { resolve = done; });
    const ticket = gate.beginRead();
    let label = "2.0";
    const pending = old.then(value => { if (gate.isCurrent(ticket)) label = value; });
    gate.invalidate();
    label = "2.1";
    resolve("2.0");
    await pending;
    expect(label).toBe("2.1");
  });
  it("rejects older reads and their errors", () => {
    const gate = createConfigResponseGate();
    const older = gate.beginRead();
    const current = gate.beginRead();
    expect(gate.isCurrent(older)).toBe(false);
    expect(gate.isCurrent(current)).toBe(true);
  });
  it("invalidates pending reads on unmount", () => {
    const gate = createConfigResponseGate();
    const older = gate.beginRead();
    gate.invalidate();
    expect(gate.isCurrent(older)).toBe(false);
    expect(gate.isCurrent(gate.beginRead())).toBe(true);
  });
});
