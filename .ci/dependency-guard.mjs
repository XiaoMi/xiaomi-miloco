// 依赖变更准入守卫。
// PR 改动了锁文件（pnpm-lock.yaml / package-lock.json / uv.lock）或依赖清单
// （package.json / pyproject.toml）时阻塞合入，除非授权人在 PR 评论
// `/allow-dependencies-change <head_sha>` 放行（须带当前 head SHA，见下方时效校验）。
//
// 仅用 github.token + REST API，不依赖 GitHub App。
//
// 安全：由 pull_request_target 在 base（可信）上下文运行，只通过 API 读取 PR 元数据，
// 从不 checkout / 执行 PR head 代码。
//
// 环境变量：
//   GITHUB_TOKEN              —— job 注入的 github.token
//   GITHUB_REPOSITORY         —— owner/repo
//   GITHUB_EVENT_PATH         —— 事件 payload 路径
//   MILOCO_SECURITY_APPROVERS —— 逗号分隔的授权 GitHub 用户名（可空，空时只认 OWNER/MEMBER/COLLABORATOR）

import { readFile } from "node:fs/promises";
import process from "node:process";

const TOKEN = process.env.GITHUB_TOKEN;
const [OWNER, REPO] = (process.env.GITHUB_REPOSITORY || "/").split("/");
const APPROVERS = (process.env.MILOCO_SECURITY_APPROVERS || "")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);
const ALLOW_MARKER = "/allow-dependencies-change";

const LOCKFILES = ["pnpm-lock.yaml", "package-lock.json", "uv.lock"];
const MANIFESTS = ["package.json", "pyproject.toml"];

async function gh(path) {
  const res = await fetch(`https://api.github.com${path}`, {
    headers: {
      Authorization: `Bearer ${TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
    },
  });
  if (!res.ok) throw new Error(`${path} → ${res.status} ${await res.text()}`);
  return res.json();
}

async function paginate(path) {
  const out = [];
  for (let page = 1; ; page++) {
    const sep = path.includes("?") ? "&" : "?";
    const batch = await gh(`${path}${sep}per_page=100&page=${page}`);
    out.push(...batch);
    if (batch.length < 100) break;
  }
  return out;
}

const basename = (p) => p.split("/").pop();

async function main() {
  const event = JSON.parse(await readFile(process.env.GITHUB_EVENT_PATH, "utf8"));
  const pr = event.pull_request
    ?? await gh(`/repos/${OWNER}/${REPO}/pulls/${event.issue.number}`);
  if (!pr) {
    console.log("⚠ 无法获取 PR 信息，跳过。");
    return;
  }

  const files = await paginate(`/repos/${OWNER}/${REPO}/pulls/${pr.number}/files`);
  const names = files.map((f) => f.filename);
  const lockChanged = names.filter((n) => LOCKFILES.includes(basename(n)));
  const manifestChanged = names.filter((n) => MANIFESTS.includes(basename(n)));

  if (lockChanged.length === 0 && manifestChanged.length === 0) {
    console.log("✓ 未改动依赖锁文件或清单，放行。");
    return;
  }

  const touched = [...new Set([...lockChanged, ...manifestChanged])];
  console.log(`检测到依赖相关变更：\n  ${touched.join("\n  ")}`);

  // 放行评论必须携带它批准的 head SHA：`/allow-dependencies-change <sha>`。
  // 任何新 push 都会改变 head SHA，使旧评论里的 SHA 失配 → 旧批准自动失效，
  // 堵住「批准后再 push 投毒依赖」的绕过。
  // 不用提交时间戳：git committer/author date 由提交者经 GIT_COMMITTER_DATE 控制，
  // 可回填到很早从而骗过「评论须晚于提交」的判断；SHA 与时间戳无关，不可伪造。
  const headSha = pr.head.sha;
  const short = headSha.slice(0, 12);
  const comments = await paginate(`/repos/${OWNER}/${REPO}/issues/${pr.number}/comments`);
  const isApprover = (c) =>
    APPROVERS.includes(c.user.login) ||
    ["OWNER", "MEMBER", "COLLABORATOR"].includes(c.author_association);
  const approved = comments.some((c) => {
    const body = c.body.trim();
    if (!body.startsWith(ALLOW_MARKER)) return false;
    const sha = body.slice(ALLOW_MARKER.length).trim().split(/\s+/)[0] || "";
    // 至少 7 位、且是当前 head 的前缀（防空串 startsWith("") 恒真）
    return sha.length >= 7 && headSha.startsWith(sha) && isApprover(c);
  });

  if (approved) {
    console.log(`✓ 已有匹配当前 head（${short}）的授权放行评论，放行。`);
    return;
  }

  // 有 marker 但 SHA 不匹配（漏带 / 带了旧 SHA）→ 明确告知正确格式
  if (comments.some((c) => c.body.trim().startsWith(ALLOW_MARKER) && isApprover(c))) {
    console.error(
      `::error::检测到 ${ALLOW_MARKER} 放行评论，但未匹配当前最新提交。请授权人重新评论：${ALLOW_MARKER} ${short}`,
    );
    process.exit(1);
  }

  console.error(
    `::error::本 PR 改动了依赖锁文件 / 清单，需仓库维护者评论 ${ALLOW_MARKER} ${short} 放行后方可合入。`,
  );
  process.exit(1);
}

main().catch((err) => {
  console.error(`::error::dependency-guard 执行失败：${err.message}`);
  process.exit(1);
});
