# 附加操作

> 本文件由 [SKILL.md](../SKILL.md) 引用。用户表达"撤销 / 看样本 / 拆开候选组"等低频操作时按本文走。

## 撤销 / Rollback

用户说"刚才挑错了,撤销" / "重选":

```bash
miloco-cli identity register rollback --person-id <id> --session-id <register_session_id>
```

历史批次可查:

```bash
miloco-cli identity register sessions --member <id> --pretty
```

## 查看某人的样本

用户说"看看张三的样本" / "登记成什么样了"。**一次性拿合并图**,不要分多次读单图 + 多次发图:

```bash
miloco-cli identity sample show --person <id> --with-face \
    --save /tmp/<id>_montage.jpg --pretty
# 返回 {body_count, face_count, saved_to, width, height}
# agent 在飞书发 /tmp/<id>_montage.jpg 一张图即可
```

布局(后端自动拼好):
- body 横排,等比 resize 到高度 256
- `--with-face` 时,face 横排,等比 resize 到高度 128,**纵向贴 body 下面**(宽度白边居中对齐)

可选参数:**只能用默认的 `--tier a`(用户登记样本),禁止改 `--tier c`**——tier c 是系统识别后自动累积的辅助样本, 数量多、质量参差, **不是用户登记产物**, 给用户看会造成"我什么时候录过这些"的困惑。本 SKILL 是身份注册流程, 只展示用户主动登记的 tier a 样本。不给 `--save` 时返回 base64 在 stdout。

❌ **反模式**(不要循环发单图):

```
for f in $(ls .../body_*.jpg); do send_to_feishu $f; done   # 多次发图刷屏
```

## 拆开跨摄像头合并的候选组

跨摄像头的候选组(`span_cam_count ≥ 2`)号码图自带视觉复核 hint。用户回"拆开 N"时:

```bash
miloco-cli identity pool cluster-split --cluster-id <cid> --remove-cam <cam>
```
