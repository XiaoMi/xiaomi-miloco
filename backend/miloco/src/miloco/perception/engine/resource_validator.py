"""Perception Engine — resource availability validation."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class EngineReadiness(Enum):
    READY = "ready"
    NOT_CONFIGURED = "not_configured"
    MODELS_MISSING = "models_missing"


@dataclass(frozen=True)
class ValidationResult:
    status: EngineReadiness
    missing_models: list[str] = field(default_factory=list)
    missing_optional_models: list[str] = field(default_factory=list)
    message: str = ""


@dataclass(frozen=True)
class ModelSpec:
    name: str
    description: str
    optional: bool = False


MODELS: tuple[ModelSpec, ...] = (
    ModelSpec("det_4C.onnx", "目标检测"),
    ModelSpec("human_body_reid_v2.onnx", "人体重识别 (v2)"),
    # 事件去重句向量（bge-small-zh）：缺失时去重降级为精确文本匹配，不阻塞引擎。
    # 需 onnx + tokenizer.json 两个文件配套，缺任一 EventEmbedder 即初始化失败。
    ModelSpec("bge-small-zh-v1.5-int8.onnx", "bge-small-zh", optional=True),
    ModelSpec("bge-small-zh-v1.5-tokenizer.json", "bge tokenizer", optional=True),
    # speeches 字段的 VAD 门控；缺失时门控停用（退回能量 gate 行为），不阻塞引擎。
    ModelSpec("silero_vad.onnx", "语音活动检测 (silero VAD)", optional=True),
)


def validate_resources(
    omni_api_key: str,
    models_dir: str | None,
    rule_only: bool = False,
) -> ValidationResult:
    """校验感知引擎所需资源，返回校验结果（不抛异常）。

    rule_only（纯场景触发）：只依赖 omni API Key——不跑 tracker/身份链路，
    det_4C / human_body_reid 等端侧模型一律不要求（缺模型也能启动场景联动）。
    """
    if not omni_api_key:
        return ValidationResult(
            status=EngineReadiness.NOT_CONFIGURED,
            message="Omni API Key 未配置",
        )

    if rule_only:
        # 纯场景触发：无 tracker/身份推理，模型目录与端侧模型都不需要。
        return ValidationResult(status=EngineReadiness.READY)

    if not models_dir:
        return ValidationResult(
            status=EngineReadiness.MODELS_MISSING,
            message="模型目录未配置",
        )

    models_path = Path(models_dir)
    if not models_path.is_dir():
        try:
            models_path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return ValidationResult(
                status=EngineReadiness.MODELS_MISSING,
                message=f"模型目录创建失败: {e}",
            )

    missing: list[str] = []
    missing_optional: list[str] = []
    for m in MODELS:
        file_path = models_path / m.name
        if not file_path.is_file():
            if m.optional:
                missing_optional.append(m.name)
            else:
                missing.append(m.name)

    if missing_optional:
        logger.warning("可选模型文件缺失（不影响引擎启动）: %s", missing_optional)

    if missing:
        return ValidationResult(
            status=EngineReadiness.MODELS_MISSING,
            missing_models=missing,
            missing_optional_models=missing_optional,
            message=f"{len(missing)} 个模型文件缺失",
        )

    return ValidationResult(
        status=EngineReadiness.READY,
        missing_optional_models=missing_optional,
    )
