/**
 * skill 正文不写死 agent 身份。
 *
 * 与 prompt.ts 的 B_IDENTITY 同一条不变量：插件是能力层不是人格层。skill 正文会随
 * skill 被加载进宿主 agent 的上下文，若写死「你是这个家的隐形管家」这类身份断言，就与
 * B_IDENTITY 的「名字 / 人设一律沿用宿主既有设定」在同一轮里打架——要么顶掉用户给自己
 * agent 设的人设，要么模型服从 B_IDENTITY 而让 skill 的角色设定静默落空。
 *
 * 两种形状都算：第二人称的身份断言（`你是这个家的……`），以及示范话术里第一人称自报
 * 家门（`Agent: 你好呀，我是 miloco～`）。后者危害更直接——那是给 agent 照抄的开场白。
 *
 * 豁免判据是「能否在带宿主人设的会话里被加载」，不是「是不是 skill 文件」：只在 isolated
 * cron 任务里激活的 skill 没有宿主人设可顶，故可保留身份断言。豁免不是白名单一挂了事——
 * 另有用例要求每个豁免项自己仍声明是 cron-only，免得它哪天变成用户可直接触发的 skill
 * 却仍躺在豁免名单里。判据与背景见 knowledge/03-features/openclaw-integration.md。
 */

import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const here = path.dirname(fileURLToPath(import.meta.url));
const SKILLS_DIR = path.resolve(here, "../../skills");

// 仅在各自 cron 任务里激活的 skill：那种会话本就没有宿主人设可顶。
const CRON_ONLY = ["miloco-home-patrol", "miloco-perception-digest"];

// 形状一：行首的「你是……」是第二人称身份断言。`你是否…` 是正常行文，排除；刻意只锚行首
// 而不整行搜索——`miloco-notify/SKILL.md` 里「写文案 —— 你是家人，不是监控系统」是语气要求
// 而非身份断言，放宽锚点会误伤它。
const IDENTITY_ASSERTION = /^你是(?!否)/;

// 形状二：示范话术 / 开场白里 agent 对用户自报家门。同一条不变量，危害更直接——那是给
// agent 照抄的句子，且 onboarding 示范正是首邀被接受后立刻加载的，破口出现在第一次对话里。
// 它不在行首（`Agent: 你好呀，我是 miloco～`），故整行搜索。一并收「我是……管家」：旧身份句
// 「家庭智能管家 Miloco」去掉 miloco 字样仍是自报身份。示范里用户说的「我是张伟」不受影响。
const SELF_NAMING = /我(?:是|叫)\s*(?:miloco|[^，。；：！？\s]{0,6}管家)/i;

function violation(line: string): string | null {
  const s = line.trim();
  if (IDENTITY_ASSERTION.test(s)) return "身份断言";
  if (SELF_NAMING.test(s)) return "自称";
  return null;
}

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
  it("会在宿主会话里加载的 skill 既不做身份断言、也不自报家门", () => {
    const offenders: string[] = [];
    for (const name of skillDirs()) {
      if (CRON_ONLY.includes(name)) continue;
      for (const file of markdownFiles(path.join(SKILLS_DIR, name))) {
        const lines = readFileSync(file, "utf8").split("\n");
        lines.forEach((line, i) => {
          const kind = violation(line);
          if (kind) {
            const rel = path.relative(SKILLS_DIR, file);
            offenders.push(
              `[${kind}] ${rel}:${i + 1}: ${line.trim().slice(0, 40)}`,
            );
          }
        });
      }
    }
    expect(
      offenders,
      "身份断言改成能力 / 职责叙述（如「打理这个家时你会像一位隐形管家：」）；" +
        "示范话术里的自称直接删掉（开场白按宿主人设来，示范只示范节奏）；" +
        "若确属 cron-only，加入 CRON_ONLY 并在 knowledge 文档里说明判据",
    ).toEqual([]);
  });

  // 「当前树上零误伤」这句话若只写在注释里，下次收紧正则时没人会去复核。钉成用例。
  it("两种形状各自认得出来，且不误伤正常行文", () => {
    const bad = [
      "你是这个家的隐形管家。除了让设备默默配合家人，",
      "Agent: 你好呀，我是 miloco～注意到家庭信息还是空的",
      "我叫 Miloco，是你的家庭助理",
      "我是家庭智能管家，很高兴认识你",
    ];
    const ok = [
      "打理这个家时你会像一位隐形管家：除了让设备默默配合家人，",
      "先判断你是否需要反问主体身份",
      "### 5. 写文案 —— 你是家人，不是监控系统",
      '（比如："我是张伟，爸爸；我妻子李静；还有 7 岁的女儿朵朵"）',
      "用户:  (语音) 我们家三口，我叫王强，孩子他妈刘敏",
      '须达到"若我是家人会开始担心"的程度',
    ];
    expect(
      bad.filter((l) => violation(l) === null),
      "漏判",
    ).toEqual([]);
    expect(
      ok.filter((l) => violation(l) !== null),
      "误伤",
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
