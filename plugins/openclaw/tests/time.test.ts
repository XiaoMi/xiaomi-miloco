/**
 * time.ts 单测：toLocalParts / nowLocalIso / deployTimezone。
 *
 * 多时区支持：通过 tz 显式参数测试 IANA 时区,与 MILOCO_TIMEZONE env 解耦。
 */

import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  deployTimezone,
  nowLocalIso,
  toLocalParts,
} from "../src/utils/time.js";

describe("toLocalParts (Asia/Shanghai)", () => {
  const tz = "Asia/Shanghai";

  it("parses ISO with +08:00", () => {
    const p = toLocalParts("2026-05-14T19:30:00+08:00", tz);
    expect(p).toEqual({
      y: 2026,
      m: 5,
      d: 14,
      h: 19,
      mi: 30,
      s: 0,
      dayMon1: 4, // 2026-05-14 is Thursday
    });
  });

  it("converts UTC to +08:00 view", () => {
    const p = toLocalParts("2026-05-14T02:00:00Z", tz);
    expect(p).toEqual({
      y: 2026,
      m: 5,
      d: 14,
      h: 10,
      mi: 0,
      s: 0,
      dayMon1: 4,
    });
  });

  it("crosses midnight when UTC near-day-end", () => {
    const p = toLocalParts("2026-05-13T17:00:00Z", tz);
    expect(p).toEqual({
      y: 2026,
      m: 5,
      d: 14,
      h: 1,
      mi: 0,
      s: 0,
      dayMon1: 4,
    });
  });

  it("identifies Sunday correctly (dayMon1=7)", () => {
    // 2026-05-17 is Sunday
    const p = toLocalParts("2026-05-17T10:00:00+08:00", tz);
    expect(p?.dayMon1).toBe(7);
  });

  it("returns null for invalid ISO", () => {
    expect(toLocalParts("abc", tz)).toBeNull();
    expect(toLocalParts("", tz)).toBeNull();
  });
});

describe("toLocalParts (cross-timezone)", () => {
  it("UTC vs Asia/Shanghai 相差 8h", () => {
    const utc = toLocalParts("2026-06-16T12:00:00Z", "UTC");
    const sh = toLocalParts("2026-06-16T12:00:00Z", "Asia/Shanghai");
    expect(utc?.h).toBe(12);
    expect(sh?.h).toBe(20);
  });

  it("America/Los_Angeles 在夏令时(PDT, -07:00)", () => {
    // 2026-07-15 美西夏令时
    const la = toLocalParts("2026-07-15T19:00:00Z", "America/Los_Angeles");
    expect(la).toMatchObject({ y: 2026, m: 7, d: 15, h: 12 });
  });

  it("America/Los_Angeles 在标准时(PST, -08:00)", () => {
    // 2026-01-15 美西标准时
    const la = toLocalParts("2026-01-15T20:00:00Z", "America/Los_Angeles");
    expect(la).toMatchObject({ y: 2026, m: 1, d: 15, h: 12 });
  });
});

describe("deployTimezone", () => {
  const prevEnv = process.env.MILOCO_TIMEZONE;
  afterEach(() => {
    if (prevEnv === undefined) delete process.env.MILOCO_TIMEZONE;
    else process.env.MILOCO_TIMEZONE = prevEnv;
  });

  it("MILOCO_TIMEZONE 优先", () => {
    process.env.MILOCO_TIMEZONE = "America/Los_Angeles";
    expect(deployTimezone()).toBe("America/Los_Angeles");
  });

  it("未配置 env 走系统时区(非空)", () => {
    delete process.env.MILOCO_TIMEZONE;
    const tz = deployTimezone();
    expect(tz).toBeTruthy();
    expect(typeof tz).toBe("string");
  });
});

describe("deployTimezone: config.json 单一真源", () => {
  // 隔离 MILOCO_HOME 到临时目录,避免读到开发机真实 ~/.openclaw/miloco/config.json。
  let tmpHome: string;
  const prevEnv = process.env.MILOCO_TIMEZONE;
  const prevHome = process.env.MILOCO_HOME;

  beforeEach(() => {
    tmpHome = mkdtempSync(path.join(tmpdir(), "miloco-tz-"));
    process.env.MILOCO_HOME = tmpHome;
    delete process.env.MILOCO_TIMEZONE;
  });

  afterEach(() => {
    if (prevEnv === undefined) delete process.env.MILOCO_TIMEZONE;
    else process.env.MILOCO_TIMEZONE = prevEnv;
    if (prevHome === undefined) delete process.env.MILOCO_HOME;
    else process.env.MILOCO_HOME = prevHome;
    rmSync(tmpHome, { recursive: true, force: true });
  });

  it("无 env 时读 config.json 的 timezone（与 backend settings 同源）", () => {
    writeFileSync(
      path.join(tmpHome, "config.json"),
      JSON.stringify({ timezone: "America/Los_Angeles" }),
      "utf8",
    );
    expect(deployTimezone()).toBe("America/Los_Angeles");
  });

  it("env 优先于 config.json（对齐 backend pydantic env > file）", () => {
    writeFileSync(
      path.join(tmpHome, "config.json"),
      JSON.stringify({ timezone: "America/Los_Angeles" }),
      "utf8",
    );
    process.env.MILOCO_TIMEZONE = "Europe/London";
    expect(deployTimezone()).toBe("Europe/London");
  });

  it("config.json 无 timezone → 精确落回 Intl 系统时区", () => {
    writeFileSync(
      path.join(tmpHome, "config.json"),
      JSON.stringify({ debug: true }),
      "utf8",
    );
    expect(deployTimezone()).toBe(
      Intl.DateTimeFormat().resolvedOptions().timeZone,
    );
  });
});

describe("nowLocalIso", () => {
  const prevEnv = process.env.MILOCO_TIMEZONE;
  afterEach(() => {
    if (prevEnv === undefined) delete process.env.MILOCO_TIMEZONE;
    else process.env.MILOCO_TIMEZONE = prevEnv;
  });

  it("Asia/Shanghai 输出 +08:00 后缀", () => {
    const iso = nowLocalIso("Asia/Shanghai");
    expect(iso).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$/);
  });

  it("UTC 输出 +00:00 后缀", () => {
    const iso = nowLocalIso("UTC");
    expect(iso).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$/);
  });

  it("跨时区两次调用偏移不同,但绝对时刻接近", () => {
    const sh = nowLocalIso("Asia/Shanghai");
    const la = nowLocalIso("America/Los_Angeles");
    expect(sh).toMatch(/\+08:00$/);
    // PST/PDT 都是负偏移
    expect(la).toMatch(/-0[78]:00$/);
    // 两个 ISO 解析后 ms 差距应 < 1s
    expect(
      Math.abs(Date.parse(sh) - Date.parse(la)),
    ).toBeLessThan(1000);
  });
});
