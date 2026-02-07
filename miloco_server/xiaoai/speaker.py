# Copyright (C) 2025 willianfu
# 小爱音箱集成模块 - Miloco Server
#
# 音箱控制模块，用于TTS和播放控制

"""
小爱音箱控制模块

提供音箱控制功能：
- 文本转语音（TTS）播放
- 音频URL播放
- 播放状态管理
- 唤醒/休眠控制
- 在音箱上执行Shell命令
"""

import json
import asyncio
import logging
from typing import Optional, List, TYPE_CHECKING

from miloco_server.xiaoai.protocol import CommandResult, PlayingStatus
from miloco_server.xiaoai.config import XiaoAIConfig

if TYPE_CHECKING:
    from miloco_server.xiaoai.websocket_server import XiaoAIWebSocketServer

logger = logging.getLogger(__name__)


class SpeakerController:
    """
    Controller for a specific XiaoAI speaker.
    
    Each connected speaker has its own controller instance for
    independent control operations.
    """
    
    def __init__(
        self,
        speaker_id: str,
        server: "XiaoAIWebSocketServer",
        config: XiaoAIConfig
    ):
        """
        Initialize SpeakerController.
        
        Args:
            speaker_id: Unique identifier for the speaker
            server: WebSocket server instance
            config: XiaoAI configuration
        """
        self._speaker_id = speaker_id
        self._server = server
        self._config = config
    
    @property
    def speaker_id(self) -> str:
        return self._speaker_id
    
    @property
    def is_connected(self) -> bool:
        return self._server.is_speaker_connected(self._speaker_id)
    
    @property
    def status(self) -> PlayingStatus:
        speaker = self._server.get_speaker(self._speaker_id)
        return speaker.status if speaker else PlayingStatus.IDLE
    
    def _sanitize_text(self, text: str) -> str:
        """Sanitize text for TTS."""
        return text.replace('\r\n', ' ').replace('\n', ' ')
    
    def _wrap_shell_arg(self, text: str) -> str:
        """Safely wrap text for single-quoted shell argument."""
        escaped = text.replace("'", "'\\''")
        return f"'{escaped}'"
    
    def _split_text(self, text: str, max_len: int = 120) -> List[str]:
        """Split long text into chunks for TTS."""
        cleaned = self._sanitize_text(text).strip()
        if not cleaned:
            return []
        
        parts = []
        sentences = []
        current = ""
        
        for char in cleaned:
            current += char
            if char in '。！？!?；;':
                sentences.append(current.strip())
                current = ""
        
        if current.strip():
            sentences.append(current.strip())
        
        for sentence in sentences:
            if not sentence:
                continue
            if len(sentence) <= max_len:
                parts.append(sentence)
            else:
                for i in range(0, len(sentence), max_len):
                    parts.append(sentence[i:i + max_len])
        
        return parts
    
    async def _delay(self, ms: int):
        """Delay for specified milliseconds."""
        await asyncio.sleep(ms / 1000)
    
    async def _wait_for_playback_end(self, timeout_ms: int = 60000) -> bool:
        """等待播放结束
        
        通过轮询 mphelper mute_stat 检测播放状态变化。
        当检测到 PLAYING -> 非PLAYING 的转换时返回。
        如果超过 max_idle_checks 次都没检测到播放，提前返回（避免空等）。
        """
        start = asyncio.get_event_loop().time() * 1000
        seen_playing = False
        idle_checks = 0
        max_idle_checks = 30  # 如果3秒内都没检测到播放，提前返回
        
        while (asyncio.get_event_loop().time() * 1000 - start) < timeout_ms:
            try:
                status = await self.get_playing(sync=True)
                if status == PlayingStatus.PLAYING:
                    seen_playing = True
                    idle_checks = 0
                elif seen_playing:
                    return True
                else:
                    idle_checks += 1
                    if idle_checks >= max_idle_checks:
                        logger.debug("[%s] 等待播放超过%d次未检测到播放，提前返回", 
                                    self._speaker_id, max_idle_checks)
                        return True  # 假设已经播放完成
            except Exception as e:
                logger.warning("[%s] 检测播放状态异常: %s", self._speaker_id, e)
                idle_checks += 1
                if idle_checks >= max_idle_checks:
                    return True
            await self._delay(100)
        
        logger.warning("[%s] 等待播放结束超时", self._speaker_id)
        return False
    
    async def run_shell(
        self,
        script: str,
        timeout: int = 10000
    ) -> Optional[CommandResult]:
        """在音箱上执行Shell脚本
        
        注意：open-xiaoai客户端的run_shell命令期望payload为纯字符串（脚本内容），
        而非对象。客户端使用 serde_json::from_value::<String>(payload) 解析。
        """
        try:
            result = await self._server.call_rpc(
                self._speaker_id,
                "run_shell",
                script,  # 直接传脚本字符串，不是对象
                timeout_ms=timeout + 5000
            )
            
            if result and result.data:
                if isinstance(result.data, str):
                    return CommandResult.from_json(result.data)
                elif isinstance(result.data, dict):
                    return CommandResult.from_dict(result.data)
            return None
        except Exception as e:
            logger.error("[%s] 执行Shell命令失败: %s", self._speaker_id, e)
            return None
    
    async def get_playing(self, sync: bool = False) -> PlayingStatus:
        """Get playback status."""
        if sync:
            result = await self.run_shell("mphelper mute_stat")
            if result:
                speaker = self._server.get_speaker(self._speaker_id)
                if speaker:
                    if "1" in result.stdout:
                        speaker.status = PlayingStatus.PLAYING
                    elif "2" in result.stdout:
                        speaker.status = PlayingStatus.PAUSED
                    else:
                        speaker.status = PlayingStatus.IDLE
        
        return self.status
    
    async def set_playing(self, playing: bool = True) -> bool:
        """Set playback state."""
        command = "mphelper play" if playing else "mphelper pause"
        result = await self.run_shell(command)
        return result is not None and '"code": 0' in result.stdout
    
    def _is_tts_success(self, result: Optional[CommandResult]) -> bool:
        """检查TTS命令是否成功"""
        if not result:
            return False
        # 检查标准成功响应
        if '"code": 0' in result.stdout or '"code":0' in result.stdout:
            return True
        # 检查exit code
        if result.exit_code == 0 and not result.stderr:
            return True
        return False

    async def play(
        self,
        text: Optional[str] = None,
        url: Optional[str] = None,
        timeout: int = 600000,
        blocking: bool = False
    ) -> bool:
        """播放文本（TTS）或音频URL"""
        logger.info("[%s] 播放请求: text=%s, url=%s, blocking=%s", 
                   self._speaker_id, text[:50] if text else None, url, blocking)
        
        if blocking:
            if url:
                url_json = json.dumps({"url": url, "type": 1}, ensure_ascii=False)
                payload = self._wrap_shell_arg(url_json)
                command = f"ubus call mediaplayer player_play_url {payload}"
                result = await self.run_shell(command, timeout=timeout)
                
                if not self._is_tts_success(result):
                    logger.error("[%s] URL播放失败: %s", self._speaker_id, 
                               result.stdout if result else "无结果")
                    return False
                
                await self._wait_for_playback_end(timeout)
                return True
            
            chunks = self._split_text(text or "你好", self._config.tts_max_length)
            
            for chunk in (chunks if chunks else ["你好"]):
                success = await self._play_tts_chunk(chunk, timeout)
                if not success:
                    logger.error("[%s] TTS块播放失败: %s", self._speaker_id, chunk[:30])
                    return False
                
                await self._wait_for_playback_end(timeout)
            
            return True
        
        # 非阻塞模式
        if url:
            url_json = json.dumps({"url": url, "type": 1}, ensure_ascii=False)
            payload = self._wrap_shell_arg(url_json)
            command = f"ubus call mediaplayer player_play_url {payload}"
            result = await self.run_shell(command, timeout=timeout)
            return self._is_tts_success(result)
        
        return await self._play_tts_chunk(text or "你好", timeout)
    
    async def _play_tts_chunk(self, text: str, timeout: int) -> bool:
        """播放单个TTS文本块，失败时尝试备用方法
        
        播放策略（与open-xiaoai保持一致）:
        1. 先尝试 ubus call mibrain text_to_speech（小爱内置TTS）
        2. 失败则尝试 /usr/sbin/tts_play.sh（兜底方案）
        """
        # 方法1: 尝试 mibrain text_to_speech
        # 注意: ensure_ascii=False 保留中文字符，避免unicode编码问题
        tts_json = json.dumps({"text": text, "save": 0}, ensure_ascii=False)
        payload = self._wrap_shell_arg(tts_json)
        command = f"ubus call mibrain text_to_speech {payload}"
        result = await self.run_shell(command, timeout=timeout)
        
        logger.info("[%s] mibrain TTS结果: exit=%s, stdout=%s", 
                   self._speaker_id,
                   result.exit_code if result else None,
                   result.stdout[:100] if result and result.stdout else None)
        
        if self._is_tts_success(result):
            return True
        
        # 方法2: 尝试 tts_play.sh（与open-xiaoai一致的兜底方案）
        logger.info("[%s] mibrain TTS失败，尝试 tts_play.sh", self._speaker_id)
        safe_text = self._wrap_shell_arg(text)
        fallback = f"/usr/sbin/tts_play.sh {safe_text}"
        fallback_result = await self.run_shell(fallback, timeout=timeout)
        
        logger.info("[%s] tts_play.sh结果: exit=%s, stdout=%s", 
                   self._speaker_id,
                   fallback_result.exit_code if fallback_result else None,
                   fallback_result.stdout[:100] if fallback_result and fallback_result.stdout else None)
        
        if fallback_result and fallback_result.exit_code == 0:
            return True
        
        logger.error("[%s] 所有TTS方法都失败了, text=%s", self._speaker_id, text[:30])
        return False
    
    async def wake_up(self, awake: bool = True, silent: bool = True) -> bool:
        """Wake or unwake the speaker."""
        if awake:
            src = 1 if silent else 0
            command = f"ubus call pnshelper event_notify '{{\"src\":{src},\"event\":0}}'"
        else:
            command = """
                ubus call pnshelper event_notify '{"src":3, "event":7}'
                sleep 0.1
                ubus call pnshelper event_notify '{"src":3, "event":8}'
            """
        
        result = await self.run_shell(command)
        return result is not None and '"code": 0' in result.stdout
    
    async def ask_xiaoai(self, text: str, silent: bool = False) -> bool:
        """将文字指令交给原来的小爱执行"""
        payload = {"nlp": 1, "nlp_text": text}
        if not silent:
            payload["tts"] = 1
        
        command = f"ubus call mibrain ai_service '{json.dumps(payload, ensure_ascii=False)}'"
        result = await self.run_shell(command)
        return result is not None and '"code": 0' in result.stdout
    
    async def abort_xiaoai(self) -> bool:
        """Abort/restart the original XiaoAI service."""
        result = await self.run_shell(
            "/etc/init.d/mico_aivs_lab restart >/dev/null 2>&1"
        )
        return result is not None and result.exit_code == 0
    
    async def get_device(self) -> dict:
        """Get device model and serial number."""
        result = await self.run_shell("echo $(micocfg_model) $(micocfg_sn)")
        if result:
            info = result.stdout.strip().split()
            return {
                "model": info[0] if len(info) > 0 else "unknown",
                "sn": info[1] if len(info) > 1 else "unknown"
            }
        return {"model": "unknown", "sn": "unknown"}
    
    async def get_mic(self) -> str:
        """Get microphone status."""
        result = await self.run_shell(
            "[ ! -f /tmp/mipns/mute ] && echo on || echo off"
        )
        if result and "on" in result.stdout:
            return "on"
        return "off"
    
    async def set_mic(self, on: bool = True) -> bool:
        """Turn microphone on or off."""
        event = 7 if on else 8
        command = f"ubus -t1 -S call pnshelper event_notify '{{\"src\":3, \"event\":{event}}}' 2>&1"
        result = await self.run_shell(command)
        return result is not None and '"code":0' in result.stdout


