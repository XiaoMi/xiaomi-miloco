/**
 * skill 正文不写死 agent 身份。
 *
 * 与 prompt.ts 的 B_IDENTITY 同一条不变量：插件是能力层不是人格层。skill 正文会随
 * skill 被加载进宿主 agent 的上下文，若写死「你是这个家的隐形管家」这类身份断言，就与
 * B_IDENTITY 的「名字 / 人设一律沿用宿主既有设定」在同一轮里打架——要么顶掉用户给自己
 * agent 设的人设，要么模型服从 B_IDENTITY 而让 skill 的角色设定静默落空。
 *
 * 豁免判据是「能否在带宿主人设的会话里被加载」，不是「是不是 skill 文件」：只在 isolated
 * cron 任务里激活的 skill 没有宿主人设可顶，故可保留身份断言。豁免不是白名单一挂了事——
 * 下方第二个用例要求每个豁免项自己仍声明是 cron-only，免得它哪天变成用户可直接触发的
 * skill 却仍躺在豁免名单里。判据与背景见 knowledge/03-features/openclaw-integration.md。
 */

import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = path.dirname(fileURLToPath(import.meta.url));
const SKILLS_DIR = path.resolve(here, "../../skills");

// 仅在各自 cron 任务里激活的 skill：那种会话本就没有宿主人设可顶。
const CRON_ONLY = ["miloco-home-patrol", "miloco-perception-digest"];

// 行首的「你是……」是身份断言的形状；`你是否…` 是正常行文，排除。
const IDENTITY_ASSERTION = /^你是(?!否)/;

function skillDirs(): string[] {
  return readdirSync(SKILLS_DIR, { withFileTypes: true })
    .filter((e) => e.isDirectory())
    .map((e) => e.name);
}

// skill 目录下所有 md（含 references/），它们都可能被 agent 读进上下文。
function markdownFiles(dir: string): string[] {
  const out: string[] = [];
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, e.name);
    if (e.isDirectory()) out.push(...markdownFiles(full));
    else if (e.name.endsWith(".md")) out.push(full);
  }
  return out;
}

describe("skill 正文不写死 agent 身份", () => {
  it("会在宿主会话里加载的 skill 不含「你是……」式身份断言", () => {
    const offenders: string[] = [];
    for (const name of skillDirs()) {
      if (CRON_ONLY.includes(name)) continue;
      for (const file of markdownFiles(path.join(SKILLS_DIR, name))) {
        const lines = readFileSync(file, "utf8").split("\n");
        lines.forEach((line, i) => {
          if (IDENTITY_ASSERTION.test(line.trim())) {
            const rel = path.relative(SKILLS_DIR, file);
            offenders.push(`${rel}:${i + 1}: ${line.trim().slice(0, 40)}`);
          }
        });
      }
    }
    expect(
      offenders,
      "skill 正文改成能力 / 职责叙述（如「打理这个家时你会像一位隐形管家：」）；" +
        "若确属 cron-only，加入 CRON_ONLY 并在 knowledge 文档里说明判据",
    ).toEqual([]);
  });

  it("豁免项自己仍声明是 cron-only（豁免不能悄悄过期）", () => {
    for (const name of CRON_ONLY) {
      const md = readFileSync(path.join(SKILLS_DIR, name, "SKILL.md"), "utf8");
      expect(md, `${name} 已不再声明「仅由该任务调用」，应撤销豁免`).toContain(
        "仅由该任务调用",
      );
      expect(md, `${name} 已不再声明「不单独使用」，应撤销豁免`).toContain(
        "不单独使用",
      );
    }
  });

  // 名单里挂了不存在的目录 → 豁免形同虚设且无人察觉，一并守住。
  it("CRON_ONLY 名单里的 skill 都真实存在", () => {
    const dirs = skillDirs();
    for (const name of CRON_ONLY) expect(dirs).toContain(name);
  });
});
