# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""miloco 后端统一配置入口。

- 优先级：环境变量（``MILOCO_*``）> ``$MILOCO_HOME/config.json``（用户可编辑）
  > ``config/settings.yaml``（后端默认）> 代码默认值
- 单例访问：``get_settings() -> MilocoSettings``
- 派生路径：``DirectorySettings`` 的 ``image_dir`` / ``log_dir`` /
  ``miot_cache_dir`` / ``static_dir`` 均由 ``$MILOCO_HOME`` 与 ``storage`` 计算。
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

import yaml
from pydantic import BaseModel, Field, computed_field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from miloco.utils.paths import config_file as _user_config_path
from miloco.utils.paths import miloco_home as _resolve_miloco_home

logger = logging.getLogger(__name__)

# ─── 路径常量 ────────────────────────────────────────────────────────────────

_CONFIG_DIR = Path(__file__).parent
_BACKEND_ROOT = _CONFIG_DIR.parent  # miloco/src/miloco
_SETTINGS_YAML = _CONFIG_DIR / "settings.yaml"
_SETTINGS_SCHEMA = _CONFIG_DIR / "settings.schema.json"


# ─── 子模型 ──────────────────────────────────────────────────────────────────


class ServerSettings(BaseModel):
    """miloco 后端服务的网络 / 启动 / 鉴权相关配置。"""

    url: str = Field(
        default="http://127.0.0.1:1810",
        description="CLI 与插件访问 miloco 后端的 HTTP Base URL（永远 HTTP；跨网加密走反代）",
    )
    token: str = Field(
        default="",
        description="CLI 与插件访问后端时使用的 Bearer Token；为空时由后端首次启动自动生成",
    )
    tls_verify: bool = Field(
        default=False,
        description="CLI 访问后端时是否校验 TLS 证书；当前 backend 永远 HTTP 故无作用，保留供未来反代场景",
    )
    python_bin: str = Field(
        default="",
        description="用于启动 miloco-backend 的 Python 解释器绝对路径（install.sh 探测后写入）",
    )
    # 非 user-facing 的运行参数（仅从 settings.yaml 读取，不进 schema.json）
    host: str = Field(
        default="127.0.0.1",
        description=(
            "后端进程监听的 host。默认 127.0.0.1 跟 spa_handler trust model 对齐:"
            "token 嵌 HTML body 公开返回,凡能 GET / 的网络位置都拿到 token 后能调"
            "任意 /api/*。住户/笔记本要从 LAN 其它机器访问时,在 ~/.openclaw/miloco/"
            "config.json 或 settings.yaml 改成 0.0.0.0,自行评估 LAN 是否可信"
            "(私网+单管理员 OK,共享网络/路由器穿透应改反代+TLS+认证)。"
        ),
    )
    port: int = Field(default=1810, description="后端进程监听的端口")
    log_level: str = Field(default="info", description="后端 uvicorn 日志级别")
    enable_console_logging: bool = Field(
        default=True, description="是否启用后端控制台日志"
    )
    tls_certfile: str = Field(
        default="",
        deprecated=True,
        description="【已废弃】backend 永远 HTTP，跨网加密走反向代理（nginx / cloudflare-tunnel）+ 真证书；本字段保留仅为兼容旧 config，不会生效",
    )
    tls_keyfile: str = Field(
        default="",
        deprecated=True,
        description="【已废弃】见 tls_certfile",
    )


class AgentSettings(BaseModel):
    """Agent webhook 出站调用配置（与具体 agent 平台无关）。"""

    webhook_url: str = Field(
        default="http://127.0.0.1:18789/miloco/webhook",
        description="agent webhook 回调地址",
    )
    auth_bearer: str = Field(
        default="",
        description="agent webhook 鉴权 Bearer 值；为空时不发送 Authorization 头",
    )


