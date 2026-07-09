/**
 * 主框架：左 Sidebar + 主区按 tab 切换。mobile 下 Sidebar 折叠为底部 nav。
 */

import { useEffect, useState } from "react";
import {
  getHomeStatus,
  listActivity,
  listCameras,
  listDevices,
  listHomeEntries,
  listPersons,
  listScenes,
  listScopeCameras,
  listScopeHomes,
  listTasks,
  refreshCameraOnline,
  pausePerception,
  resumePerception,
  toggleScopeCamera,
  toggleScopeCameraVoice,
  switchScopeHome,
} from "./api";
import { useAsync } from "./hooks/useAsync";
import type { Person } from "./lib/types";
import { Sidebar, MobileTabBar, type TabKey } from "./components/Sidebar";
import { SettingsDrawer } from "./components/SettingsDrawer";
import { HomeSwitcher } from "./components/HomeSwitcher";
import { StatusRibbon } from "./components/StatusRibbon";
import { HeroNow } from "./components/HeroNow";
import { DevicesByRoom } from "./components/DevicesByRoom";
import { ActivityFeed } from "./components/ActivityFeed";
import { FamilyStrip } from "./components/FamilyStrip";
import { PersonDrawer } from "./components/PersonDrawer";
import { PersonProfilePanel } from "./components/PersonProfilePanel";
import { HomeKnowledgePanel } from "./components/HomeKnowledgePanel";
import { TaskListPanel } from "./components/TaskListPanel";
import { CandidateReviewPanel } from "./components/CandidateReviewPanel";
import { MiotBindDialog } from "./components/MiotBindDialog";
import { ToastHost, toast } from "./components/Toast";
import { UsagePage } from "./components/UsagePage";
import type { HomeId } from "./lib/types";
import { PerfPage } from "./components/PerfPage";
import { IconMoon, IconSun } from "./lib/icons";
import { useTheme } from "./hooks/useTheme";
import { useTranslation } from "react-i18next";
import { LanguageSwitcher } from "./components/LanguageSwitcher";

/** URL hash 是 #perf 时,App 整屏渲染性能调试视图,跳过主框架。 */
function usePerfMode(): boolean {
  const read = () => {
    if (typeof window === "undefined") return false;
    return window.location.hash === "#perf";
  };
  const [on, setOn] = useState<boolean>(read);
  useEffect(() => {
    const handler = () => setOn(read());
    window.addEventListener("hashchange", handler);
    return () => window.removeEventListener("hashchange", handler);
  }, []);
  return on;
}

function PerfView() {
  const { t } = useTranslation();
  return (
    <div className="h-screen flex flex-col overflow-hidden bg-bg-primary text-text-primary">
      <header
        className="flex items-center justify-between px-5 md:px-8 border-b border-border bg-bg-secondary shrink-0"
        style={{ minHeight: 56 }}
      >
        <div className="flex items-baseline gap-2">
          <span className="text-title text-text-primary">{t("perfView.title")}</span>
          <span className="text-caption-mono text-text-tertiary">
            {t("perfView.subtitle")}
          </span>
        </div>
        <button
          type="button"
          onClick={() => {
            // 直接清掉 hash + 触发 hashchange 让 Root 切回 MainApp。
            // 用 history.pushState 保留 history,避免 href="#" 的滚动到顶 / 焦点跳。
            const url = new URL(window.location.href);
            url.hash = "";
            window.history.pushState({}, "", url.toString());
            window.dispatchEvent(new HashChangeEvent("hashchange"));
          }}
          className="text-caption px-3 py-1.5 rounded-md border border-border text-text-secondary hover:text-text-primary hover:border-border-strong transition-colors"
        >
          {t("perfView.back")}
        </button>
      </header>
      <main className="flex-1 overflow-y-auto min-h-0">
        <div className="max-w-[1200px] w-full mx-auto px-4 md:px-8 pt-5 pb-12">
          <PerfPage />
        </div>
      </main>
      <ToastHost />
    </div>
  );
}

/** 顶层切换器:debug mode 与主应用是两棵独立的 React 子树。
 *  这样切换时 hooks 序列不会发生数量变化,避免 Rules of Hooks 错误。 */
