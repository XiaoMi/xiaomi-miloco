# 示例与反例

> 本文件由 [SKILL.md](../SKILL.md) 引用。每个场景演示一种入口形态的完整流程,反例集中列 11 条 LLM 常踩坑。

## 场景 1 · 用户发 4 张图 + "这是我哥张三,帮我注册"(共 2 轮)

```
解析触发源:附件 = 4 张图,姓名 = "张三",家庭角色 = "哥",意图 = 注册
通路判定: 上传通路 · 图片分支,走单次批量(约束 3)

第三步:
  $ miloco-cli identity register preview \
        --images /tmp/<uuid>_1.jpg --images /tmp/<uuid>_2.jpg \
        --images /tmp/<uuid>_3.jpg --images /tmp/<uuid>_4.jpg \
        --topk 5 --save-montage /tmp/<uuid>_preview.jpg --pretty
  → status_preview = ok, auto_selected_indices = [0, 3, 5, 7]
    auto_selected_body_count = 4, auto_selected_face_count = 3
  发用户:[拼图] "找到 4 张身体样本 + 3 张人脸样本,要登记到「张三」(哥哥)?回'确认'入库。"

第四步:等待用户回复(本轮终止)

第五步(用户回"确认"):
  $ miloco-cli identity register commit \
        --pending-id rsp-xxxx --indices 0,3,5,7 \
        --member-name 张三 --member-role 哥哥 --pretty
  发用户:"已为「张三」入库 4 张样本"
```

## 场景 2 · 用户发视频(单人) + "这是张三"(共 2 轮)

```
解析触发源:附件 = 视频, 姓名 = "张三"
通路判定: 上传通路 · 视频分支(multi_track 待第三步揭晓)

第三步:
  $ miloco-cli identity register preview --video /tmp/<uuid>.mp4 \
        --topk 5 --save-montage /tmp/<uuid>_preview.jpg --pretty
  → multi_track = false, auto_selected_indices = [2, 4, 7, 9, 11]
    auto_selected_body_count = 5, auto_selected_face_count = 4
  发用户:[拼图] "找到 5 张身体样本 + 4 张人脸样本,要登记到「张三」?回'确认'入库。"

第五步(用户回"确认"):
  $ miloco-cli identity register commit \
        --pending-id rsp-yyyy --indices 2,4,7,9,11 --member-name 张三 --pretty
  发用户:"已为「张三」入库 5 张样本"
```

## 场景 3 · 用户发视频(多人) + "登记一下"(没给姓名)(共 2 轮)

```
解析触发源:附件 = 视频, 姓名 = 缺

第三步:
  $ miloco-cli identity register preview --video /tmp/<uuid>.mp4 \
        --topk 5 --save-montage /tmp/<uuid>_preview.jpg --pretty
  → multi_track = true, track_count = 3
    tracks = [
      {label: "1", track_id: 11, auto_selected_indices_global: [1, 3, 5]},
      {label: "2", track_id: 17, auto_selected_indices_global: [2, 4, 8]},
      {label: "3", track_id: 23, auto_selected_indices_global: [6, 7, 10]}
    ]
  发用户:[号码图] "视频里识别出 3 个人(图中编号),要登记的是哪一位?
                  回数字 1/2/3,并告诉我 TA 叫什么。"

第五步(用户回"2,张三"):
  → 解析:编号 = 2, indices = tracks[1].auto_selected_indices_global = [2, 4, 8]
  $ miloco-cli identity register commit \
        --pending-id rsp-zzzz --indices 2,4,8 --member-name 张三 --pretty
  发用户:"已为「张三」入库 3 张样本"
```

## 场景 4 · 推送陌生人响应 "这是我自己"(共 2 轮)

```
触发源:[感知引擎] 推送 "陌生人在工位活动(cam=desk_cam, track=7)"
用户回:这是我自己,登记一下,我叫小赵

通路判定: 陌生人候选通路 · 锁定候选(用推送 hint)

第三步:
  $ miloco-cli identity pool fetch --cam desk_cam --track 7 \
        --save-montage /tmp/<uuid>_pool.jpg --pretty
  → clusters_total = 1
    tracks = [{label: "1", cluster_id: "c-7", total_crops: 8, span_cam_count: 1}]
  发用户:[拼图] "锁定到 1 组候选(8 张样本,单摄像头),要给「小赵」创建档案?回'确认'入库。"

第五步(用户回"确认"):
  $ miloco-cli identity register from-cluster --name 小赵 --cluster-id c-7 --pretty
  发用户:"已为「小赵」入库 8 张样本"
```

## 场景 5 · 用户无附件主动注册(走双入口话术)(共 3 轮 —— 含双入口话术一轮)

```
轮 1:
  用户:帮我登记王阿姨
  通路判定: 无附件 + 无 hint, 走双入口话术(约束 4)
  发用户:[双入口话术 · 有姓名版]
  本轮结束,等待用户回复

轮 2:
  用户:2
  通路判定: 走陌生人候选通路 · 跨摄像头近 5 min

  第三步:
    $ miloco-cli identity pool fetch --window 300 \
          --save-montage /tmp/<uuid>_pool.jpg --pretty
    → clusters_total = 4, clusters_displayed = 4, tracks = [...]
    发用户:[号码图] "近 5 分钟看到 4 组陌生人(图中编号),要给「王阿姨」选哪一组?回数字 1~4。"
  本轮结束,等待用户回复

轮 3:
  用户:3
  第五步:
    cluster_id = tracks[2].cluster_id = "c-xy"
    $ miloco-cli identity register from-cluster --name 王阿姨 --cluster-id c-xy --pretty
    发用户:"已为「王阿姨」入库 5 张样本"
```

## 反例(LLM 容易犯,严禁)

```
❌ 错 1:同一轮内调 preview 后立刻调 commit(没等用户回复)—— 违反约束 1

❌ 错 2:同一轮内调 preview → 回 "正在入库,稍等" → 接着调 commit —— 违反约束 1

❌ 错 3:第三步发完拼图前,先发"收到视频,正在分析..."等进度消息
        —— 用户不需要 progress,本轮只该有一条 assistant 文本(就是最后的拼图 + 问询)

❌ 错 4:把后端拼好的拼图拆成多条消息发("这是第一个人:[图]","这是第二个人:[图]")
        —— 拼图就一张 jpg,发这一张即可

❌ 错 5:给 CLI 加 | python3 -c / | jq / | grep 等管道过滤
        —— 会丢 multi_track / tracks / montage_kind 等关键字段

❌ 错 6:多人视频第五步 commit 用顶层的 auto_selected_indices
        —— 那是跨人物平铺的,会把多个不同人的样本混入同一个档案

❌ 错 7:看到视频文件,先 ffmpeg 抽帧再走 --image —— 违反约束 2

❌ 错 8:用户发 4 张图,循环 for img: register preview --image $img —— 违反约束 3,
        只能把第 1 张的拼图发出去,后 3 张全丢

❌ 错 9:用户无附件说"登记张三",agent 直接回"请上传照片"
        —— 违反约束 4,把"从摄像头看到的人里挑"这条路径吞了

❌ 错 10:回复用户时出现"陌生人池"三个字 —— 违反用户可见输出术语黑名单

❌ 错 11:用户提了 SKILL 不支持的请求(如"用录音当样本"),agent 想办法用别的工具
         凑出来 —— 违反总原则,应引导回标准入口
```