class DispatcherSettings(BaseModel):
    """Agent 投递调度参数（内部运行参数，仅从 settings.yaml 读取，不进 schema.json）。"""

    turn_wait_timeout_ms: int = Field(
        default=180_000,
        description="dispatcher 同步等待单个 agent turn 结束的超时（毫秒）。",
    )
    max_queue: int = Field(
        default=10,
        description=(
            "每会话 dispatcher 队列上限；超出时按 (类型优先级, 条目级优先级, 入队时间) 淘汰最不紧急者。"
        ),
    )
    message_ttl_sec: float = Field(
        default=300.0,
        description=(
            "待发送消息在队列中的最大存活时长（秒）；入队龄超过该值的消息在"
            "「新消息入队」与「打包发送前」两处被清理，不再投递。<=0 = 关闭过期。"
        ),
    )
    # 不加 ge=0.0：与 notify.dedup_window_sec 一致，<=0 语义为「关闭过期」；
    # 加 ge 会让误配负值直接崩掉整个 settings 加载（后端起不来）。


class OmniModelSettings(BaseModel):
    """多模态大模型（omni）配置，默认使用小米 MiMo。"""

    label: str = Field(
        default="",
        description="档案显示名（可选，仅供 web 展示）；为空时前端回退为 model · 域名。",
    )
    model: str = Field(
        default="xiaomi/mimo-v2.5",
        description="多模态模型标识（provider/model）",
    )
    base_url: str = Field(
        default="https://api.xiaomimimo.com/v1",
        description="多模态模型服务 Base URL（需兼容 OpenAI-compatible 协议）",
    )
    api_key: str = Field(
        default="",
        description="多模态模型 API Key；为空时视为未配置，插件与后端启动前校验",
    )


class ModelSettings(BaseModel):
    """miloco 使用的第三方模型配置集合。"""

    omni: OmniModelSettings = Field(
        default_factory=OmniModelSettings,
        description="当前生效的多模态大模型（omni）配置，默认使用小米 MiMo",
    )
    omni_profiles: list[OmniModelSettings] = Field(
        default_factory=list,
        description=(
            "已保存的 omni 配置档案列表（供 web 切换）。每项为一套 "
            "{label, base_url, api_key, model}；label 为唯一标识，"
            "当前生效的那套即 omni（按 label 匹配）。"
        ),
    )


class DatabaseSettings(BaseModel):
    """SQLite 数据库连接参数。"""

    path: str = Field(
        default="miloco.db", description="数据库文件路径（相对 storage 或绝对路径）"
    )
    timeout: float = Field(default=30.0, description="SQLite 连接超时（秒）")
    check_same_thread: bool = Field(default=False, description="是否强制单线程访问")
    isolation_level: str | None = Field(
        default=None, description="SQLite 事务隔离级别；None 表示默认"
    )