export function App() {
  const perfMode = usePerfMode();
  return perfMode ? <PerfView /> : <MainApp />;
}

function MainApp() {
  const { t } = useTranslation();
  // ── 当前家 ────────────────────────────────────────
  // backend 多家庭未上线,前端 homeId 永远 "primary"。切家走 onSwitchHome 调
  // switchScopeHome + window.location.reload(),不靠前端 homeId 状态触发 reload。
  const homeId: HomeId = "primary";

  // ── 数据加载（按当前家拉取；mock 家走 empty）─────────
  const status = useAsync(() => getHomeStatus(homeId), [homeId], {
    errorLabel: t("app.loadHomeStatusFail"),
  });
  const persons = useAsync(() => listPersons(homeId), [homeId], {
    errorLabel: t("app.loadPersonsFail"),
  });
  const cameras = useAsync(() => listCameras(homeId), [homeId], {
    errorLabel: t("app.loadCamerasFail"),
  });
  // 加载相机列表前先轻量刷新在线状态(/refresh_camera_online,只更新缓存元数据、
  // 不碰解码/流,故不卡流)——否则相机重新上线后 list_cameras_with_state 只读旧缓存,
  // 页面一直显"已离线"。节流 + 失败静默,不阻断列表。
  const scopeCameras = useAsync(
    async () => {
      await refreshCameraOnline(homeId).catch(() => {});
      return listScopeCameras(homeId);
    },
    [homeId],
    { errorLabel: t("app.loadScopeCamerasFail") },
  );
  const scopeHomes = useAsync(() => listScopeHomes(homeId), [homeId], {
    errorLabel: t("app.loadScopeHomesFail"),
  });
  const devices = useAsync(() => listDevices(homeId), [homeId], {
    errorLabel: t("app.loadDevicesFail"),
  });
  const scenes = useAsync(() => listScenes(homeId), [homeId], {
    errorLabel: t("app.loadScenesFail"),
  });
  const activity = useAsync(() => listActivity(homeId), [homeId], {
    errorLabel: t("app.loadActivityFail"),
  });
  // 家庭档案（候选区 + 正式区记忆）——家庭 tab 用，成员抽屉与非人面板共享。
  const home = useAsync(() => listHomeEntries(homeId), [homeId], {
    errorLabel: t("app.loadHomeEntriesFail"),
  });
  // miloco 为家庭创建的持续任务——家庭 tab 家庭档案卡下方展示。
  const tasks = useAsync(() => listTasks(homeId), [homeId], {
    errorLabel: t("app.loadTasksFail"),
  });

  // ── 字号 / 抽屉 / 弹层 ─────────────────────────
  // (原本有 now state + 30s setInterval 给 Sidebar 显示时间，现 Sidebar
  // 已不展示时间；HeroNow 的 cam card 内部各自维护 1min 时钟。)

  const [activeTab, setActiveTab] = useState<TabKey>("now");
  const [editingPerson, setEditingPerson] = useState<Person | null | undefined>(
    undefined,
  );
  // 打开 PersonDrawer 时是否直接进入身份录入流程（成员档案头部「录入身份」CTA 用）。
  const [enrollOnOpen, setEnrollOnOpen] = useState(false);
  // 家庭 tab 当前选中的成员（chip 选择器）——存 id，从 persons.data 解析当前
  // Person；null 时回退到第一位。改名 / 删除后随 reload 自动同步。
  const [selectedPersonId, setSelectedPersonId] = useState<string | null>(null);
  const [miotBindOpen, setMiotBindOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);

  // 米家家庭名直接走 backend `/api/miot/home::home_name`，米家给啥前端就显啥；
  // 未绑或 backend 没返时**不渲染** HomeSwitcher（未登录提示由头像 button 承担，
  // HomeSwitcher 不该兼任"未绑占位"角色）。
  // TopBar 直接 props 传 `scopeHomes.data` 给 HomeSwitcher。

  // ── 主区 tab 内容渲染 ────────────────────────────────────
  const renderTab = () => {
    switch (activeTab) {
      case "now": {
        // scopeCameras 进错误聚合：listScopeCameras 失败时（米家 SDK 限频 -704 /
        // 网络断），不能让 HeroNow 拿 `scopeCameras.data ?? []` 退化成"账号下没
        // 相机"假态——cameras（PerceptionCamera 子集）可能仍有值,会让 hero 显
        // 空但状态条显"在看家"语义割裂,且没有 retry 入口。
        // devices 也纳入聚合:HeroNow 用 devices 推 miotHasCamera,devices 拉
        // 失败时 `(devices.data ?? []).some(...)` 会兜底成 false → 米家上明明有
        // 摄像头但 hero 显"家里还没有摄像头",住户被误导去米家 app 加而非排查网络。
        const err = persons.error ?? cameras.error ?? scopeCameras.error ?? devices.error;
        if (err) {
          return (
            <TabPanelError
              message={t("app.tabHomeError", { msg: err.message })}
              onRetry={() => {
                persons.reload();
                cameras.reload();
                scopeCameras.reload();
                devices.reload();
              }}
            />
          );
        }
        if (!persons.data || !cameras.data || !scopeCameras.data || !devices.data) {
          return <TabPanelLoading text={t("app.tabHomeLoading")} />;
        }
        return (
          <div className="space-y-6">
            <HeroNow
              persons={persons.data}
              cameras={cameras.data}
              scopeCameras={scopeCameras.data}
              miotHasCamera={devices.data.some(
                (d) => d.category === "camera",
              )}
              /* 投喂上限唯一来源:后端 MAX_ENABLED_CAMERAS，经 /api/miot/status 下发。
                 status 未到/出错时兜底 4，与后端默认一致。 */
              maxStreamCams={status.data?.maxEnabledCameras ?? 4}
              /* 概览页家人 chip 不跳转 —— family tab 才走 PersonDrawer 流。
                 不传 onPersonClick → PersonChip 降级成 div（无 hover/点击反馈）,
                 防住户看到可点 button 形态点了无反馈以为系统坏。 */
              onJumpUsage={() => setActiveTab("usage")}
              onToggleCameras={async (dids, inUse) => {
                try {
                  await toggleScopeCamera(dids, inUse);
                } catch (e) {
                  toast(
                    e instanceof Error ? e.message : t("common.switchFailed"),
                    "warn",
                  );
                }
                // 三个 reload —— LivePlayer iframe src 用 useRef 按 cameraDid 锁住,
                // channelByDid useMemo + iframe React diff 双层防 src 变化触发
                // iframe 重 mount,reload cameras 安全。新接入 cam 时 cameras.reload
                // 才能拿到 channel,不 reload 会让多通道 cam 永远兜底 channel=0。
                scopeCameras.reload();
                cameras.reload();
                status.reload();
              }}
              onToggleCameraVoice={async (did, voiceInUse) => {
                try {
                  await toggleScopeCameraVoice([did], voiceInUse);
                } catch (e) {
                  toast(
                    e instanceof Error ? e.message : t("common.switchFailed"),
                    "warn",
                  );
                }
                // 拾音开关只改 KV 偏好,不动投喂/流(音频在引擎入口按 KV 实时剥离),
                // 只需 reload scopeCameras 拿新 voiceInUse。
                scopeCameras.reload();
              }}
            />
          </div>
        );
      }
      case "devices": {
        const err = devices.error ?? scenes.error;
        if (err) {
          return (
            <TabPanelError
              message={t("app.tabDevicesError", { msg: err.message })}
              onRetry={() => {
                devices.reload();
                scenes.reload();
              }}
            />
          );
        }
        if (!devices.data || !scenes.data) {
          return <TabPanelLoading text={t("app.tabDevicesLoading")} />;
        }
        return (
          <div className="space-y-6">
            <DevicesByRoom
              devices={devices.data}
              scenes={scenes.data}
              onChanged={() => {
                devices.reload();
                scenes.reload();
              }}
            />
          </div>
        );
      }
      case "family": {
        if (persons.error) {
          return (
            <TabPanelError
              message={t("app.tabFamilyError", { msg: persons.error.message })}
              onRetry={() => persons.reload()}
            />
          );
        }
        if (!persons.data) {
          return <TabPanelLoading text={t("app.tabFamilyLoading")} />;
        }
        // 单页上下布局：chip 选择器默认选中第一位成员；选中某成员 → 同卡内就地展开
        // 其档案（至少保持一个选中，不可取消）。下方依次是家庭档案、观察中聚合卡。
        const selectedPerson =
          persons.data.find((p) => p.id === selectedPersonId) ??
          persons.data[0] ??
          null;
        return (
          <div className="space-y-6">
            <FamilyStrip
              persons={persons.data}
              selectedId={selectedPerson?.id ?? null}
              onSelect={(p) => setSelectedPersonId(p.id)}
              onAddPerson={() => setEditingPerson(null)}
            />
            {selectedPerson && (
              <PersonProfilePanel
                person={selectedPerson}
                entries={home.data}
                loading={home.loading}
                onEdit={() => {
                  setEnrollOnOpen(false);
                  setEditingPerson(selectedPerson);
                }}
                onEnroll={() => {
                  setEnrollOnOpen(true);
                  setEditingPerson(selectedPerson);
                }}
                onChanged={() => {
                  home.reload();
                  persons.reload();
                }}
              />
            )}
            <HomeKnowledgePanel
              data={home.data}
              persons={persons.data}
              loading={home.loading}
              onChanged={() => home.reload()}
            />
            <TaskListPanel
              tasks={tasks.data}
              loading={tasks.loading}
              onChanged={() => tasks.reload()}
            />
            <CandidateReviewPanel
              data={home.data}
              onChanged={() => home.reload()}
            />
          </div>
        );
      }
      case "activity": {
        if (activity.error) {
          return (
            <TabPanelError
              message={t("app.tabActivityError", { msg: activity.error.message })}
              onRetry={() => activity.reload()}
            />
          );
        }
        if (!activity.data) {
          return <TabPanelLoading text={t("app.tabActivityLoading")} />;
        }
        return (
          <div className="space-y-6">
            <ActivityFeed events={activity.data} homeId={homeId} />
          </div>
        );
      }
      case "usage":
        return <UsagePage />;
    }
  };

  return (
    <div className="h-screen flex overflow-hidden bg-bg-primary text-text-primary">
      {/* 左 Sidebar 固定不滚动:内部 nav 自己 overflow-y-auto,米家头像固定在 left-bottom */}
      <Sidebar
        active={activeTab}
        onChange={setActiveTab}
        miot={status.data?.miot}
        onOpenMiotBind={() => setMiotBindOpen(true)}
        onMiotChanged={() => window.location.reload()}
        onOpenSettings={() => setSettingsOpen(true)}
      />

      {/* 主区:固定高度 + flex-col,TopBar/StatusRibbon 顶在上面,只 main 区滚 */}
      <div className="flex-1 flex flex-col min-w-0 min-h-0">
        {/* 顶部 bar(shrink-0,跟随主区不滚动) */}
        <TopBar
          miotBound={status.data?.miot.bound}
          homes={scopeHomes.data ?? []}
          onSwitchHome={async (targetHomeId) => {
            // 切家走 backend 端点 PUT /api/miot/scope/homes:
            // 单事务把 home_id 设为唯一 in_use=true,其它全 false。代替之前的
            // "先 add 目标 → 再 remove 其它" 两步序列(step1 成功 step2 失败时启用集
            // 半态需要 reload + sessionStorage cross-reload toast 兜底),后端一步搞定。
            const target = (scopeHomes.data ?? []).find(
              (h) => h.homeId === targetHomeId,
            );
            const okToast = t("app.switchedTo", {
              name: target?.homeName ?? t("app.anotherHome"),
            });
            try {
              await switchScopeHome(targetHomeId);
            } catch (e) {
              toast(e instanceof Error ? e.message : t("common.switchFailed"), "warn");
              // backend atomic 端点失败 = 状态没改,UI 跟 backend 真态对齐让 TopBar
              // 不显错误的"已切目标"假态。
              scopeHomes.reload();
              return;
            }
            // 切家成功 → reload 让所有 useAsync 拉到新 home 的设备/cameras/scenes,
            // sessionStorage 通道把 toast 跨 reload 显出来(直接 toast 会被即时
            // unmount 的 ToastHost 吞)。
            try {
              sessionStorage.setItem(
                "miloco_pending_toast",
                JSON.stringify({ text: okToast, tone: "ok" }),
              );
            } catch {
              /* sessionStorage 不可用降级 */
            }
            window.location.reload();
          }}
        />

        {/* 状态条(shrink-0) */}
        {status.data && (
          <StatusRibbon
            status={status.data}
            allCamerasOff={
              // 未就绪时兜底 false（宁可短暂显"在看家"，不误报"待机中"）
              // 注意：不要加 data.length > 0 守卫——[].every() 本就返回 true（空集全称量化），
              // 无摄像头家庭（scopeCameras.data === []）应正确显示"待机中"而非"在看家"。
              !scopeCameras.loading &&
              !scopeCameras.error &&
              !!scopeCameras.data &&
              scopeCameras.data.every((c) => !c.inUse)
            }
            onConnectMiot={() => setMiotBindOpen(true)}
            onWakeUp={async () => {
              try {
                await resumePerception();
                // 跟 onRestartEngine "引擎已重启" 同款成功 toast——都是 StatusRibbon cta,
                // 反馈一致住户才能确认操作真生效（ribbon 颜色变化是 reload 后异步的,
                // ~50ms,无 toast 易让住户怀疑没生效）。
                toast(t("app.woke"), "ok");
                status.reload();
              } catch (e) {
                toast(e instanceof Error ? e.message : t("app.wakeFail"), "warn");
              }
            }}
            onJumpDevices={() => setActiveTab("devices")}
            onRestartEngine={async () => {
              // 拆两段 try：pause 失败 → 引擎仍跑（无副作用）；resume 失败 →
              // 引擎已停下需要住户手动唤醒。两种 toast 文案不同避免割裂——
              // 否则 pause 成功 + resume 失败时住户看到笼统"重启失败"，但 reload 后
              // StatusRibbon 跳到蓝色"休息中"，跟"重启失败"的语义错位。
              try {
                await pausePerception();
              } catch (e) {
                toast(
                  e instanceof Error
                    ? t("app.restartFailEngineRunning", { msg: e.message })
                    : t("app.restartFailEngineRunningNoMsg"),
                  "warn",
                );
                status.reload();
                return;
              }
              try {
                await resumePerception();
                toast(t("app.engineRestarted"), "ok");
              } catch (e) {
                toast(
                  e instanceof Error
                    ? t("app.engineStoppedWakeManually", { msg: e.message })
                    : t("app.engineStoppedWakeManuallyNoMsg"),
                  "warn",
                );
              }
              status.reload();
            }}
          />
        )}

        {/* tab 内容——这里是唯一会滚的区域 */}
        <main className="flex-1 overflow-y-auto min-h-0">
          <div className="max-w-[1200px] w-full mx-auto px-4 md:px-8 pt-5 pb-12">
            {renderTab()}
          </div>
        </main>

        {/* mobile 底部 tab bar(在主区底部,不会被 main 滚走) */}
        <div className="md:hidden shrink-0">
          <MobileTabBar
            active={activeTab}
            onChange={setActiveTab}
            miot={status.data?.miot}
            onOpenMiotBind={() => setMiotBindOpen(true)}
            onMiotChanged={() => window.location.reload()}
            onOpenSettings={() => setSettingsOpen(true)}
          />
        </div>
      </div>

      {/* 弹层 */}
      <PersonDrawer
        person={editingPerson === undefined ? null : editingPerson}
        open={editingPerson !== undefined}
        startEnrolling={enrollOnOpen}
        cameras={cameras.data ?? []}
        onClose={() => {
          setEditingPerson(undefined);
          setEnrollOnOpen(false);
        }}
        onChanged={() => {
          persons.reload();
          status.reload();
          home.reload();
        }}
      />
      <MiotBindDialog
        open={miotBindOpen}
        onClose={() => setMiotBindOpen(false)}
        onDone={() => {
          // 跟切家失败的 cross-reload toast 走同一根管子：直接 toast() 会被
          // 紧跟的 reload unmount ToastHost 立即吞掉,住户看不见绑定成功反馈。
          // 写 sessionStorage,reload 后 ToastHost mount 时 pop 出来显示。
          try {
            sessionStorage.setItem(
              "miloco_pending_toast",
              JSON.stringify({ text: t("app.miotBound"), tone: "ok" }),
            );
          } catch {
            /* sessionStorage 不可用降级,不影响 reload */
          }
          window.location.reload();
        }}
      />

      <SettingsDrawer open={settingsOpen} onClose={() => setSettingsOpen(false)} />

      <ToastHost />
    </div>
  );
}

