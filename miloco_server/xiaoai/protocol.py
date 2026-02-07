# Copyright (C) 2025 willianfu
# XiaoAI Speaker Integration Module for Miloco Server
#
# Protocol data structures for WebSocket communication with XiaoAI speaker client.

"""
Protocol module for XiaoAI WebSocket communication.

Defines the data structures used for communication between the server
and the XiaoAI speaker client (open-xiaoai client-rust).

Message Types:
- Request: RPC request from client to server
- Response: RPC response from server to client  
- Event: Event notifications (playing status, speech recognition, etc.)
- Stream: Binary data streams (audio recording, etc.)
"""

import json
import uuid
import logging
from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import Optional, Any, Union

logger = logging.getLogger(__name__)


class MessageType(str, Enum):
    """WebSocket message types."""
    REQUEST = "Request"
    RESPONSE = "Response"
    EVENT = "Event"
    STREAM = "Stream"


class PlayingStatus(str, Enum):
    """Speaker playback status."""
    PLAYING = "playing"
    PAUSED = "paused"
    IDLE = "idle"
    
    @classmethod
    def from_event_data(cls, data: str) -> "PlayingStatus":
        """Convert event data to PlayingStatus."""
        if data == "Playing":
            return cls.PLAYING
        elif data == "Paused":
            return cls.PAUSED
        return cls.IDLE


class EventType(str, Enum):
    """Event types from speaker client."""
    PLAYING = "playing"  # Playback status change
    INSTRUCTION = "instruction"  # Speech recognition result
    KWS = "kws"  # Keyword wake detection


@dataclass
class Request:
    """RPC request message."""
    id: str
    command: str
    payload: Optional[Any] = None
    
    @classmethod
    def from_dict(cls, data: dict) -> "Request":
        return cls(
            id=data.get("id", ""),
            command=data.get("command", ""),
            payload=data.get("payload")
        )
    
    def to_dict(self) -> dict:
        result = {"id": self.id, "command": self.command}
        if self.payload is not None:
            result["payload"] = self.payload
        return result


@dataclass
class Response:
    """RPC response message."""
    id: str
    code: Optional[int] = None
    msg: Optional[str] = None
    data: Optional[Any] = None
    
    @classmethod
    def success(cls, request_id: str = "0", data: Any = None) -> "Response":
        return cls(id=request_id, code=0, msg="success", data=data)
    
    @classmethod
    def error(cls, request_id: str, message: str) -> "Response":
        return cls(id=request_id, code=-1, msg=message)
    
    @classmethod
    def from_data(cls, data: Any) -> "Response":
        return cls(id="0", data=data)
    
    @classmethod
    def from_dict(cls, data: dict) -> "Response":
        return cls(
            id=data.get("id", "0"),
            code=data.get("code"),
            msg=data.get("msg"),
            data=data.get("data")
        )
    
    def to_dict(self) -> dict:
        result = {"id": self.id}
        if self.code is not None:
            result["code"] = self.code
        if self.msg is not None:
            result["msg"] = self.msg
        if self.data is not None:
            result["data"] = self.data
        return result


@dataclass
class Event:
    """Event message from speaker client."""
    id: str
    event: str
    data: Optional[Any] = None
    
    @classmethod
    def create(cls, event: str, data: Any = None) -> "Event":
        return cls(id=str(uuid.uuid4()), event=event, data=data)
    
    @classmethod
    def from_dict(cls, data: dict) -> "Event":
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            event=data.get("event", ""),
            data=data.get("data")
        )
    
    def to_dict(self) -> dict:
        result = {"id": self.id, "event": self.event}
        if self.data is not None:
            result["data"] = self.data
        return result


@dataclass
class Stream:
    """Binary stream message."""
    id: str
    tag: str
    bytes: bytes
    data: Optional[Any] = None
    
    @classmethod
    def create(cls, tag: str, bytes_data: bytes, data: Any = None) -> "Stream":
        return cls(id=str(uuid.uuid4()), tag=tag, bytes=bytes_data, data=data)
    
    @classmethod
    def from_bytes(cls, raw_bytes: bytes) -> "Stream":
        """Parse stream from binary data (JSON with bytes field)."""
        data = json.loads(raw_bytes.decode('utf-8'))
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            tag=data.get("tag", ""),
            bytes=bytes(data.get("bytes", [])),
            data=data.get("data")
        )
    
    def to_bytes(self) -> bytes:
        """Serialize stream to binary data."""
        data = {
            "id": self.id,
            "tag": self.tag,
            "bytes": list(self.bytes)
        }
        if self.data is not None:
            data["data"] = self.data
        return json.dumps(data).encode('utf-8')


