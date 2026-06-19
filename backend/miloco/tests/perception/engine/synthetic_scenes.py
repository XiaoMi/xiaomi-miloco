"""合成测试数据生成器。

生成模拟的视频帧序列和音频数据，覆盖 MVP 各层测试场景，无需真实视频。
"""

from __future__ import annotations

import numpy as np
from miloco.perception.engine.input.video_splitter import create_input_slice
from miloco.perception.engine.types import InputSlice
from numpy.typing import NDArray

# =============================================================================
# 帧生成
# =============================================================================


def empty_room(w: int = 640, h: int = 480) -> NDArray[np.uint8]:
    """空房间：纯灰色背景。"""
    frame = np.full((h, w, 3), 180, dtype=np.uint8)
    # 桌子（棕色矩形）
    frame[300:380, 200:440] = [60, 100, 140]
    return frame


def person_sitting(w: int = 640, h: int = 480, x_offset: int = 280) -> NDArray[np.uint8]:
    """有人坐在桌前：背景 + 桌子 + 人形色块（上半身）。"""
    frame = empty_room(w, h)
    # 头部（肤色圆形区域用矩形近似）
    frame[120:180, x_offset : x_offset + 60] = [130, 170, 210]
    # 身体
    frame[180:300, x_offset - 20 : x_offset + 80] = [140, 80, 50]
    return frame


def person_sitting_with_book(w: int = 640, h: int = 480) -> NDArray[np.uint8]:
    """有人坐在桌前看书：人 + 桌上有白色书本矩形。"""
    frame = person_sitting(w, h)
    # 书本（白色矩形在桌面上）
    frame[305:340, 320:400] = [240, 240, 240]
    return frame


def person_sitting_with_phone(w: int = 640, h: int = 480) -> NDArray[np.uint8]:
    """有人坐在桌前看手机：人 + 手持小亮块。"""
    frame = person_sitting(w, h)
    # 手机（小亮块在手部位置）
    frame[240:270, 310:330] = [200, 200, 230]
    return frame


def person_walking(w: int = 640, h: int = 480, step: int = 0) -> NDArray[np.uint8]:
    """人在走动：每帧 x_offset 不同。"""
    x_offset = 100 + step * 60
    x_offset = min(x_offset, w - 100)
    frame = empty_room(w, h)
    # 全身人形
    frame[100:170, x_offset : x_offset + 50] = [130, 170, 210]  # 头
    frame[170:350, x_offset - 15 : x_offset + 65] = [140, 80, 50]  # 身体
    frame[350:450, x_offset - 10 : x_offset + 60] = [80, 60, 40]  # 腿
    return frame


def pet_moving(w: int = 640, h: int = 480, step: int = 0) -> NDArray[np.uint8]:
    """宠物在移动：小色块在地面移动。"""
    x_offset = 80 + step * 40
    frame = empty_room(w, h)
    # 宠物（橙色小块）
    frame[380:420, x_offset : x_offset + 60] = [50, 120, 220]
    return frame


def pet_stationary(w: int = 640, h: int = 480) -> NDArray[np.uint8]:
    """宠物静坐不动：固定位置橙色小块。"""
    frame = empty_room(w, h)
    # 宠物（橙色小块，静止在地面）
    frame[380:420, 200:260] = [50, 120, 220]
    return frame


def pet_and_person(w: int = 640, h: int = 480) -> NDArray[np.uint8]:
    """人宠同框：人坐在左侧，宠物在右侧地面。"""
    frame = person_sitting(w, h, x_offset=180)
    # 宠物（橙色小块在右侧）
    frame[390:425, 420:490] = [50, 120, 220]
    return frame


def two_pets(w: int = 640, h: int = 480) -> NDArray[np.uint8]:
    """两只宠物：左侧橙色（猫），右侧黄绿色（狗）。"""
    frame = empty_room(w, h)
    # 猫（橙色小块）
    frame[380:415, 100:160] = [50, 120, 220]
    # 狗（黄绿色，略大）
    frame[370:420, 380:460] = [60, 180, 180]
    return frame


def two_people(w: int = 640, h: int = 480) -> NDArray[np.uint8]:
    """两人在房间里。"""
    frame = empty_room(w, h)
    # 人 1（左侧坐着）
    frame[120:180, 180:240] = [130, 170, 210]
    frame[180:300, 160:260] = [140, 80, 50]
    # 人 2（右侧站着）
    frame[80:150, 420:470] = [120, 160, 200]
    frame[150:340, 400:490] = [50, 70, 130]
    return frame


def light_change(w: int = 640, h: int = 480, brightness: int = 180) -> NDArray[np.uint8]:
    """光线变化：整体亮度微调。"""
    frame = empty_room(w, h)
    delta = brightness - 180
    frame = np.clip(frame.astype(np.int16) + delta, 0, 255).astype(np.uint8)
    return frame


# =============================================================================
# 音频生成
# =============================================================================


def silent_audio(duration_sec: float = 3.0, sample_rate: int = 16000) -> NDArray[np.int16]:
    """静音。"""
    return np.zeros(int(duration_sec * sample_rate), dtype=np.int16)


def speech_audio(duration_sec: float = 3.0, sample_rate: int = 16000) -> NDArray[np.int16]:
    """模拟语音信号：中等频率正弦波 + 调制（ZCR 在语音范围内）。"""
    n = int(duration_sec * sample_rate)
    t = np.arange(n, dtype=np.float64) / sample_rate
    # 300Hz 基频 + 幅度调制模拟说话节奏
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)  # 3Hz 调制
    signal = envelope * np.sin(2 * np.pi * 300 * t) * 5000
    return signal.astype(np.int16)


