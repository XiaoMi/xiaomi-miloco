import { useEffect, useMemo, useState } from "react";
import {
  createMiotEventMapping,
  deleteMiotEventMapping,
  fetchDeviceSpec,
  listMiotEventLogs,
  listMiotEventMappings,
  testMiotEventTrigger,
  updateMiotEventMapping,
} from "@/api";
import type {
  MiotEventMapping,
  MiotEventSource,
  MiotPropertyFilterCondition,
  MiotPropertyFilterOp,
  MiotEventTriggerLog,
  ScopeCamera,
} from "@/lib/types";
import { useAsync } from "@/hooks/useAsync";
import { resolveToken } from "@/api/client";
import { toast } from "./Toast";

interface Props {
  devices: MiotEventSource[];
  scenes: MiotEventSource[];
  cameras: ScopeCamera[];
}

interface SpecProperty {
  key: string;
  name: string;
  value_list?: { value: string; description: string }[];
  value_range?: { min: number; max: number; step: number };
  description: string;
  format: string;
  unit: string;
}

interface SpecEvent {
  key: string;
  name: string;
  description: string;
  arguments: SpecProperty[];
}

interface EditingPropFilter {
  key: string;
  op: MiotPropertyFilterOp;
  value: string;
}

interface MappingSpecMeta {
  names: Record<string, string>;
  values: Record<string, Record<string, string>>;
}

type SourceKind = "device_prop" | "device_event" | "scene";

const SOURCE_KIND_OPTIONS: { value: SourceKind; label: string; hint: string }[] = [
  { value: "device_prop", label: "设备属性变化", hint: "属性变为指定值时触发" },
  { value: "device_event", label: "设备事件触发", hint: "单击、接近、告警等事件触发" },
  { value: "scene", label: "场景触发", hint: "米家场景变化时触发" },
];

const FILTER_OP_OPTIONS: { value: MiotPropertyFilterOp; label: string }[] = [
  { value: "eq", label: "等于" },
  { value: "ne", label: "不等于" },
  { value: "gt", label: "大于" },
  { value: "lt", label: "小于" },
  { value: "gte", label: "大于等于" },
  { value: "lte", label: "小于等于" },
];

function normalizeFilterCondition(
  condition: string | MiotPropertyFilterCondition,
): MiotPropertyFilterCondition {
  if (typeof condition === "string") {
    return condition === "*"
      ? { op: "any", value: "*" }
      : { op: "eq", value: condition };
  }
  return {
    op: condition.op ?? "eq",
    value: condition.value ?? "",
  };
}

function getFilterOpLabel(op: MiotPropertyFilterOp): string {
  switch (op) {
    case "eq":
      return "等于";
    case "ne":
      return "不等于";
    case "gt":
      return "大于";
    case "lt":
      return "小于";
    case "gte":
      return "大于等于";
    case "lte":
      return "小于等于";
    case "any":
      return "任意值";
    default:
      return op;
  }
}

function isNumericProperty(prop?: SpecProperty | null): boolean {
  if (!prop) return false;
  if (prop.value_range) return true;
  return ["int", "float", "uint8", "uint16", "uint32", "uint64", "integer", "double", "number"].includes(
    prop.format,
  );
}

function getPropertyDisplayName(prop?: SpecProperty | null, key?: string): string {
  if (!prop) return key ?? "";
  return prop.description || prop.name || prop.key || key || "";
}

function getPropertyValueDisplayName(
  specMeta: MappingSpecMeta | undefined,
  key: string,
  value: string,
): string {
  return specMeta?.values[key]?.[value] || value;
}

function getMappingKindLabel(item: MiotEventMapping): string {
  if (item.source_type === "scene") return "场景触发";
  return item.event_kinds.some((kind) => kind.startsWith("event."))
    ? "设备事件触发"
    : "设备属性变化";
}

