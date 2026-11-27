# IoT 旁路契约

输入是 `miloco-cli observe batch` 返回的 `data` 事实快照（外层 code/message 是 API 信封）：

- `attributes` 是属性字典，key 用于引用，含设备 ID、服务、属性和策略版本。
  `current_labels` 是当前名称/房间/枚举解释，不是历史归属证明；枚举被截断时不要猜剩余数值的含义。
- `events` 是本批新增事件，`context` 是重叠上下文；保留入库 ID 顺序，
  但迟到事件的 `at_ms` 可能更早，判断先后需看时间。`old/new` 是带类型的值。
- `scope` 不同的片段不能连接成一条连续事件链。
- `transport` 仅表示传输来源，`actor=unknown` 不能被解释成用户手动操作。
- `coverage=healthy` 仅是该事件时刻覆盖；不代表全天或两个事件之间完整。
  提交时后台会再次校验整个引用片段。`unknown` 不是“没有发生”。
- `lost_before_id` 非空表示早期未处理记录已不可用；`has_more` 表示仍有积压。
- `known` 只用于关联相同定义的描述。只有定义、条件和属性集合相同才复用 ID，
  原样保留 description/attributes；不要为了合并而改写结论。新定义另建，不继承证据。

描述事实，不猜原因。例如“电视开启后落地灯开启”可描述为一个片段，
不是“主人喜欢观影灯光”。只有一次时注明；没有值得保留的信息返回空数组。
同时记录与已知描述不一致之处；不能只挑支持证据，不能把未知当反例。
可以自由发现顺序、共现、条件关系等，不要求固定时段、固定频次。

提交格式（ID 均使用真实返回值；每片段最多 16 个事件）：

```bash
miloco-cli observe submit --data '{
  "batch_id": "返回的批次ID",
  "observations": [{
    "description": "电视开启后落地灯开启",
    "attributes": ["电视属性key", "灯属性key"],
    "episodes": [{"event_ids": [101, 102], "kind": "support"}],
    "uncertainty": "本批仅一个片段；操作来源未知"
  }]
}'
```

同定义追加证据时加 `observation_id`，保持 description 和 attributes 不变。
片段 `kind` 为 `support` / `difference` / `unknown`，引用本批 events/context 的事件 ID。
support 必须包含定义中每个属性的事件；difference 可引用其中部分属性的实际变化，
但不能仅凭缺少另一属性事件断言其未变化。无法确定连续性时标 unknown。
不要添加置信度、日期计数、成熟度或动作字段；后台验证与累积，不采信自报统计。

无发现：`miloco-cli observe submit --data '{"batch_id":"真实ID","observations":[]}'`。
复杂 JSON 可用 `--data-file`，但不要保存设备流水到长期记忆。
本轮最多一次读取、一次提交，不为了得到结论反复读取或追查。保持默认紧凑 JSON，
`--pretty` 仅用于人工调试，会突破紧凑输出的字节预算。