class SpeakerManager:
    """
    Manager for all connected speakers.
    
    Provides access to individual speaker controllers.
    """
    
    def __init__(self, server: "XiaoAIWebSocketServer", config: XiaoAIConfig):
        """
        Initialize SpeakerManager.
        
        Args:
            server: WebSocket server instance
            config: XiaoAI configuration
        """
        self._server = server
        self._config = config
        self._controllers: dict[str, SpeakerController] = {}
    
    def get_controller(self, speaker_id: str) -> Optional[SpeakerController]:
        """Get or create controller for a speaker."""
        if speaker_id not in self._controllers:
            if self._server.is_speaker_connected(speaker_id):
                self._controllers[speaker_id] = SpeakerController(
                    speaker_id, self._server, self._config
                )
        return self._controllers.get(speaker_id)
    
    def remove_controller(self, speaker_id: str):
        """Remove controller for disconnected speaker."""
        self._controllers.pop(speaker_id, None)
    
    @property
    def connected_speaker_ids(self) -> List[str]:
        """Get list of connected speaker IDs."""
        return list(self._server.connected_speakers.keys())
    
    def get_connected_speakers_info(self) -> List[dict]:
        """Get info about all connected speakers."""
        result = []
        for speaker_id, conn in self._server.connected_speakers.items():
            result.append({
                "speaker_id": speaker_id,
                "model": conn.model,
                "status": conn.status.value,
                "connected_at": conn.connected_at,
                "remote_address": conn.remote_address
            })
        return result
