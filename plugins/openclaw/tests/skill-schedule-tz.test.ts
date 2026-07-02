/**
 * miloco-create-task SKILL 的 cron 语法契约测试。
 *
 * 回归 bug：曾把 `cron list` 的显示格式 `<expr> @ <tz>` 当输入语法写进 SKILL 示例，
 * 而真实契约是 cron tool `schedule` 对象的独立 `tz` 字段（plugin-sdk CronSchedule
 * {kind:"cron", expr, tz?}），expr 原样进 croner——带 `@` 的 expr 直接解析失败。
 * 本测试从 SKILL.md / crud-ops.md 抽取示例 schedule 片段做静态校验，防止回潮。
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = path.dirname(fileURLToPath(import.meta.url));
const skillDir = path.resolve(here, "../../skills/miloco-create-task");
const skill = readFileSync(path.join(skillDir, "SKILL.md"), "utf8");
const crudOps = readFileSync(
  path.join(skillDir, "references", "crud-ops.md"),
  "utf8",
);

// 裸 5 段 cron：每段仅 数字 * , - /（croner 可解析的子集，足够覆盖 SKILL 示例）
const FIVE_FIELD = /^[\d*,/-]+( [\d*,/-]+){4}$/;

describe("miloco-create-task SKILL cron 契约", () => {
  it('expr="..." 均为裸 5 段表达式，绝无 @ 时区后缀（那是 cron list 显示格式）', () => {
    const exprs = [...skill.matchAll(/expr="([^"]+)"/g)].map((m) => m[1]);
    expect(exprs.length).toBeGreaterThan(0);
    for (const e of exprs) {
      expect(e, `expr 混入了显示格式的时区后缀: ${e}`).not.toContain("@");
      // `0 H1,H2,...,Hn * * *` 这类模板占位不按字面校验，其余必须 croner 可解析形状
      if (!/[A-Za-z]|\.{3}/.test(e)) {
        expect(e, `expr 不是合法 5 段 cron: ${e}`).toMatch(FIVE_FIELD);
      }
    }
  });

  it('每处 expr="..." 示例同一行都带独立 tz= 字段', () => {
    for (const line of skill.split("\n")) {
      if (line.includes('expr="')) {
        expect(line, `expr 示例缺独立 tz 字段: ${line}`).toMatch(/tz[=:]/);
      }
    }
  });

  it('不存在把时区塞进表达式字符串的 cron="...@..." 写法', () => {
    for (const text of [skill, crudOps]) {
      expect(text).not.toMatch(/(?:cron|expr)="[^"]*@[^"]*"/);
    }
  });

  it("存在强制时区规则章节，且指向独立 tz 字段", () => {
    expect(skill).toContain("### Schedule.时区（强制）");
    expect(skill).toContain("绝不创建不带家庭时区的 cron 定时任务");
    // 规则必须描述真实契约：schedule 对象独立 tz 字段
    expect(skill).toMatch(/schedule=\{kind:"cron", expr:"[^"]+", tz:"<家庭时区>"\}/);
  });
});
