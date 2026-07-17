# 示例与反例（miloco-miot-pet-register）

命令里的 `<MediaPath>` 一律用宿主框架本轮给的真实本地路径（OpenClaw 用消息里的 `MediaPath`，不要用 `media://` 别名，不要凭文件名拼目录）。发图必须用 `message` 工具显式上传。

---

## 场景 0 · B 文字通路 · 纯描述注册（无素材）

用户："我想注册下胖胖，是我家的橘猫，尾部有白毛，体型较胖"

1. 解析：name=胖胖，species=猫，外观="橘猫，尾部有白毛，体型偏胖"；无附件 → B 文字通路。
2. **回显请确认**（不擅自落库）：
   > 好的，登记这只：名字 胖胖 · 猫 · 外观"橘猫，尾部有白毛、体型偏胖"。确认入库？（想让画面里也认出它，回头发张照片/短视频，我再建识别参照）
3. **本轮终止等待**。
4. 用户："确认"
5. 落库（**无 reference-crops**）：
   ```bash
   miloco-cli pet add --name 胖胖 --species 猫 --pretty            # → id=pet_yyy
   # /tmp/p0_persona.json = [{"op":"add","entry":{"type":"member_persona",
   #   "subject_id":"pet_yyy","subject_name":"胖胖","content":"橘猫，尾部有白毛，体型偏胖"}}]
   miloco-cli home-profile profile-write --ops-file /tmp/p0_persona.json --user-edit --pretty
   miloco-cli home-profile commit --pretty
   ```
6. 回复："已给「胖胖」建好档案（记了外观）。想让系统在画面里认得出它，发张照片或短视频，我再帮它建识别参照。"

---

## 场景 1 · A 素材通路 · 单只 · 多图注册（约束 3 单次批量）

用户（一条消息附 3 张不同姿态的猫图）："这是我家猫小黑，记一下"

1. observe（**一次** `--images` 批量）：
   ```bash
   miloco-cli pet observe --images <p1> --images <p2> --images <p3> \
       --save-crops /tmp/ab12_pet --pretty
   ```
   → `crops_saved=[{index:0,path:/tmp/ab12_pet_0.jpg,score:0.36,...},{index:1,...}]`，
     `description.summary="黑色短毛猫，胸口一撮白，右耳尖有缺口"`，`warnings=[]`
2. 用 `message` 发 `crops_saved` 里的候选图 + 文字：
   > 观察好了：一只黑色短毛猫，胸口一撮白、右耳尖有缺口。我挑了 2 张不同姿态作识别参照（图）。确认给「小黑」入库？回"确认"。
3. **本轮终止等待**。
4. 用户："确认"
5. 落库：
   ```bash
   miloco-cli pet add --name 小黑 --species 猫 --pretty            # → id=pet_xxx
   miloco-cli pet reference-crops pet_xxx \
       --crops /tmp/ab12_pet_0.jpg --crops /tmp/ab12_pet_1.jpg \
       --scores 0.36,0.21 --mode replace --pretty
   # /tmp/ab12_persona.json = [{"op":"add","entry":{"type":"member_persona",
   #   "subject_id":"pet_xxx","subject_name":"小黑","content":"黑色短毛猫，胸口一撮白，右耳尖有缺口"}}]
   miloco-cli home-profile profile-write --ops-file /tmp/ab12_persona.json --user-edit --pretty
   miloco-cli home-profile commit --pretty
   ```
6. 回复："已给「小黑」建好档案：记了 2 张识别参照 + 外观。"

---

## 场景 2 · 单只 · 视频注册（禁抽帧）

用户（附一段 10 秒狗视频）："这是我家柯基，叫豆豆"

1. observe（原文件 `--video`）：
   ```bash
   miloco-cli pet observe --video <MediaPath.mp4> --save-crops /tmp/cd34_pet --pretty
   ```
2. 发候选图 + "一只柯基犬……挑了 3 张参照，确认给「豆豆」入库？"
3. 等待 → 用户"确认" → `pet add --name 豆豆 --species 狗` + `reference-crops … --mode replace` + 写外观 + commit。

---

## 场景 3 · 补充素材（append）

用户（附 2 张图）："给小黑再补几张，最近它换季掉毛了"

1. `miloco-cli pet list` 确认"小黑"已存在 → pet_id=pet_xxx。
2. observe（`--images`）→ 拿新候选 crop。
3. 发图 + "给「小黑」补这几张作参照？"→ 等待 → 确认。
4. 落库（**append**，不 `pet add`）：
   ```bash
   miloco-cli pet reference-crops pet_xxx --crops /tmp/ef_pet_0.jpg --crops /tmp/ef_pet_1.jpg \
       --scores 0.31,0.28 --mode append --pretty
   ```
   （append 与现有合并、按绝对分留 top-3。）回复："已给「小黑」补充素材，识别参照已更新。"

---

## 场景 4 · 无描述无素材 → 引导（复述已知、只问缺的；描述 或 发素材）

用户："我想登记下我家的猫"（说了物种"猫"，但没名字、没外观、没附件）

Agent（**接住已知的"猫"，只补问名字+外观**，两条路都给）：
> 好嘞，这只猫要登记，两种方式任选：
> - **描述一下它长什么样**（叫什么名字、毛色花纹/体型/有没有显著标记），我建个档；
> - 或**发张照片/短视频**，我还能顺带建识别参照，让画面里也认得出它。

- **本轮终止等待**，不建空壳。用户补描述 → 场景 0（B）；补素材 → 场景 1/2（A）。
- 若用户连物种都没说（"帮我登记个宠物"）→ 话术里把"猫还是狗"也一起问。

---

## 反例（LLM 易犯）

1. ❌ **视频抽帧当图**：`ffmpeg v.mp4 → f.png && pet observe --image f.png`。✅ 直接 `--video v.mp4`（约束 2）。
2. ❌ **多图拆调**：对 3 张图各调一次 `pet observe --image`。✅ 一次 `--images a --images b --images c`（约束 3）。
3. ❌ **observe 完直接落库**：同一轮 observe 后不等用户确认就 `pet add`+`reference-crops`。✅ observe → 发候选 → 等确认 → 才落库（约束 1）。
4. ❌ **模拟"从摄像头挑宠物"**：用 perceive / 拉摄像头记录去"找没登记的宠物"。✅ 宠物无陌生池，一句话引导用户描述或发素材（总原则）。
9. ❌ **功能关仍强行 observe**：`pet_recognition` 关时还调 `pet observe`（返 404）。✅ 转 B 文字通路，让用户描述、先建档（约束 4）。
10. ❌ **纯文字描述被踢走**：用户描述了样子却回"请发照片才能登记"。✅ 描述即可走 B 文字通路建档（+ 提示发素材可加识别参照）。
5. ❌ **无名硬建**：用户没给名就 `pet add --name 猫`。✅ 追问真名。
6. ❌ **功能关还引导**：`pet_recognition` 关时仍跑 observe。✅ 先告知去设置打开。
7. ❌ **refs_inconsistent 直接混入**：多图疑似不同只仍一起入库。✅ 先与用户确认是不是同一只。
8. ❌ **给用户看内部词**：回复里出现 "observe / crop / reference-crops / member_persona"。✅ 说"观察 / 识别参照 / 外观"。
