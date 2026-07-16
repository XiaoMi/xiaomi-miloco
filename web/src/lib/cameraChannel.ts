/**
 * 多通道相机（双摄等）的通道/镜头前端工具。
 *
 * 与后端口径一致（miloco.miot.filter）：感知层用**合成 did** `did:ch{n}` 作每路身份，
 * 单摄裸 did；启停/拾音按整台物理 did。这里集中「拆/拼合成 did、判多通道、镜头标签、
 * 哪路有 mic」几件纯逻辑，供 HeroNow / MiotRecorder 共用并可单测。
 */

/** 拆合成 did → {物理 did, 通道}。`'cam:ch1'`→`{cam,1}`；裸 did→`{did,0}`（单通道直通）。
 *  只认末尾的 `:ch{n}`，物理 did 里含冒号也不误伤。对齐后端 `split_channel_did`。 */
export function splitChannelDid(did: string): {
  physicalDid: string;
  channel: number;
} {
  const i = did.lastIndexOf(":ch");
  if (i < 0) return { physicalDid: did, channel: 0 };
  const ch = Number(did.slice(i + 3));
  return Number.isFinite(ch)
    ? { physicalDid: did.slice(0, i), channel: ch }
    : { physicalDid: did, channel: 0 };
}

/** 在一批相机记录里，出现多于一条记录的物理 did = 多通道相机（双摄两条同 did）。 */
export function multiChannelDidSet(cams: { did: string }[]): Set<string> {
  const count = new Map<string, number>();
  for (const c of cams) count.set(c.did, (count.get(c.did) ?? 0) + 1);
  return new Set(
    [...count.entries()].filter(([, n]) => n > 1).map(([did]) => did),
  );
}

/** 投喂开关的目标 did：多通道 → 合成 `did:ch{n}`（精确到某路）；单通道 → 裸 did。 */
export function feedDid(did: string, channel: number, isMulti: boolean): string {
  return isMulti ? `${did}:ch${channel}` : did;
}

/** 通道 → 镜头标签的 i18n key：ch0=移动画面（球机）/ ch1=固定画面（枪机）；
 *  其它通道返回 null，调用方用 `hero.channelLabel`（通道 N）兜底。 */
export function lensLabelKey(channel: number): string | null {
  if (channel === 0) return "hero.lensMoving";
  if (channel === 1) return "hero.lensFixed";
  return null;
}

/** 该通道是否有 mic：只有球机/ch0 有音频，枪机及其它通道无（枪机永久无 mic）。 */
export function channelHasMic(channel: number): boolean {
  return channel === 0;
}