/**
 * 顶部 bar——桌面端把家庭切换器作为标题位(同行加"切换"二字),
 * mobile 端简化版与 sidebar 互斥。
 */
function TopBar({
  miotBound,
  homes,
  onSwitchHome,
}: {
  miotBound: boolean | undefined;
  homes: { homeId: string; homeName: string; inUse: boolean }[];
  onSwitchHome: (homeId: string) => void;
}) {
  const { effective, toggle } = useTheme();
  const { t } = useTranslation();
  // 当前 in_use 的家（已启用）—— 多个 in_use 时取首个；
  // 后端 list_homes 兜底保证至少一个 in_use=true（含启用家失效后自动重选），
  // 此处 ?? sortedHomes[0] 仅作防御性 fallback。
  // 显式按 homeId 字典序排——跟 backend `service.py::get_home_info` home_name
  // 选取的 `sorted(homes, key=home_id)` 保持一致,否则 TopBar 高亮的家可能跟
  // hero 区设备/相机的 home（backend 选的）错位。realListScopeHomes 已排,这里
  // 再排一次让前后端对齐 invariant 显式不依赖隐式约定。
  const sortedHomes = [...homes].sort((a, b) =>
    a.homeId < b.homeId ? -1 : a.homeId > b.homeId ? 1 : 0,
  );
  const currentHome = sortedHomes.find((h) => h.inUse) ?? sortedHomes[0];

  return (
    <header
      className="flex items-center justify-between gap-4 px-5 md:px-8 border-b border-border bg-bg-secondary"
      style={{ minHeight: 64 }}
    >
      <div className="flex items-center gap-3 min-w-0">
        {/* 未绑米家时不渲染 HomeSwitcher——空数据下"我的家"占位是加戏；
            未登录的提示由 Sidebar 左下头像 button 的黄状态点 + 点击弹绑定 dialog 承担。*/}
        {miotBound && currentHome && (
          <HomeSwitcher
            currentHomeId={currentHome.homeId}
            homes={sortedHomes.map((h) => ({ id: h.homeId, name: h.homeName }))}
            onSwitch={onSwitchHome}
          />
        )}
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <LanguageSwitcher />
        <button
          type="button"
          onClick={toggle}
          className="inline-flex items-center justify-center rounded-md text-text-secondary hover:bg-bg-tertiary hover:text-text-primary transition-colors"
          style={{ width: 32, height: 32 }}
          aria-label={effective === "dark" ? t("theme.toLight") : t("theme.toDark")}
          title={effective === "dark" ? t("theme.toLight") : t("theme.toDark")}
        >
          {effective === "dark" ? <IconSun /> : <IconMoon />}
        </button>
      </div>
    </header>
  );
}

function TabPanelLoading({ text }: { text: string }) {
  return (
    <div
      className="rounded-xl bg-bg-secondary border border-border shadow-sm p-12 text-center text-text-secondary anim-in"
      role="status"
      aria-live="polite"
    >
      <div className="inline-flex items-center gap-2">
        <span className="inline-block w-2 h-2 rounded-full bg-text-tertiary animate-pulse" />
        {text}
      </div>
    </div>
  );
}

function TabPanelError({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div
      className="rounded-xl bg-bg-secondary border border-error shadow-sm p-8 text-center anim-in"
      role="alert"
    >
      <div className="text-title text-error mb-3 font-normal">{message}</div>
      <button
        type="button"
        onClick={onRetry}
        className="text-body px-4 py-2 rounded-lg bg-bg-primary border border-border text-text-secondary hover:text-text-primary"
      >
        {t("common.retry")}
      </button>
    </div>
  );
}
