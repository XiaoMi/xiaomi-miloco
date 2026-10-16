import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { getPerceptionFlow } from "@/api";
import type { PerceptionFlowResult } from "@/api";
import { useAsync } from "@/hooks/useAsync";
import type {
  GraphDeviceSummary,
  GraphProtocolWarning,
  GraphResponse,
} from "@/lib/types";
import { GenericGraphViewer } from "./GenericGraphViewer";

export function perceptionFlowRequestDeps(
  deviceId: string | undefined,
  refreshNonce: number,
  refreshSec: number,
): [string, number, number] {
  return [deviceId ?? "all", refreshNonce, refreshSec];
}

export function perceptionFlowRefreshDelayMs(refreshSec: number): number {
  return refreshSec * 1000;
}

export function firstPerceptionFlowDeviceId(
  devices: readonly GraphDeviceSummary[],
): string | undefined {
  return devices[0]?.device_id;
}

// 无设备时全局图 scope.device_id 为 null,而选中态是 undefined —— 两种"未选设备"
// 必须视为同一回事,否则空设备场景全局图永不显示。
export function scopeMatchesSelection(
  scopeDeviceId: string | null | undefined,
  selectedDeviceId: string | undefined,
): boolean {
  return (scopeDeviceId ?? null) === (selectedDeviceId ?? null);
}

interface PerfPipelineFlowViewProps {
  graph: GraphResponse | undefined;
  devices: readonly GraphDeviceSummary[];
  loading: boolean;
  error: Error | undefined;
  protocolWarnings: GraphProtocolWarning[];
  selectedDeviceId: string | undefined;
  onDeviceChange: (deviceId: string) => void;
  onRefresh: () => void;
}

function deviceLabel(device: GraphResponse["summary"]["devices"][number]): string {
  return device.room_name
    ? `${device.room_name} · ${device.device_id}`
    : device.device_id;
}

function freshnessKey(graph: GraphResponse): string {
  const freshness = graph.summary.freshness;
  if (freshness === "stale") return "perf.flowFreshnessStale";
  if (freshness === "unknown") return "perf.flowFreshnessUnknown";
  return "perf.flowFreshnessFresh";
}

export function PerfPipelineFlowView({
  graph,
  devices,
  loading,
  error,
  protocolWarnings,
  selectedDeviceId,
  onDeviceChange,
  onRefresh,
}: PerfPipelineFlowViewProps) {
  const { t, i18n } = useTranslation();

  return (
    <section
      className="rounded-xl border border-border bg-bg-secondary p-4 shadow-sm md:p-5"
      aria-labelledby="perception-flow-title"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2
            id="perception-flow-title"
            className="text-heading font-semibold text-text-primary"
          >
            {t("perf.flowTitle")}
          </h2>
          <p className="mt-1 text-caption text-text-secondary">
            {t("perf.flowSubtitle")}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-caption text-text-secondary" htmlFor="perception-flow-device">
            {t("perf.flowScope")}
          </label>
          <select
            id="perception-flow-device"
            value={selectedDeviceId ?? ""}
            onChange={(event) => onDeviceChange(event.target.value)}
            disabled={devices.length === 0}
            className="max-w-64 rounded-md border border-border bg-bg-primary px-2 py-1.5 text-caption text-text-primary"
          >
            {devices.map((device) => (
              <option key={device.device_id} value={device.device_id}>
                {deviceLabel(device)}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={onRefresh}
            className="rounded-md border border-border px-3 py-1.5 text-caption text-text-secondary transition-colors hover:border-border-strong hover:text-text-primary"
          >
            {t("perf.flowRefresh")}
          </button>
        </div>
      </div>

      {protocolWarnings.length > 0 ? (
        <div
          className="mt-4 rounded-lg border border-warning bg-warning/5 p-3 text-caption text-text-primary"
          role="status"
        >
          <div className="font-semibold">{t("perf.flowProtocolWarning")}</div>
          <ul className="mt-1 list-disc space-y-1 pl-5">
            {protocolWarnings.map((warning) => (
              <li key={`${warning.path}:${warning.value}`}>{warning.message}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {error ? (
        <div
          className="mt-4 rounded-lg border border-error bg-error/5 p-4 text-body text-error"
          role="alert"
        >
          {t("perf.flowProtocolError", { message: error.message })}
        </div>
      ) : null}

      {loading && !graph ? (
        <div className="mt-4 rounded-lg border border-border p-8 text-center text-text-secondary">
          {t("perf.flowLoading")}
        </div>
      ) : null}

      {graph ? (
        <div className="mt-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2 text-caption text-text-secondary">
            <span>{t(freshnessKey(graph))}</span>
            <span>
              {t("perf.flowGenerated", {
                time: new Date(graph.generated_at).toLocaleString(i18n.language),
              })}
              {loading ? ` · ${t("perf.flowRefreshing")}` : ""}
            </span>
          </div>
          <GenericGraphViewer graph={graph} />
        </div>
      ) : null}
    </section>
  );
}

export function PerfPipelineFlow({
  refreshNonce,
  refreshSec,
}: {
  refreshNonce: number;
  refreshSec: number;
}) {
  const [selectedDeviceId, setSelectedDeviceId] = useState<string | undefined>();
  const [devices, setDevices] = useState<GraphDeviceSummary[]>([]);
  const requestDeps = perceptionFlowRequestDeps(
    selectedDeviceId,
    refreshNonce,
    refreshSec,
  );
  const request = useAsync<PerceptionFlowResult>(
    () => getPerceptionFlow(selectedDeviceId),
    requestDeps,
  );
  const reloadRef = useRef(request.reload);
  reloadRef.current = request.reload;

  useEffect(() => {
    const responseDevices = request.data?.graph.summary.devices;
    if (!responseDevices) return;
    setDevices(responseDevices);
    if (!selectedDeviceId) {
      setSelectedDeviceId(firstPerceptionFlowDeviceId(responseDevices));
      return;
    }
    if (!responseDevices.some((device) => device.device_id === selectedDeviceId)) {
      setSelectedDeviceId(firstPerceptionFlowDeviceId(responseDevices));
    }
  }, [request.data?.graph.summary.devices, selectedDeviceId]);

  useEffect(() => {
    const timer = window.setInterval(
      () => void reloadRef.current(),
      perceptionFlowRefreshDelayMs(refreshSec),
    );
    const onVisible = () => {
      if (document.visibilityState === "visible") void reloadRef.current();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [refreshSec]);

  const displayedResult = scopeMatchesSelection(
    request.data?.graph.scope.device_id,
    selectedDeviceId,
  )
    ? request.data
    : undefined;

  return (
    <PerfPipelineFlowView
      graph={displayedResult?.graph}
      devices={devices}
      loading={request.loading}
      error={request.error}
      protocolWarnings={displayedResult?.warnings ?? []}
      selectedDeviceId={selectedDeviceId}
      onDeviceChange={setSelectedDeviceId}
      onRefresh={() => void request.reload()}
    />
  );
}
