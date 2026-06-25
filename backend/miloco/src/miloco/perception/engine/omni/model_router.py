"""Capability-based model routing for Omni calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from miloco.config.settings import ModelCapability, OmniModelSettings
from miloco.perception.engine.config import OmniConfig

VisualRouteMode = Literal["video", "frames", "audio"]


@dataclass(frozen=True)
class VisualRoute:
    """Selected models for one perception window."""

    visual_mode: VisualRouteMode
    primary: OmniModelSettings | None
    audio: OmniModelSettings | None = None


def enabled_profiles(profiles: list[OmniModelSettings]) -> list[OmniModelSettings]:
    """Return profiles that are enabled and have a configured model identifier."""

    return [p for p in profiles if p.enabled and p.model.strip()]


def select_model_for(
    profiles: list[OmniModelSettings],
    capability: ModelCapability,
) -> OmniModelSettings | None:
    """Pick the first enabled profile that advertises ``capability``."""

    for profile in enabled_profiles(profiles):
        if capability in profile.capabilities:
            return profile
    return None


def select_visual_route(profiles: list[OmniModelSettings]) -> VisualRoute:
    """Prefer a video-capable model, otherwise split image and audio models."""

    video = select_model_for(profiles, "video")
    if video is not None:
        return VisualRoute(visual_mode="video", primary=video)

    image = select_model_for(profiles, "image")
    if image is not None:
        return VisualRoute(
            visual_mode="frames",
            primary=image,
            audio=select_model_for(profiles, "audio"),
        )

    audio = select_model_for(profiles, "audio")
    if audio is not None:
        return VisualRoute(visual_mode="audio", primary=audio)

    return VisualRoute(visual_mode="frames", primary=None)


def profile_to_omni_config(base: OmniConfig, profile: OmniModelSettings) -> OmniConfig:
    """Apply user-facing profile fields to a runtime ``OmniConfig`` snapshot."""

    from dataclasses import replace

    return replace(
        base,
        model=profile.model,
        base_url=profile.base_url,
        api_key=profile.api_key or base.api_key,
    )


def get_live_profiles() -> list[OmniModelSettings]:
    """Read current settings and return profiles used for capability routing.

    If the user has not saved any profile yet, the legacy single active omni
    config remains the default all-capability model.
    """

    from miloco.config import get_settings

    model_settings = get_settings().model
    return list(model_settings.omni_profiles) or [model_settings.omni]