def loud_noise(duration_sec: float = 3.0, sample_rate: int = 16000) -> NDArray[np.int16]:
    """突然大声响（模拟拍桌子/关门）。"""
    n = int(duration_sec * sample_rate)
    audio = np.zeros(n, dtype=np.float64)
    # 在中间位置放一个冲击信号
    impact_start = n // 2
    impact_len = int(0.1 * sample_rate)  # 100ms
    audio[impact_start : impact_start + impact_len] = np.random.default_rng(42).normal(0, 15000, impact_len)
    return audio.astype(np.int16)


def alarm_audio(duration_sec: float = 3.0, sample_rate: int = 16000) -> NDArray[np.int16]:
    """模拟报警声：3.5kHz 周期性高能信号。"""
    n = int(duration_sec * sample_rate)
    t = np.arange(n, dtype=np.float64) / sample_rate
    signal = np.sin(2 * np.pi * 3500 * t) * 20000
    # 周期性开关（模拟报警器 beep-beep）
    on_off = (np.sin(2 * np.pi * 2 * t) > 0).astype(np.float64)
    return (signal * on_off).astype(np.int16)


# =============================================================================
# 场景组合 → InputSlice
# =============================================================================


def scene_empty_room() -> InputSlice:
    """场景1：空房间，完全静止+静音。Gate 应跳过。"""
    frame = empty_room()
    return create_input_slice("study-room", [frame] * 6, silent_audio())


def scene_person_enters() -> InputSlice:
    """场景2：空房间 → 有人走入。Gate 应触发。"""
    frames = [
        empty_room(),
        empty_room(),
        person_walking(step=0),
        person_walking(step=1),
        person_walking(step=2),
        person_walking(step=3),
    ]
    return create_input_slice("study-room", frames, silent_audio())


def scene_person_sitting_still() -> InputSlice:
    """场景5：有人坐着不动。Gate 触发（有人），Edge 判断 static。"""
    frame = person_sitting()
    return create_input_slice("study-room", [frame] * 6, silent_audio())


def scene_person_walking() -> InputSlice:
    """场景4：有人走动。Edge 判断 dynamic。"""
    frames = [person_walking(step=i) for i in range(6)]
    return create_input_slice("study-room", frames, silent_audio())


def scene_person_reading() -> InputSlice:
    """场景11：有人坐在桌前看书。核心用例——规则"学习时开灯"应命中。"""
    frame = person_sitting_with_book()
    return create_input_slice("study-room", [frame] * 6, silent_audio())


def scene_person_phone() -> InputSlice:
    """场景12：有人坐在桌前玩手机。规则"学习时开灯"不应命中。"""
    frame = person_sitting_with_phone()
    return create_input_slice("study-room", [frame] * 6, silent_audio())


def scene_person_speaking() -> InputSlice:
    """场景3/15：有人坐着说话。Gate 音频触发，Omni 应检测 speech。"""
    frame = person_sitting()
    return create_input_slice("study-room", [frame] * 6, speech_audio())


def scene_audio_only_noise() -> InputSlice:
    """场景7：画面不变，突然有声响。仅音频触发。"""
    frame = empty_room()
    return create_input_slice("study-room", [frame] * 6, loud_noise())


def scene_light_change() -> InputSlice:
    """场景6：光线缓慢变化。测试 Gate 阈值——微小变化应不触发。"""
    frames = [light_change(brightness=180 + i) for i in range(6)]
    return create_input_slice("study-room", frames, silent_audio())


def scene_two_people_chatting() -> InputSlice:
    """场景8/16：两人在房间聊天。多目标 + ambient speech。"""
    frame = two_people()
    return create_input_slice("study-room", [frame] * 6, speech_audio())


def scene_pet_moving() -> InputSlice:
    """场景10：宠物在移动。Edge 应检测 pet + dynamic。"""
    frames = [pet_moving(step=i) for i in range(6)]
    return create_input_slice("study-room", frames, silent_audio())


def scene_pet_stationary() -> InputSlice:
    """场景15：宠物静坐不动。Gate 层应判为 static（无显著变化）。"""
    frame = pet_stationary()
    return create_input_slice("study-room", [frame] * 6, silent_audio())


def scene_pet_and_person() -> InputSlice:
    """场景16：人宠同框。验证多类型目标同时存在。"""
    frame = pet_and_person()
    return create_input_slice("study-room", [frame] * 6, silent_audio())


def scene_two_pets() -> InputSlice:
    """场景17：两只宠物同时在场。验证多宠物检测。"""
    frame = two_pets()
    return create_input_slice("study-room", [frame] * 6, silent_audio())


def scene_person_leaving() -> InputSlice:
    """场景14：有人坐着 → 站起离开。Omni 应描述 delta。"""
    frames = [
        person_sitting(),
        person_sitting(),
        person_sitting(),
        person_walking(step=3),
        person_walking(step=4),
        empty_room(),
    ]
    return create_input_slice("study-room", frames, silent_audio())


# =============================================================================
# 全部场景清单
# =============================================================================

ALL_SCENES = {
    "01_empty_room": scene_empty_room,
    "02_person_enters": scene_person_enters,
    "03_person_speaking": scene_person_speaking,
    "04_person_walking": scene_person_walking,
    "05_person_sitting_still": scene_person_sitting_still,
    "06_light_change": scene_light_change,
    "07_audio_only_noise": scene_audio_only_noise,
    "08_two_people_chatting": scene_two_people_chatting,
    "10_pet_moving": scene_pet_moving,
    "11_person_reading": scene_person_reading,
    "12_person_phone": scene_person_phone,
    "14_person_leaving": scene_person_leaving,
    "15_pet_stationary": scene_pet_stationary,
    "16_pet_and_person": scene_pet_and_person,
    "17_two_pets": scene_two_pets,
}