@functools.lru_cache(maxsize=1)
def _resolve_version() -> str:
    """运行时版本：优先读已安装包元数据，未安装时回退 git describe，最后兜底。

    纯展示用，导入期绝不抛异常。get_settings() 自带 lru_cache 已保证只算一次，
    这里再加 lru_cache 是防御未来非缓存路径重复 fork git。
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("miloco")
    except PackageNotFoundError:
        import subprocess

        try:
            out = subprocess.run(
                ["git", "describe", "--tags", "--always", "--dirty"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except Exception:
            pass
        return "0.0.0+unknown"


class AppSettings(BaseModel):
    """FastAPI 应用元信息。"""

    title: str = Field(default="Miloco backend", description="OpenAPI 标题")
    service_name: str = Field(default="miloco-backend", description="服务标识")
    description: str = Field(
        default="Miloco backend, responsible for scheduling various modules",
        description="OpenAPI 描述",
    )
    version: str = Field(default_factory=_resolve_version, description="服务版本号")


class MiotSettings(BaseModel):
    """MIoT 云接入参数。"""

    cloud_server: str = Field(
        default="cn", description="MIoT 云区域（cn/de/i2/ru/sg/us）"
    )


class NotifySettings(BaseModel):
    """通知发送相关运行参数。"""

    dedup_window_sec: float = Field(
        default=60.0,
        description="相同通知文案在此窗口（秒）内只发一次；<=0 = 关闭去重。",
    )
    # 不加 ge=0.0：MessageDeduper 已把 window_sec<=0 当作「关闭去重」，与 TS 侧
    # getNotifyDedupWindowMs 的归零语义一致。加了 ge 会让误配负值直接崩掉整个
    # settings 加载（后端起不来），对一个可选兜底旋钮是过度约束。


class CameraSettings(BaseModel):
    """摄像头采集参数。"""

    frame_interval: int = Field(default=1000, description="帧采集间隔（毫秒）")
    max_cache_images: int = Field(default=6, description="最大缓存图像数量")


class RuleSettings(BaseModel):
    """规则引擎相关配置。"""

    log_ttl: int = Field(default=30, description="规则日志保留天数")
    default_duration_ratio: float = Field(
        default=0.6,
        gt=0.0,
        le=1.0,
        description=(
            "duration_seconds 窗口内 True 比例阈值的默认值；"
            "规则创建时未显式指定 --duration-ratio 则使用此值"
        ),
    )


class PerceptionCollectSettings(BaseModel):
    """感知采集模块窗口参数。"""

    window_size: int = Field(default=4, description="时间窗口大小（秒）")
    max_windows: int = Field(default=3, description="最大待处理窗口数")
    settle_ms: int = Field(default=500, description="等待慢轨道的宽限期（毫秒）")
    full_action: str = Field(
        default="clear", description="窗口满载处理策略（drop/clear/keep）"
    )


class PerceptionSettings(BaseModel):
    """感知管线相关配置。"""

    log_ttl: int = Field(default=30, description="感知日志保留天数")
    event_ttl_days: int = Field(
        default=7,
        description=(
            "meaningful_events 表保留天数;_log_cleanup_loop 24h 周期按 created_at 删旧行."
            "产品决策:近 7 天数据足够回看,不保留更长元数据."
            "LRU 触发(磁盘满 5GB)时 clip 可能先于 row 被清,API 返 410,前端"
            "ClipPlayer/AudioClipPlayer onError 触发降级占位('🎬 已过期'/'🎤 音频已过期')."
        ),
    )
    snapshot_ttl_days: int = Field(
        default=7,
        description="事件 clip 按 mtime 清理的保留天数;跟 event_ttl_days 一致",
    )
    snapshot_max_disk_mb: int = Field(
        default=5000,
        description="截图磁盘配额上限(MB);超出按 mtime 升序 LRU 删",
    )
    snapshot_min_free_disk_mb: int = Field(
        default=500,
        description=(
            "写前预检阈值;可用空间低于此值跳过落盘(仅写 metadata,snapshot_count=0),"
            "避免磁盘满时 cv2.imwrite 抛 IOError 让 _persist task 整个崩"
        ),
    )
    snapshot_root: str | None = Field(
        default=None,
        description="截图根目录;null 时由 DirectorySettings.snapshot_dir 派生(workspace_dir / 'snapshots')",
    )
    collect: PerceptionCollectSettings = Field(
        default_factory=PerceptionCollectSettings,
        description="采集窗口策略",
    )
    engine: dict[str, Any] = Field(
        default_factory=dict,
        description="感知 engine 其它参数（input/identity 等，沿用 dataclass 消费）",
    )
    tier_u_dump_enable: bool = Field(
        default=False,
        description=(
            "POST /api/identity/pool/dump 调试端点开关（生产保持 false）。"
            "true 时端点允许把陌生人池快照（含 body crop 像素 + ReID embedding "
            "+ cluster 拓扑）落盘到 $MILOCO_HOME/snapshots/tier_u/。"
            "本地开发/调试可开,上线环境保持默认关。"
        ),
    )


class PerfRetentionSettings(BaseModel):
    """observability 数据保留天数。"""

    traces_days: int = Field(default=7, description="traces / traces_device 表保留天数")
    events_days: int = Field(default=7, description="events 表保留天数")
    agent_runs_days: int = Field(default=7, description="agent_runs 表保留天数")
    trace_jsonl_days: int = Field(default=7, description="agent jsonl.gz 文件保留天数")


class PerfSettings(BaseModel):
    """性能指标总开关与保留策略。"""

    enabled: bool = Field(
        default=True,
        description=(
            "性能指标采集总开关。关闭后 MetricsClient / AgentMetaPoller 不启动,"
            "observability.db / agent_runs / trace jsonl cleanup 全部跳过,"
            "track_agent_run 调用单点短路。"
        ),
    )
    retention: PerfRetentionSettings = Field(
        default_factory=PerfRetentionSettings,
        description="observability 数据保留参数",
    )


_DEFAULT_PERF_FIELD_LABELS: dict[str, str] = {
    "decode": "音视频解码",
    "collect": "数据采集",
    "convert": "格式转换",
    "gate": "门控检测",
    "identity": "身份识别",
    "omni": "多模态推理",
    "log": "日志保存",
    "in_delay": "输入延迟",
    "out_delay": "输出延迟",
    "cycle_total": "周期总耗时",
    "pipeline_total": "推理管线总耗时",
}


class DirectorySettings(BaseModel):
    """目录配置；派生路径均由 ``$MILOCO_HOME`` + ``storage`` 计算。"""

    static: str = Field(
        default="static", description="静态资源目录（相对后端源码 root）"
    )
    storage: str = Field(
        default=".",
        description="工作目录；相对路径将相对 $MILOCO_HOME 解析，绝对路径直接使用",
    )
    models: str = Field(
        default="",
        description="ONNX 模型目录；为空时默认 $MILOCO_HOME/models",
    )

    # ── 派生路径（均为 computed_field，不参与 schema / env 解析） ───────────
    @computed_field(description="后端静态资源目录绝对路径")  # type: ignore[misc]
    @property
    def static_dir(self) -> Path:
        base = Path(self.static)
        return base if base.is_absolute() else _BACKEND_ROOT / base

    @computed_field(description="工作目录绝对路径（$MILOCO_HOME/storage 或绝对路径）")  # type: ignore[misc]
    @property
    def workspace_dir(self) -> Path:
        base = Path(self.storage)
        if base.is_absolute():
            return base
        if str(base) in (".", ""):
            return _resolve_miloco_home()
        return _resolve_miloco_home() / base

    @computed_field(description="摄像头采集图像目录")  # type: ignore[misc]
    @property
    def image_dir(self) -> Path:
        return self.workspace_dir / "images"

    @computed_field(description="MIoT 本地缓存目录")  # type: ignore[misc]
    @property
    def miot_cache_dir(self) -> Path:
        return self.workspace_dir / "miot_cache"

    @computed_field(description="服务运行日志目录")  # type: ignore[misc]
    @property
    def log_dir(self) -> Path:
        return self.workspace_dir / "log"

    @computed_field(description="meaningful_events 截图目录")  # type: ignore[misc]
    @property
    def snapshot_dir(self) -> Path:
        return self.workspace_dir / "snapshots"

    @computed_field(description="ONNX 模型目录绝对路径")  # type: ignore[misc]
    @property
    def models_dir(self) -> Path:
        if self.models:
            p = Path(self.models)
            return p if p.is_absolute() else _resolve_miloco_home() / p
        return _resolve_miloco_home() / "models"


# ─── 配置源 ──────────────────────────────────────────────────────────────────


def _load_yaml_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must deserialize to a mapping, got {type(data)!r}")
    return data


def _load_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # noqa: BLE001
        logger.warning("config.json 解析失败 (%s)，将视为空配置：%s", path, exc)
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must deserialize to a mapping, got {type(data)!r}")
    return data


class YamlConfigSource(PydanticBaseSettingsSource):
    """读取 ``backend/miloco/src/miloco/config/settings.yaml``。"""

    def __init__(self, settings_cls: type[BaseSettings], path: Path) -> None:
        super().__init__(settings_cls)
        self._path = path

    def get_field_value(self, field, field_name):  # type: ignore[override]
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:  # type: ignore[override]
        return _load_yaml_dict(self._path)


class JsonConfigSource(PydanticBaseSettingsSource):
    """读取 ``$MILOCO_HOME/config.json``。"""

    def __init__(self, settings_cls: type[BaseSettings], path: Path) -> None:
        super().__init__(settings_cls)
        self._path = path

    def get_field_value(self, field, field_name):  # type: ignore[override]
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:  # type: ignore[override]
        return _load_json_dict(self._path)


# ─── 顶层 Settings ───────────────────────────────────────────────────────────


class MilocoSettings(BaseSettings):
    """miloco 后端 / CLI / 插件共用的统一配置模型。

    加载优先级（高 → 低）：环境变量 > ``config.json`` > ``settings.yaml`` > 代码默认值。
    """

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_prefix="MILOCO_",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    debug: bool = Field(
        default=False,
        description="是否启用调试模式：为 true 时 CLI / backend / openclaw 插件都会输出更详细的日志",
    )
    timezone: str | None = Field(
        default=None,
        description=(
            "部署时区 (IANA 名,如 Asia/Shanghai / America/Los_Angeles);"
            "null = 跟随系统 /etc/timezone。影响业务侧"
            "\"今天 / 本周 / rollover\"等部署概念,以及 API 出口 ISO 偏移后缀"
            "(如 +08:00);DB 存储始终 INTEGER ms (UTC 绝对时刻)。"
        ),
    )
    server: ServerSettings = Field(
        default_factory=ServerSettings,
        description="miloco 后端服务相关配置（HTTP 访问、token、启动用 Python 解释器）",
    )
    agent: AgentSettings = Field(
        default_factory=AgentSettings,
        description="agent webhook 出站调用配置（webhook 地址 + 鉴权凭据）",
    )
    dispatcher: DispatcherSettings = Field(
        default_factory=DispatcherSettings,
        description="agent 投递调度参数（队列上限 + turn 等待超时）",
    )
    model: ModelSettings = Field(
        default_factory=ModelSettings,
        description="miloco 使用的第三方多模态模型配置",
    )
    directories: DirectorySettings = Field(
        default_factory=DirectorySettings,
        description="目录配置与派生路径",
    )
    database: DatabaseSettings = Field(
        default_factory=DatabaseSettings,
        description="SQLite 数据库连接参数",
    )
    app: AppSettings = Field(
        default_factory=AppSettings,
        description="FastAPI 应用元信息",
    )
    miot: MiotSettings = Field(
        default_factory=MiotSettings,
        description="MIoT 云接入参数",
    )
    notify: NotifySettings = Field(
        default_factory=NotifySettings,
        description="通知发送运行参数（去重窗口等）",
    )
    camera: CameraSettings = Field(
        default_factory=CameraSettings,
        description="摄像头采集参数",
    )
    rule: RuleSettings = Field(
        default_factory=RuleSettings,
        description="规则引擎相关配置",
    )
    perception: PerceptionSettings = Field(
        default_factory=PerceptionSettings,
        description="感知管线相关配置",
    )
    perf: PerfSettings = Field(
        default_factory=PerfSettings,
        description="性能指标总开关与报告参数",
    )

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return None
        from zoneinfo import available_timezones

        if v not in available_timezones():
            raise ValueError(
                f"Invalid IANA timezone name: {v!r}. "
                "Use names like 'Asia/Shanghai', 'America/Los_Angeles', 'UTC'."
            )
        return v

    # ── 派生 / 便捷属性 ─────────────────────────────────────────────────

    @computed_field(description="数据库文件绝对路径（相对路径基于 storage 解析）")  # type: ignore[misc]
    @property
    def database_path(self) -> Path:
        base = Path(self.database.path)
        if base.is_absolute():
            return base
        return self.directories.workspace_dir / base

    # ── model.omni → perception.engine 下推 ───────────────────────────────

    @model_validator(mode="after")
    def _propagate_model_omni_to_perception(self) -> "MilocoSettings":
        """把用户配置的 ``model.omni.*`` 覆盖写入 ``perception.engine["omni"]``"""
        existing = self.perception.engine.get("omni")
        base: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
        merged = {
            **base,
            "model": self.model.omni.model,
            "base_url": self.model.omni.base_url,
            "api_key": self.model.omni.api_key,
        }
        if merged != existing:
            new_engine = {**self.perception.engine, "omni": merged}
            self.perception = self.perception.model_copy(update={"engine": new_engine})
        return self

    # ── server.url 与 server.host/port 一致性校验 ────────────────────────

    @model_validator(mode="after")
    def _validate_server_url_host_port(self) -> "MilocoSettings":
        """检查 server.url 与 server.host/port 的一致性。

        当 server.host 为 '0.0.0.0' 时，仅检查 port 是否一致；
        否则同时检查 host 和 port。
        """
        from urllib.parse import urlparse

        url = self.server.url
        try:
            parsed = urlparse(url)
            url_host = parsed.hostname or "localhost"
            url_port = parsed.port
        except ValueError:
            logger.warning("无法解析 server.url: %s，跳过一致性校验", url)
            return self

        if url_port is None:
            # 根据协议推断默认端口
            if parsed.scheme == "http":
                url_port = 80
            elif parsed.scheme == "https":
                url_port = 443
            # 对于其他协议，保持 None，后续跳过端口检查

        # 将 localhost 映射为 127.0.0.1
        if url_host == "localhost":
            url_host = "127.0.0.1"

        host = self.server.host
        if host == "localhost":
            host = "127.0.0.1"
        # host 为 0.0.0.0 时，后端监听所有接口，url 可以为任意 host，仅检查 port
        port_mismatch = url_port is not None and url_port != self.server.port
        host_mismatch = host != "0.0.0.0" and url_host != host
        if port_mismatch or host_mismatch:
            logger.warning(
                "server.url (%s) 与 server.host (%s) / server.port (%d) 配置不一致，"
                "CLI 使用 server.url 访问后端，后端监听 server.host:server.port，"
                "不一致将导致健康检查失败。",
                url,
                self.server.host,
                self.server.port,
            )
        return self

    # ── 配置源编排 ──────────────────────────────────────────────────────

    @classmethod
    def settings_customise_sources(  # type: ignore[override]
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # 优先级：init（测试覆盖）> env > config.json > settings.yaml > 默认值
        json_source = JsonConfigSource(settings_cls, _user_config_path())
        yaml_source = YamlConfigSource(settings_cls, _SETTINGS_YAML)
        return (
            init_settings,
            env_settings,
            json_source,
            yaml_source,
        )


# ─── 单例访问 ────────────────────────────────────────────────────────────────


@functools.lru_cache(maxsize=1)
def _cached_settings() -> MilocoSettings:
    return MilocoSettings()


def get_settings() -> MilocoSettings:
    """返回缓存的 ``MilocoSettings`` 单例。"""
    return _cached_settings()


# 其它模块若有派生缓存（例如 main.py 里 spa_handler 的 _resolved_static_root），
# 在 import 时通过 register_reset_hook(name, fn) 注册自己的 cache_clear ——
# reset_settings() 会一并触发，防止 settings 重新解析后派生缓存仍指向老路径。
# 用 dict + stable key 保证 module reload 场景下幂等（list contains 是身份比较，
# lru_cache_wrapper 每次 reload 是新实例，老 hook 永远 in 不掉，会累积）。
RESET_HOOKS: dict[str, Callable[[], None]] = {}


def register_reset_hook(name: str, fn: Callable[[], None]) -> None:
    """注册一个 reset_settings() 时一并触发的 cache_clear。

    `name` 是 stable key（推荐 `module.qualname` 形式），module reload 时同 key
    覆盖老 hook，避免 list 累积。
    """
    RESET_HOOKS[name] = fn


def reset_settings() -> None:
    """清空 ``get_settings()`` 的 LRU 缓存以及注册的派生缓存。

    主要供以下场景使用：

    - **测试**：``monkeypatch`` 修改 ``MILOCO_*`` 环境变量或 ``config.json``
      后，调用本函数强制下一次 ``get_settings()`` 重新解析；
    - **bootstrap**：``update_shared_config()`` 刚把新值写回 ``config.json``
      时，清缓存让后续 ``get_settings()`` 立刻看到落盘值。

    正常业务进程里这些快照只在启动时建立一次，不影响正确性；若未来有动态重载需求，需要改造这些快照位点。
    """
    _cached_settings.cache_clear()
    for name, hook in RESET_HOOKS.items():
        try:
            hook()
        except Exception as e:
            # 留 trace 让排查时能定位是哪个 hook 抛错(测试 monkeypatch /
            # lru_cache wrapper replace 等场景)。
            logger.warning("reset hook %s failed: %s", name, e)


__all__ = [
    "AppSettings",
    "CameraSettings",
    "DatabaseSettings",
    "DirectorySettings",
    "DispatcherSettings",
    "MilocoSettings",
    "MiotSettings",
    "ModelSettings",
    "NotifySettings",
    "OmniModelSettings",
    "AgentSettings",
    "PerceptionCollectSettings",
    "PerceptionSettings",
    "PerfRetentionSettings",
    "PerfSettings",
    "RuleSettings",
    "ServerSettings",
    "get_settings",
    "reset_settings",
]
