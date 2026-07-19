/**
 * 多通道相机（双摄等）的通道/镜头前端工具。
 *
 * 与后端口径一致（miloco.miot.filter）：感知层用**合成 did** `did:ch{n}` 作每路身份，
 * 单摄裸 did；启停/拾音按整台物理 did。这里集中「拆/拼合成 did、判多通道、镜头标签、
 * 哪路有 mic」几件纯逻辑，供 HeroNow / MiotRecorder 共用并可单测。
 */

/** 拆合成 did → {物理 did, 通道}。`'cam:ch1'`→`{cam,1}`；裸 did→`{did,0}`（单通道直通）。
 *  **严格**只认末尾的 `:ch{非负整数}`（正则 `/:ch\d+$/`）——空 `:ch`、小数、负数、十六进制
 *  等后端 `toggle_camera` 会拒绝的畸形一律不当作通道，退化成整串裸 did（后续按 did 查相机时
 *  自然落空，不会被误挂到某台的 ch0）。贪婪 `(.*)` 保证物理 did 内含冒号也不误伤，口径与后端
 *  `physical_camera_did` / `toggle_camera` 校验一致。 */
export function splitChannelDid(did: string): {
  physicalDid: string;
  channel: number;
} {
  const m = /^(.*):ch(\d+)$/.exec(did);
  if (!m) return { physicalDid: did, channel: 0 };
  return { physicalDid: m[1], channel: Number(m[2]) };
}

/** 该 did 是否为多通道相机的某一路——即合成 did 形态 `…:ch{n}`。
 *  后端只对多通道相机（channel_count>1）合成 `:ch{n}`、单摄保持裸 did，故「did 带 :ch{n}
 *  后缀」与「channel_count>1」等价，是每行独立的**权威**判据（不依赖同 did 出现几行的行数
 *  代理，即便某台只有一路在列表里也能正确识别）。与后端 `synthetic_camera_did` 口径一致。 */
export function isChannelDid(did: string): boolean {
  return /:ch\d+$/.test(did);
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