export function AutomationPage({ devices, scenes, cameras }: Props) {
  const mappings = useAsync(() => listMiotEventMappings(), [devices.length, scenes.length]);
  const logs = useAsync(() => listMiotEventLogs(), []);

  const [sourceKind, setSourceKind] = useState<SourceKind>("device_prop");
  const [sourceId, setSourceId] = useState("");
  const [selectedEventKey, setSelectedEventKey] = useState("");
  const [cameraIds, setCameraIds] = useState<string[]>([]);
  const [queryTemplate, setQueryTemplate] = useState("");
  const [cooldownSeconds, setCooldownSeconds] = useState(30);
  const [notes, setNotes] = useState("");

  // Device spec: properties + value options
  const [deviceSpec, setDeviceSpec] = useState<{ model: string; name: string; properties: SpecProperty[]; events?: SpecEvent[] } | null>(null);
  const [specLoading, setSpecLoading] = useState(false);
  const [mappingSpecMeta, setMappingSpecMeta] = useState<Record<string, MappingSpecMeta>>({});
  // Selected property filters: array of { key, value }
  const [propFilters, setPropFilters] = useState<EditingPropFilter[]>([]);

  const sourceOptions = useMemo(
    () => (sourceKind === "scene" ? scenes : devices),
    [devices, scenes, sourceKind],
  );

  async function reloadAll() {
    mappings.reload();
    logs.reload();
  }

  useEffect(() => {
    const deviceIds = Array.from(
      new Set(
        (mappings.data ?? [])
          .filter((item) => item.source_type === "device")
          .map((item) => item.source_id),
      ),
    ).filter((did) => !mappingSpecMeta[did]);
    if (deviceIds.length === 0) return;
    let cancelled = false;
    void Promise.all(
      deviceIds.map(async (did) => {
        try {
          const spec = await fetchDeviceSpec(did);
          return {
            did,
            meta: {
              names: Object.fromEntries(
                [
                  ...(spec.properties ?? []).map((prop) => [
                    prop.key,
                    prop.description || prop.name || prop.key,
                  ]),
                  ...((spec.events ?? []).map((event) => [
                    event.key,
                    event.name || event.description || event.key,
                  ])),
                  ...((spec.events ?? []).flatMap((event) =>
                    (event.arguments ?? []).map((arg) => [
                      arg.key,
                      arg.description || arg.name || arg.key,
                    ]),
                  )),
                ],
              ),
              values: Object.fromEntries(
                [
                  ...(spec.properties ?? []).map((prop) => [
                    prop.key,
                    Object.fromEntries(
                      (prop.value_list ?? []).map((item) => [item.value, item.description || item.value]),
                    ),
                  ]),
                  ...((spec.events ?? []).flatMap((event) =>
                    (event.arguments ?? []).map((arg) => [
                      arg.key,
                      Object.fromEntries(
                        (arg.value_list ?? []).map((item) => [item.value, item.description || item.value]),
                      ),
                    ]),
                  )),
                ],
              ),
            },
          };
        } catch {
          return { did, meta: { names: {}, values: {} } };
        }
      }),
    ).then((items) => {
      if (cancelled) return;
      setMappingSpecMeta((prev) => {
        const next = { ...prev };
        for (const item of items) next[item.did] = item.meta;
        return next;
      });
    });
    return () => {
      cancelled = true;
    };
  }, [mappings.data, mappingSpecMeta]);

  async function onSourceChange(did: string) {
    setSourceId(did);
    setPropFilters([]);
    setSelectedEventKey("");
    if (sourceKind === "scene" || !did) { setDeviceSpec(null); return; }
    setSpecLoading(true);
    try {
      const spec = await fetchDeviceSpec(did);
      setDeviceSpec(spec as { model: string; name: string; properties: SpecProperty[]; events?: SpecEvent[] });
    } catch {
      setDeviceSpec(null);
    } finally {
      setSpecLoading(false);
    }
  }

  function addPropFilter() {
    setPropFilters((prev) => [...prev, { key: "", op: "eq", value: "" }]);
  }

  function updatePropFilter(idx: number, field: "key" | "op" | "value", val: string) {
    setPropFilters((prev) => prev.map((f, i) => i === idx ? { ...f, [field]: val } : f));
  }

  function removePropFilter(idx: number) {
    setPropFilters((prev) => prev.filter((_, i) => i !== idx));
  }

  function getSelectedProp(): Record<string, MiotPropertyFilterCondition> {
    const pf: Record<string, MiotPropertyFilterCondition> = {};
    for (const f of propFilters) {
      if (f.key) pf[f.key] = { op: f.op, value: f.value };
    }
    return pf;
  }

  function selectedEvent(): SpecEvent | undefined {
    return deviceSpec?.events?.find((event) => event.key === selectedEventKey);
  }

  function eventArguments(): SpecProperty[] {
    return selectedEvent()?.arguments ?? [];
  }

  // Get value options for a selected property key
  function getValueOptions(key: string): { value: string; description: string }[] {
    if (!deviceSpec) return [];
    const prop =
      (sourceKind === "device_event"
        ? eventArguments()
        : deviceSpec.properties
      ).find((p) => p.key === key);
    if (!prop) return [];
    if (prop.value_list && prop.value_list.length > 0) return prop.value_list;
    return [];
  }

  async function handleCreate() {
    const source = sourceOptions.find((item) => item.source_id === sourceId);
    if (!source) { toast("请选择事件源", "warn"); return; }
    if (sourceKind === "device_event" && !selectedEventKey) {
      toast("请选择设备事件", "warn");
      return;
    }
    const sourceType = sourceKind === "scene" ? "scene" : "device";
    // Create mapping
    await createMiotEventMapping({
      source_type: sourceType,
      source_id: source.source_id,
      source_name_snapshot: source.source_name,
      camera_dids: cameraIds,
      enabled: true,
      query_template: queryTemplate,
      event_kinds: [
        sourceKind === "device_prop"
          ? "device_prop"
          : sourceKind === "device_event"
            ? selectedEventKey
            : "scene",
      ],
      property_filters: sourceKind === "scene" ? {} : getSelectedProp(),
      cooldown_seconds: cooldownSeconds,
      notes,
      created_at: null,
      updated_at: null,
    });
    setCameraIds([]);
    setQueryTemplate("");
    setCooldownSeconds(30);
    setNotes("");
    setPropFilters([]);
    setSelectedEventKey("");
    setDeviceSpec(null);
    await reloadAll();
  }

  async function toggleEnabled(item: MiotEventMapping) {
    await updateMiotEventMapping(item.id, { enabled: !item.enabled });
    await reloadAll();
  }

  async function runTest(item: MiotEventMapping) {
    const changedProperties =
      item.source_type === "device"
        ? Object.fromEntries(
            Object.entries(item.property_filters ?? {}).map(([key, value]) => {
              const cond = normalizeFilterCondition(value);
              let sample = cond.value;
              if (cond.op === "any") sample = "1";
              if (cond.op === "ne") sample = cond.value === "1" ? "0" : "1";
              if (cond.op === "gt") sample = String(Number(cond.value || 0) + 1);
              if (cond.op === "lt") sample = String(Number(cond.value || 0) - 1);
              if (cond.op === "gte") sample = String(Number(cond.value || 0));
              if (cond.op === "lte") sample = String(Number(cond.value || 0));
              return [key, sample];
            }),
          )
        : {};
    const eventName =
      item.source_type === "device"
        ? (item.event_kinds.find((kind) => kind.startsWith("event.")) ?? "device_prop")
        : "scene";
    await testMiotEventTrigger({
      source_type: item.source_type,
      source_id: item.source_id,
      source_name: item.source_name_snapshot,
      event_name: eventName,
      changed_properties: changedProperties,
    });
    await reloadAll();
  }

  function fmtTs(ts: number): string {
    if (!ts) return "-";
    return new Date(ts).toLocaleString("zh-CN");
  }

  const token = resolveToken();

  function getClipUrl(logId: string, deviceId: string): string {
    const base = `/api/events/${encodeURIComponent(logId)}/clip/${encodeURIComponent(deviceId)}`;
    return token ? `${base}?token=${encodeURIComponent(token)}` : base;
  }

  function getSnapshotUrl(path: string): string {
    const filename = path.split("/").pop() ?? path;
    const base = `/api/automation/snapshots/${encodeURIComponent(filename)}`;
    return token ? `${base}?token=${encodeURIComponent(token)}` : base;
  }

  const createDisabled = !sourceId || cameraIds.length === 0 || (sourceKind === "device_event" && !selectedEventKey);
  const createHint = !sourceId
    ? "请选择事件源"
    : cameraIds.length === 0
      ? "请选择至少一个关联摄像头"
      : sourceKind === "device_event" && !selectedEventKey
        ? "请选择要监听的设备事件"
        : "配置完成后点击保存，命中条件时会触发关联摄像头感知";

  return (
    <div className="mx-auto max-w-5xl space-y-6 px-4 py-6">
      <section className="space-y-1">
        <h1 className="text-title text-text-primary">感知触发</h1>
        <p className="text-caption text-text-tertiary">
          用米家设备事件或场景触发一次摄像头主动感知，并附带可选的属性和值筛选。
        </p>
      </section>

      {/* Event Mapping Creation Form */}
      <section className="rounded-xl border border-border bg-bg-secondary p-5 space-y-4">
        <div>
          <h2 className="text-title text-text-primary">事件映射</h2>
          <p className="text-caption text-text-tertiary">
            配置米家事件源对应触发哪些摄像头感知。创建后会按触发条件直接执行感知。
          </p>
        </div>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <div className="md:col-span-2">
            <label className="block text-caption text-text-secondary mb-2">触发方式</label>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
              {SOURCE_KIND_OPTIONS.map((option) => {
                const selected = sourceKind === option.value;
                return (
                  <button
                    key={option.value}
                    type="button"
                    className={
                      "rounded-lg border px-3 py-2 text-left transition-colors " +
                      (selected
                        ? "border-brand-primary bg-brand-soft text-brand-primary"
                        : "border-border bg-bg-primary text-text-secondary hover:border-border-strong hover:text-text-primary")
                    }
                    onClick={() => {
                      setSourceKind(option.value);
                      setSourceId("");
                      setSelectedEventKey("");
                      setDeviceSpec(null);
                      setPropFilters([]);
                    }}
                  >
                    <span className="block text-caption font-medium">{option.label}</span>
                    <span className="mt-0.5 block text-xs text-text-tertiary">{option.hint}</span>
                  </button>
                );
              })}
            </div>
          </div>
          <div>
            <label className="block text-caption text-text-secondary mb-1">事件源</label>
            <select
              className="w-full rounded-md border border-border bg-bg-primary px-3 py-2 text-caption"
              value={sourceId}
              onChange={(e) => onSourceChange(e.target.value)}
            >
              <option value="">-- 请选择 --</option>
              {sourceOptions.map((item) => (
                <option key={item.source_id} value={item.source_id}>
                  {item.source_name}
                  {item.room_name ? `（${item.room_name}）` : ""}
                  {` (${item.source_id.slice(-6)})`}
                </option>
              ))}
            </select>
          </div>
          <div className="md:col-span-2">
            <label className="block text-caption text-text-secondary mb-1">关联摄像头</label>
            <div className="flex flex-wrap gap-1.5">
              {cameras.map((cam) => (
                <button
                  key={cam.did}
                  type="button"
                  className={
                    "rounded-full border px-3 py-1 text-xs transition-colors " +
                    (cameraIds.includes(cam.did)
                      ? "bg-brand-soft border-brand-primary text-brand-primary"
                      : "bg-bg-primary border-border text-text-secondary hover:text-text-primary hover:border-border-strong")
                  }
                  onClick={() => setCameraIds((prev) => prev.includes(cam.did) ? prev.filter((id) => id !== cam.did) : [...prev, cam.did])}
                >
                  {cam.name || cam.did.slice(-6)}
                </button>
              ))}
            </div>
          </div>

          {/* Device Spec Property / Event Filters */}
          {sourceKind !== "scene" && sourceId && (
            <div className="md:col-span-2 space-y-2">
              {sourceKind === "device_event" ? (
                <div>
                  <label className="block text-caption text-text-secondary mb-1">
                    事件筛选 {specLoading ? "(加载spec中...)" : deviceSpec ? `(${deviceSpec.events?.length ?? 0} 个事件)` : ""}
                  </label>
                  <select
                    className="w-full rounded-md border border-border bg-bg-primary px-3 py-2 text-caption"
                    value={selectedEventKey}
                    onChange={(e) => {
                      setSelectedEventKey(e.target.value);
                      setPropFilters([]);
                    }}
                  >
                    <option value="">-- 选择事件 --</option>
                    {(deviceSpec?.events ?? []).map((event) => (
                      <option key={event.key} value={event.key}>
                        {event.name || event.description || event.key}
                      </option>
                    ))}
                  </select>
                  {!specLoading && deviceSpec && (deviceSpec.events?.length ?? 0) === 0 ? (
                    <div className="mt-2 rounded-md border border-dashed border-border bg-bg-primary px-3 py-2 text-caption text-text-tertiary">
                      还没有取到这个设备的事件定义。这里会按设备 MIoT Spec 展示可选事件和触发参数。
                    </div>
                  ) : null}
                </div>
              ) : null}
              <label className="block text-caption text-text-secondary">
                {sourceKind === "device_event" ? "触发参数（可选）" : "属性筛选"}{" "}
                {specLoading
                  ? "(加载spec中...)"
                  : sourceKind === "device_prop" && deviceSpec
                    ? `(${deviceSpec.properties.length} 个属性)`
                    : sourceKind === "device_event" && selectedEventKey
                      ? `(${eventArguments().length} 个参数)`
                      : ""}
              </label>
              {!specLoading && deviceSpec && sourceKind === "device_prop" && deviceSpec.properties.length === 0 ? (
                <div className="rounded-md border border-dashed border-border bg-bg-primary px-3 py-2 text-caption text-text-tertiary">
                  还没有取到这个设备的属性定义。这里会按设备 MIoT Spec 展示可选属性和值。
                </div>
              ) : null}
              {sourceKind === "device_event" && selectedEventKey && eventArguments().length === 0 ? (
                <div className="rounded-md border border-dashed border-border bg-bg-primary px-3 py-2 text-caption text-text-tertiary">
                  这个事件没有可配置参数；只要该事件发生就会触发。
                </div>
              ) : null}
              {propFilters.map((pf, idx) => {
                const options = sourceKind === "device_event" ? eventArguments() : (deviceSpec?.properties ?? []);
                const selectedProp = options.find((p) => p.key === pf.key);
                const valueOptions = getValueOptions(pf.key);
                const numericProp = isNumericProperty(selectedProp);
                const allowedOps = numericProp
                  ? FILTER_OP_OPTIONS
                  : FILTER_OP_OPTIONS.filter((item) => item.value === "eq" || item.value === "ne");
                return (
                  <div key={idx} className="flex gap-2 items-start">
                    <select
                      className="flex-1 rounded-md border border-border bg-bg-primary px-3 py-2 text-caption"
                      value={pf.key}
                      onChange={(e) => {
                        const nextKey = e.target.value;
                        const nextProp = options.find((p) => p.key === nextKey);
                        const nextNumeric = isNumericProperty(nextProp);
                        setPropFilters((prev) =>
                          prev.map((item, i) =>
                            i !== idx
                              ? item
                              : {
                                  ...item,
                                  key: nextKey,
                                  op: nextNumeric
                                    ? item.op
                                    : (item.op === "eq" || item.op === "ne" ? item.op : "eq"),
                                  value: item.value,
                                },
                          ),
                        );
                      }}
                    >
                      <option value="">-- 选择属性 --</option>
                      {options.map((prop) => (
                        <option key={prop.key} value={prop.key}>
                          {getPropertyDisplayName(prop, prop.key)} {prop.unit ? "(" + prop.unit + ")" : ""}
                        </option>
                      ))}
                    </select>
                    <select
                      className="w-36 rounded-md border border-border bg-bg-primary px-3 py-2 text-caption"
                      value={pf.op}
                      onChange={(e) => updatePropFilter(idx, "op", e.target.value)}
                    >
                      {allowedOps.map((op) => (
                        <option key={op.value} value={op.value}>
                          {op.label}
                        </option>
                      ))}
                    </select>
                    {valueOptions.length > 0 ? (
                      <select
                        className="flex-1 rounded-md border border-border bg-bg-primary px-3 py-2 text-caption"
                        value={pf.value}
                        onChange={(e) => updatePropFilter(idx, "value", e.target.value)}
                      >
                        {valueOptions.map((vo) => (
                          <option key={vo.value} value={vo.value}>
                            {vo.description || vo.value}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <input
                        className="flex-1 rounded-md border border-border bg-bg-primary px-3 py-2 text-caption"
                        placeholder={selectedProp?.value_range ? `${selectedProp.value_range.min} ~ ${selectedProp.value_range.max}` : numericProp ? "输入数值" : "输入属性值"}
                        value={pf.value}
                        onChange={(e) => updatePropFilter(idx, "value", e.target.value)}
                      />
                    )}
                    <button
                      type="button"
                      className="rounded-md border border-border px-2 py-2 text-caption text-red-600"
                      onClick={() => removePropFilter(idx)}
                    >
                      ×
                    </button>
                  </div>
                );
              })}
              <button
                type="button"
                className="rounded-md border border-dashed border-border px-3 py-1.5 text-xs text-text-tertiary"
                onClick={addPropFilter}
                disabled={sourceKind === "device_event" && (!selectedEventKey || eventArguments().length === 0)}
              >
                {sourceKind === "device_event" ? "+ 添加触发参数" : "+ 添加属性筛选"}
              </button>
            </div>
          )}

          <div>
            <label className="block text-caption text-text-secondary mb-1">感知提示（可选）</label>
            <p className="mb-1 text-xs text-text-tertiary">
              这是一段会附加到触发感知里的默认提示词，用来告诉模型重点看什么。
            </p>
            <input
              className="w-full rounded-md border border-border bg-bg-primary px-3 py-2 text-caption"
              placeholder="例如：重点看窗边是否有人、门窗是否打开、有没有异常动作"
              value={queryTemplate}
              onChange={(e) => setQueryTemplate(e.target.value)}
            />
          </div>
          <div>
            <label className="block text-caption text-text-secondary mb-1">冷却时间（秒）</label>
            <input
              className="w-full rounded-md border border-border bg-bg-primary px-3 py-2 text-caption"
              type="number"
              min={5}
              max={3600}
              value={cooldownSeconds}
              onChange={(e) => setCooldownSeconds(Number(e.target.value))}
            />
          </div>
          <div className="md:col-span-2">
            <label className="block text-caption text-text-secondary mb-1">备注</label>
            <input
              className="w-full rounded-md border border-border bg-bg-primary px-3 py-2 text-caption"
              placeholder="可选"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
            />
          </div>
        </div>
        <div className="sticky bottom-3 z-10 -mx-1 rounded-xl border border-border bg-bg-secondary/95 px-3 py-3 shadow-lg backdrop-blur">
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
            <span className="text-xs text-text-tertiary">{createHint}</span>
            <button
              type="button"
              className={
                "rounded-md px-5 py-2 text-caption font-medium transition-colors " +
                (createDisabled
                  ? "cursor-not-allowed bg-bg-tertiary text-text-tertiary"
                  : "bg-brand text-white hover:bg-brand-primary")
              }
              onClick={handleCreate}
              disabled={createDisabled}
            >
              保存触发配置
            </button>
          </div>
        </div>
      </section>

      {/* Existing Mappings */}
      <section className="rounded-xl border border-border bg-bg-secondary p-5 space-y-4">
        <div>
          <h2 className="text-title text-text-primary">已配置映射</h2>
          <p className="text-caption text-text-tertiary">
            {mappings.data?.length ?? 0} 条映射。命中映射后会按配置触发对应摄像头感知。
          </p>
        </div>
        <div className="space-y-3">
          {(mappings.data ?? []).map((item: MiotEventMapping) => (
            <div key={item.id} className="rounded-lg border border-border bg-bg-primary p-4">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="text-title text-text-primary">
                    {item.source_name_snapshot || item.source_id}
                  </div>
                  <div className="text-caption text-text-tertiary">
                    {getMappingKindLabel(item)} . 摄像头: {item.camera_dids.length} 个 . 冷却: {item.cooldown_seconds}s
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    className="rounded-md border border-border px-3 py-1.5 text-caption"
                    onClick={() => runTest(item)}
                  >
                    测试触发
                  </button>
                  <button
                    type="button"
                    className={"rounded-md px-3 py-1.5 text-caption " + (item.enabled ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-500")}
                    onClick={() => toggleEnabled(item)}
                  >
                    {item.enabled ? "已启用" : "已停用"}
                  </button>
                  <button
                    type="button"
                    className="rounded-md border border-border px-3 py-1.5 text-caption text-red-600"
                    onClick={async () => {
                      await deleteMiotEventMapping(item.id);
                      await reloadAll();
                    }}
                  >
                    删除
                  </button>
                </div>
              </div>
              {item.query_template ? (
                <div className="mt-3 text-caption text-text-secondary">
                  感知提示：{item.query_template}
                </div>
              ) : null}
              {item.event_kinds.some((kind) => kind.startsWith("event.")) ? (
                <div className="mt-3 text-caption text-text-secondary">
                  事件：{mappingSpecMeta[item.source_id]?.names[item.event_kinds[0]] || item.event_kinds[0]}
                </div>
              ) : null}
              {Object.keys(item.property_filters ?? {}).length > 0 ? (
                <div className="mt-3 flex flex-wrap gap-1.5">
                  {Object.entries(item.property_filters ?? {}).map(([key, value]) => (
                    <span
                      key={key}
                      className="rounded-full border border-border bg-bg-secondary px-2 py-0.5 text-xs text-text-secondary"
                    >
                      {(() => {
                        const cond = normalizeFilterCondition(value);
                        const specMeta = mappingSpecMeta[item.source_id];
                        const displayName = specMeta?.names[key] || key;
                        const displayValue =
                          cond.op === "any"
                            ? "任意值"
                            : getPropertyValueDisplayName(specMeta, key, String(cond.value));
                        return `${displayName} ${getFilterOpLabel(cond.op)} ${displayValue}`;
                      })()}
                    </span>
                  ))}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      </section>

      {/* Debug & Recent Triggers */}
      <section className="rounded-xl border border-border bg-bg-secondary p-5 space-y-4">
        <div>
          <h2 className="text-title text-text-primary">调试与最近触发</h2>
          <p className="text-caption text-text-tertiary">
            查看是否命中映射、是否发起感知、是否进入规则执行，以及跳过原因。
          </p>
        </div>
        <div className="space-y-3">
          {(logs.data ?? []).map((log: MiotEventTriggerLog) => (
            <div key={log.id} className="rounded-lg border border-border bg-bg-primary p-4">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="text-title text-text-primary">
                    {log.trigger.source_name || log.trigger.source_id}
                  </div>
                  <div className="text-caption text-text-tertiary">
                    {log.trigger.event_name} . {fmtTs(log.created_at)}
                  </div>
                </div>
                <div className="text-caption text-text-tertiary">
                  {log.error || log.skipped_reason || (log.perception_started ? "已感知" : "未感知")}
                </div>
              </div>
              {log.clip_kind && log.clip_device_ids?.length > 0 ? (
                <div className="mt-2 space-y-2">
                  {log.clip_device_ids.map((deviceId: string) => (
                    <video
                      key={deviceId}
                      src={getClipUrl(log.id, deviceId)}
                      controls
                      preload="metadata"
                      className="max-h-56 w-full rounded-md border border-border bg-black"
                    />
                  ))}
                </div>
              ) : log.snapshot_paths?.length > 0 ? (
                <div className="mt-2 flex gap-2 overflow-x-auto">
                  {log.snapshot_paths.map((p: string) => (
                    <img key={p} src={getSnapshotUrl(p)} className="max-h-32 rounded-md border border-border" alt="snapshot" />
                  ))}
                </div>
              ) : null}
              {log.perception_answer ? (
                <div className="mt-2 text-caption text-text-secondary whitespace-pre-wrap">
                  {log.perception_answer}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
