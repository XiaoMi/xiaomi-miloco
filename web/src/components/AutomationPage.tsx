import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  createMiotEventMapping,
  deleteMiotEventMapping,
  fetchDeviceSpec,
  listMiotEventMappings,
  testMiotEventTrigger,
  updateMiotEventMapping,
} from "@/api";
import type {
  MiotEventMapping,
  MiotEventSource,
  MiotPropertyFilterCondition,
  MiotPropertyFilterOp,
  ScopeCamera,
} from "@/lib/types";
import { useAsync } from "@/hooks/useAsync";
import { toast } from "./Toast";

interface Props {
  devices: MiotEventSource[];
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

type SourceKind = "device_prop" | "device_event";

// label/hint 存 i18n key，渲染时 t() 翻译。
const SOURCE_KIND_OPTIONS: { value: SourceKind; label: string; hint: string }[] = [
  { value: "device_prop", label: "automation.sourceKindProp", hint: "automation.sourceKindPropHint" },
  { value: "device_event", label: "automation.sourceKindEvent", hint: "automation.sourceKindEventHint" },
];

const FILTER_OP_OPTIONS: { value: MiotPropertyFilterOp; label: string }[] = [
  { value: "eq", label: "automation.opEq" },
  { value: "ne", label: "automation.opNe" },
  { value: "gt", label: "automation.opGt" },
  { value: "lt", label: "automation.opLt" },
  { value: "gte", label: "automation.opGte" },
  { value: "lte", label: "automation.opLte" },
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

// 返回 i18n key，调用方 t() 翻译。
function getFilterOpLabel(op: MiotPropertyFilterOp): string {
  switch (op) {
    case "eq":
      return "automation.opEq";
    case "ne":
      return "automation.opNe";
    case "gt":
      return "automation.opGt";
    case "lt":
      return "automation.opLt";
    case "gte":
      return "automation.opGte";
    case "lte":
      return "automation.opLte";
    case "any":
      return "automation.opAny";
    default:
      return op;
  }
}

function getMappingKindLabel(item: MiotEventMapping): string {
  return item.event_kinds.some((kind) => kind.startsWith("event."))
    ? "automation.sourceKindEvent"
    : "automation.sourceKindProp";
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

function compareText(a: string | null | undefined, b: string | null | undefined): number {
  return (a || "\uffff").localeCompare(b || "\uffff", "zh-CN", {
    numeric: true,
    sensitivity: "base",
  });
}

function sortEventSourcesByRoom(items: MiotEventSource[]): MiotEventSource[] {
  return [...items].sort((a, b) => {
    const roomCompare = compareText(a.room_name, b.room_name);
    if (roomCompare !== 0) return roomCompare;
    const nameCompare = compareText(a.source_name, b.source_name);
    if (nameCompare !== 0) return nameCompare;
    return compareText(a.source_id, b.source_id);
  });
}

export function AutomationPage({ devices, cameras }: Props) {
  const { t } = useTranslation();
  const mappings = useAsync(() => listMiotEventMappings(), [devices.length]);

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
    () => sortEventSourcesByRoom(devices),
    [devices],
  );

  async function reloadAll() {
    mappings.reload();
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
    if (!did) { setDeviceSpec(null); return; }
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
    if (!source) { toast(t("automation.toastSelectSource"), "warn"); return; }
    if (sourceKind === "device_event" && !selectedEventKey) {
      toast(t("automation.toastSelectEvent"), "warn");
      return;
    }
    try {
      const created = await createMiotEventMapping({
        source_type: "device",
        source_id: source.source_id,
        source_name_snapshot: source.source_name,
        camera_dids: cameraIds,
        enabled: true,
        query_template: queryTemplate,
        event_kinds: [
          sourceKind === "device_prop"
            ? "device_prop"
            : selectedEventKey,
        ],
        property_filters: getSelectedProp(),
        cooldown_seconds: cooldownSeconds,
        notes,
        created_at: null,
        updated_at: null,
      });
      mappings.mutate((items) => [created, ...(items ?? [])]);
      setSourceId("");
      setSourceKind("device_prop");
      setCameraIds([]);
      setQueryTemplate("");
      setCooldownSeconds(30);
      setNotes("");
      setPropFilters([]);
      setSelectedEventKey("");
      setDeviceSpec(null);
    } catch (err) {
      toast(
        err instanceof Error ? err.message : t("automation.toastCreateFailed"),
        "warn",
      );
    }
  }

  async function toggleEnabled(item: MiotEventMapping) {
    const updated = await updateMiotEventMapping(item.id, { enabled: !item.enabled });
    mappings.mutate((items) =>
      (items ?? []).map((entry) => (entry.id === updated.id ? updated : entry)),
    );
  }

  async function runTest(item: MiotEventMapping) {
    const changedProperties =
      Object.fromEntries(
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
          );
    const eventName =
      item.event_kinds.find((kind) => kind.startsWith("event.")) ?? "device_prop";
    await testMiotEventTrigger({
      source_type: item.source_type,
      source_id: item.source_id,
      source_name: item.source_name_snapshot,
      event_name: eventName,
      changed_properties: changedProperties,
    });
    await reloadAll();
  }

  const createDisabled = !sourceId || cameraIds.length === 0 || (sourceKind === "device_event" && !selectedEventKey);
  const createHint = !sourceId
    ? t("automation.hintSelectSource")
    : cameraIds.length === 0
      ? t("automation.hintSelectCamera")
      : sourceKind === "device_event" && !selectedEventKey
        ? t("automation.hintSelectEvent")
        : t("automation.hintReady");

  return (
    <div className="mx-auto max-w-5xl space-y-6 px-4 py-6">
      <section className="space-y-1">
          <h1 className="text-title text-text-primary">{t("automation.title")}</h1>
        <p className="text-caption text-text-tertiary">
          {t("automation.subtitle")}
        </p>
      </section>

      {/* Event Mapping Creation Form */}
      <section className="rounded-xl border border-border bg-bg-secondary p-5 space-y-4">
        <div>
          <h2 className="text-title text-text-primary">{t("automation.configTitle")}</h2>
          <p className="text-caption text-text-tertiary">
            {t("automation.configDesc")}
          </p>
        </div>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <div className="md:col-span-2">
            <label className="block text-caption text-text-secondary mb-2">{t("automation.triggerMethod")}</label>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
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
                    <span className="block text-caption font-medium">{t(option.label)}</span>
                    <span className="mt-0.5 block text-xs text-text-tertiary">{t(option.hint)}</span>
                  </button>
                );
              })}
            </div>
          </div>
          <div>
            <label className="block text-caption text-text-secondary mb-1">{t("automation.eventSource")}</label>
            <select
              className="w-full rounded-md border border-border bg-bg-primary px-3 py-2 text-caption"
              value={sourceId}
              onChange={(e) => onSourceChange(e.target.value)}
            >
              <option value="">{t("automation.selectPlaceholder")}</option>
              {sourceOptions.map((item) => (
                <option key={item.source_id} value={item.source_id}>
                  {item.room_name ? `${item.room_name} / ` : ""}
                  {item.source_name}
                  {` (${item.source_id.slice(-6)})`}
                </option>
              ))}
            </select>
          </div>
          <div className="md:col-span-2">
            <label className="block text-caption text-text-secondary mb-1">{t("automation.relatedCameras")}</label>
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
          {sourceId && (
            <div className="md:col-span-2 space-y-2">
              {sourceKind === "device_event" ? (
                <div>
                  <label className="block text-caption text-text-secondary mb-1">
                    {t("automation.eventFilter")} {specLoading ? t("automation.loadingSpec") : deviceSpec ? t("automation.eventCount", { count: deviceSpec.events?.length ?? 0 }) : ""}
                  </label>
                  <select
                    className="w-full rounded-md border border-border bg-bg-primary px-3 py-2 text-caption"
                    value={selectedEventKey}
                    onChange={(e) => {
                      setSelectedEventKey(e.target.value);
                      setPropFilters([]);
                    }}
                  >
                    <option value="">{t("automation.selectEvent")}</option>
                    {(deviceSpec?.events ?? []).map((event) => (
                      <option key={event.key} value={event.key}>
                        {event.name || event.description || event.key}
                      </option>
                    ))}
                  </select>
                  {!specLoading && deviceSpec && (deviceSpec.events?.length ?? 0) === 0 ? (
                    <div className="mt-2 rounded-md border border-dashed border-border bg-bg-primary px-3 py-2 text-caption text-text-tertiary">
                      {t("automation.noEventDef")}
                    </div>
                  ) : null}
                </div>
              ) : null}
              <label className="block text-caption text-text-secondary">
                {sourceKind === "device_event" ? t("automation.triggerParamOptional") : t("automation.propFilter")}{" "}
                {specLoading
                  ? t("automation.loadingSpec")
                  : sourceKind === "device_prop" && deviceSpec
                    ? t("automation.propCount", { count: deviceSpec.properties.length })
                    : sourceKind === "device_event" && selectedEventKey
                      ? t("automation.paramCount", { count: eventArguments().length })
                      : ""}
              </label>
              {!specLoading && deviceSpec && sourceKind === "device_prop" && deviceSpec.properties.length === 0 ? (
                <div className="rounded-md border border-dashed border-border bg-bg-primary px-3 py-2 text-caption text-text-tertiary">
                  {t("automation.noPropDef")}
                </div>
              ) : null}
              {sourceKind === "device_event" && selectedEventKey && eventArguments().length === 0 ? (
                <div className="rounded-md border border-dashed border-border bg-bg-primary px-3 py-2 text-caption text-text-tertiary">
                  {t("automation.noParamDef")}
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
                      <option value="">{t("automation.selectProp")}</option>
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
                          {t(op.label)}
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
                        placeholder={selectedProp?.value_range ? `${selectedProp.value_range.min} ~ ${selectedProp.value_range.max}` : numericProp ? t("automation.inputNumber") : t("automation.inputValue")}
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
                {sourceKind === "device_event" ? t("automation.addTriggerParam") : t("automation.addPropFilter")}
              </button>
            </div>
          )}

          <div>
            <label className="block text-caption text-text-secondary mb-1">{t("automation.perceptionHint")}</label>
            <p className="mb-1 text-xs text-text-tertiary">
              {t("automation.perceptionHintDesc")}
            </p>
            <input
              className="w-full rounded-md border border-border bg-bg-primary px-3 py-2 text-caption"
              placeholder={t("automation.perceptionPlaceholder")}
              value={queryTemplate}
              onChange={(e) => setQueryTemplate(e.target.value)}
            />
          </div>
          <div>
            <label className="block text-caption text-text-secondary mb-1">{t("automation.cooldown")}</label>
            <p className="mb-1 text-xs text-text-tertiary">
              {t("automation.cooldownDesc")}
            </p>
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
            <label className="block text-caption text-text-secondary mb-1">{t("automation.notes")}</label>
            <input
              className="w-full rounded-md border border-border bg-bg-primary px-3 py-2 text-caption"
              placeholder={t("automation.notesPlaceholder")}
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
                  ? "cursor-not-allowed border border-border bg-bg-tertiary text-text-tertiary"
                  : "border border-brand-primary bg-brand-primary text-white shadow-sm hover:brightness-95")
              }
              onClick={handleCreate}
              disabled={createDisabled}
            >
              {t("automation.save")}
            </button>
          </div>
        </div>
      </section>

      {/* Existing Mappings */}
      <section className="rounded-xl border border-border bg-bg-secondary p-5 space-y-4">
        <div>
          <h2 className="text-title text-text-primary">{t("automation.existingTitle")}</h2>
          <p className="text-caption text-text-tertiary">
            {t("automation.existingDesc", { count: mappings.data?.length ?? 0 })}
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
                    {t(getMappingKindLabel(item))} . {t("automation.camerasLabel")}: {t("automation.camerasCount", { count: item.camera_dids.length })} . {t("automation.cooldownLabel")}: {item.cooldown_seconds}s
                  </div>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    type="button"
                    className="rounded-md border border-border px-3 py-1.5 text-caption"
                    onClick={() => runTest(item)}
                  >
                    {t("automation.testTrigger")}
                  </button>
                  <button
                    type="button"
                    className={"rounded-md px-3 py-1.5 text-caption " + (item.enabled ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-500")}
                    onClick={() => toggleEnabled(item)}
                  >
                    {item.enabled ? t("automation.enabled") : t("automation.disabled")}
                  </button>
                  <button
                    type="button"
                    className="rounded-md border border-border px-3 py-1.5 text-caption text-red-600"
                    onClick={async () => {
                      await deleteMiotEventMapping(item.id);
                      mappings.mutate((items) =>
                        (items ?? []).filter((entry) => entry.id !== item.id),
                      );
                    }}
                  >
                    {t("automation.delete")}
                  </button>
                </div>
              </div>
              {item.query_template ? (
                <div className="mt-3 text-caption text-text-secondary">
                  {t("automation.perceptionHintLabel")}{item.query_template}
                </div>
              ) : null}
              {item.event_kinds.some((kind) => kind.startsWith("event.")) ? (
                <div className="mt-3 text-caption text-text-secondary">
                  {t("automation.eventLabel")}{mappingSpecMeta[item.source_id]?.names[item.event_kinds[0]] || item.event_kinds[0]}
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
                            ? t("automation.opAny")
                            : getPropertyValueDisplayName(specMeta, key, String(cond.value));
                        return `${displayName} ${t(getFilterOpLabel(cond.op))} ${displayValue}`;
                      })()}
                    </span>
                  ))}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