@dataclass
class AppMessage:
    """Wrapper for all message types."""
    type: MessageType
    content: Union[Request, Response, Event, Stream]
    
    @classmethod
    def from_json(cls, json_str: str) -> Optional["AppMessage"]:
        """Parse AppMessage from JSON string."""
        try:
            data = json.loads(json_str)
            
            if "Request" in data:
                return cls(
                    type=MessageType.REQUEST,
                    content=Request.from_dict(data["Request"])
                )
            elif "Response" in data:
                return cls(
                    type=MessageType.RESPONSE,
                    content=Response.from_dict(data["Response"])
                )
            elif "Event" in data:
                return cls(
                    type=MessageType.EVENT,
                    content=Event.from_dict(data["Event"])
                )
            elif "Stream" in data:
                return cls(
                    type=MessageType.STREAM,
                    content=Stream.from_bytes(json.dumps(data["Stream"]).encode())
                )
            
            logger.warning("Unknown message format: %s", json_str[:100])
            return None
            
        except json.JSONDecodeError as e:
            logger.error("Failed to parse message JSON: %s", e)
            return None
    
    def to_json(self) -> str:
        """序列化AppMessage为JSON字符串
        
        注意：使用ensure_ascii=False保留中文等非ASCII字符，
        避免在传输过程中产生unicode编码问题。
        """
        if isinstance(self.content, Request):
            return json.dumps({"Request": self.content.to_dict()}, ensure_ascii=False)
        elif isinstance(self.content, Response):
            return json.dumps({"Response": self.content.to_dict()}, ensure_ascii=False)
        elif isinstance(self.content, Event):
            return json.dumps({"Event": self.content.to_dict()}, ensure_ascii=False)
        elif isinstance(self.content, Stream):
            # Stream通常作为二进制发送
            return json.dumps({"Stream": {
                "id": self.content.id,
                "tag": self.content.tag,
                "bytes": list(self.content.bytes),
                "data": self.content.data
            }}, ensure_ascii=False)
        return "{}"


@dataclass 
class RecognizeResult:
    """Speech recognition result from speaker."""
    text: str
    is_final: bool
    
    @classmethod
    def from_instruction_data(cls, data: dict) -> Optional["RecognizeResult"]:
        """
        从instruction事件数据解析语音识别结果。
        
        instruction事件格式:
        {
            "event": "instruction",
            "data": {
                "NewLine": "{header和payload的JSON字符串}"
            }
        }
        """
        try:
            new_line = data.get("NewLine")
            if not new_line:
                logger.debug("instruction数据中无NewLine字段: %s", list(data.keys()))
                return None
            
            line = json.loads(new_line) if isinstance(new_line, str) else new_line
            
            header = line.get("header", {})
            payload = line.get("payload", {})
            
            namespace = header.get("namespace", "")
            name = header.get("name", "")
            is_final = payload.get("is_final", False)
            
            logger.debug("解析instruction: namespace=%s, name=%s, is_final=%s", 
                        namespace, name, is_final)
            
            # 检查是否是最终的语音识别结果
            if namespace == "SpeechRecognizer" and name == "RecognizeResult" and is_final:
                results = payload.get("results", [])
                if results and results[0].get("text"):
                    text = results[0]["text"]
                    logger.info("解析到语音识别结果: %s", text)
                    return cls(
                        text=text,
                        is_final=True
                    )
                else:
                    logger.debug("语音识别结果为空或无文本: %s", results)
            
            return None
            
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("解析语音识别结果失败: %s, data=%s", e, str(data)[:200])
            return None


@dataclass
class CommandResult:
    """Shell command execution result."""
    stdout: str
    stderr: str
    exit_code: int
    
    @classmethod
    def from_dict(cls, data: dict) -> "CommandResult":
        return cls(
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
            exit_code=data.get("exit_code", -1)
        )
    
    @classmethod
    def from_json(cls, json_str: str) -> Optional["CommandResult"]:
        try:
            data = json.loads(json_str)
            return cls.from_dict(data)
        except json.JSONDecodeError:
            return None
