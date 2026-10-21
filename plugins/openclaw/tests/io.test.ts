import {
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { writeTextFileSync } from "../src/utils/io.js";

describe("writeTextFileSync 原子写契约", () => {
  let dir: string;
  let target: string;

  beforeEach(() => {
    dir = mkdtempSync(path.join(tmpdir(), "miloco-io-"));
    target = path.join(dir, "config.json");
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it("写入内容并可被读回", () => {
    writeTextFileSync(target, '{"a":1}\n');
    expect(readFileSync(target, "utf8")).toBe('{"a":1}\n');
  });

  it("覆盖已存在的目标文件", () => {
    writeFileSync(target, "old");
    writeTextFileSync(target, "new");
    expect(readFileSync(target, "utf8")).toBe("new");
  });

  it("rename 失败时抛错且不留临时文件", () => {
    // 目标路径是目录 → renameSync(tmp, path) 必然失败，走 catch 清理分支
    rmSync(target, { force: true });
    mkdirSync(target);

    expect(() => writeTextFileSync(target, "boom")).toThrow();
    // 目录里只有 target 本身，没有残留的 .tmp-* 文件
    expect(readdirSync(dir)).toEqual(["config.json"]);
  });
});
