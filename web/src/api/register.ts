/**
 * 身份注册流程接口。挂在主 backend ``/api/identity/*`` 路由下,与 perception
 * 主流程同进程,token 校验、生命周期与主服务一致——无需再手动启动独立进程。
 *
 * 历史:之前由独立的离线注册服务(8765 端口)提供,前端通过 vite proxy ``/identity``
 * → ``127.0.0.1:8765`` 调通、需额外开进程。现已退役移除,所有路由迁移到
 * ``miloco.person.router``(prefix ``/identity``, 经 app.include_router 挂在 ``/api`` 下),
 * 前端直接走主 backend。
 */

import { errorMessage, resolveToken } from "./client";
import i18n from "@/i18n";

// 生产 / dev 都通过主 backend 转发,token 经 vite proxy::attachAuth(dev)或前端
// 显式带 Authorization(prod)抵达。两条路径都走 resolveToken。
export function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const token = resolveToken();
  return token ? { ...(extra ?? {}), Authorization: `Bearer ${token}` } : (extra ?? {});
}

export interface ExtractCandidate {
  type: "body" | "face";
  image_b64: string; // 不带 data: 前缀
  confidence: number;
  frame_index: number;
  bbox?: [number, number, number, number] | null;
}

export interface ExtractResult {
  is_video: boolean;
  n_frames: number;
  candidates: ExtractCandidate[];
  // 算法预选 indices(指向 candidates 数组),前端默认勾选这些;用户可手改。
  auto_selected: { body: number[]; face: number[] };
}

interface Normal<T> {
  code: number;
  message: string;
  data: T;
}

export async function extractCandidates(
  personId: string,
  media: Blob,
  filename: string,
  maxFrames = 12,
): Promise<ExtractResult> {
  const form = new FormData();
  form.append("media", media, filename);
  form.append("max_frames", String(maxFrames));
  const r = await fetch(`/api/identity/persons/${personId}/extract`, {
    method: "POST",
    body: form,
    headers: authHeaders(),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(
      errorMessage(err) ?? i18n.t("api.extractFail", { status: r.status }),
    );
  }
  const body = (await r.json()) as Normal<ExtractResult>;
  // 后端缺 auto_selected 字段(老版兼容)时降级为空数组,前端逻辑别炸。
  const data = body.data;
  if (!data.auto_selected) {
    data.auto_selected = { body: [], face: [] };
  }
  return data;
}

// 该人识别库 tier_a 下已存的 body / face 样本数。注册前端据此算"还能再选几张"
// (后端每类硬上限 5 = tier_a_max // 2),从源头约束勾选、避免存超被静默丢弃。
//
// 前瞻预留(reviewer 留意):当前唯一注册入口 PersonDrawer"让它认识X"按钮门控在
// ``!faceEnrolled`` 上,而 faceEnrolled 只要 tier_a 有任意样本即为 true——所以今天
// 能进 EnrollFlow 的人 tier_a 恒为空,本接口实际恒返回 {0,0}。保留它是为将来可能
// 引入的"给已认识的人追加/补充样本"入口预留:那条路径下 existing>0 才成真,届时
// EnrollFlow 的"5 − 已存"额度逻辑可直接生效,无需返工。
export interface TierACounts {
  body: number;
  face: number;
}

export async function fetchTierACounts(personId: string): Promise<TierACounts> {
  const r = await fetch(`/api/identity/persons/${personId}/samples`, {
    method: "GET",
    cache: "no-store",
    headers: authHeaders(),
  });
  if (!r.ok) {
    throw new Error(i18n.t("api.fetchSamplesFail", { status: r.status }));
  }
  const body = (await r.json()) as {
    data?: { body?: unknown[]; face?: unknown[] };
  };
  return {
    body: body.data?.body?.length ?? 0,
    face: body.data?.face?.length ?? 0,
  };
}

// 后端 /samples/batch 即便部分 item 写入失败(容量满 / 解码失败)也返回 200,
// 失败明细在 data.failed。调用方必须消费 failed,否则会把部分失败误显示为全部成功。
export interface SaveBatchResult {
  written_body: number;
  written_face: number;
  failed: { index: number; reason: string }[];
}

export async function saveSamplesBatch(
  personId: string,
  items: { type: "body" | "face"; image_b64: string }[],
): Promise<SaveBatchResult> {
  const r = await fetch(`/api/identity/persons/${personId}/samples/batch`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ items }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(
      errorMessage(err) ?? i18n.t("api.saveFail", { status: r.status }),
    );
  }
  const body = (await r.json()) as { data: SaveBatchResult };
  const data = body.data;
  // 老版后端兼容:缺字段时降级,前端逻辑别炸。
  return {
    written_body: data?.written_body ?? 0,
    written_face: data?.written_face ?? 0,
    failed: data?.failed ?? [],
  };
}
