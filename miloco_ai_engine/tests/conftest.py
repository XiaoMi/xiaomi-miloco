# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Pytest fixtures."""
# pylint: disable=import-outside-toplevel, unused-argument, missing-function-docstring, line-too-long, redefined-outer-name, W0212, c0115
import pytest
import json
import logging

logger = logging.getLogger(__name__)

MOCK_MODEL_NAME = "mock-model"
MOCK_MODEL_FILE = "fake-model.gguf"
MOCK_MODEL_CONFIG = {
    "logging": {
        "log_level": "INFO",
        "enable_console_logging": True,
        "enable_file_logging": False
    },
    "server": {
        "host": "127.0.0.1",
        "port": 18080
    },
    "app": {
        "title": "test",
        "service_name": "ai_engine",
        "description": "",
        "version": "0.0.1"
    },
    "models": {
        MOCK_MODEL_NAME: {
            "model_path": f"/tmp/{MOCK_MODEL_FILE}",
            "mmproj_path": None,
            "device": "cpu",
            "cache_seq_num": 1,
            "parallel_seq_num": 2,
            "total_context_num": 4096,
            "context_per_seq": 4096,
            "chunk_size": 512
        }
    },
    "server_concurrency": {
        "max_queue_size": 100,
        "abandon_low_priority": True,
        "queue_wait_timeout": 30
    },
    "auto_opt_vram": True,
}

TEST_PAYLOAD = {
    "model": MOCK_MODEL_NAME,
    "messages": [{
        "role": "user",
        "content": "hi"
    }],
    "tools": [],
    "stream": False
}

TEST_PAYLOAD_STREAM = {
    "model": MOCK_MODEL_NAME,
    "messages": [{
        "role": "user",
        "content": "hi"
    }],
    "tools": [],
    "stream": True
}


class _MockCLib:
    def __init__(self) -> None:
        self.inited = False

    def llama_mico_init(self, config_json_bytes, handle_ptr):
        self.inited = True
        handle_ptr._obj.value = 1  # fake pointer
        return 0

    def llama_mico_free(self, handle):
        return 0

    def llama_mico_request_prompt(self, handle, req_json_bytes, is_finished_ptr, content_ptr):
        text = "hello world"
        content_ptr._obj.value = text.encode("utf-8")
        is_finished_ptr._obj.value = 0
        return 0

    def llama_mico_request_generate(self, handle, req_json_bytes, is_finished_ptr, content_ptr):
        data = json.loads(req_json_bytes.decode("utf-8"))
        if data.get("stop"):
            is_finished_ptr._obj.value = 1
            content_ptr._obj.value = b""
            return 0
        content_ptr._obj.value = b"<tool_call>\n{\"name\": \"get_weather\", \"arguments\": {\"city\": \"Beijing\"}}\n</tool_call>"
        is_finished_ptr._obj.value = 1
        return 0


@pytest.fixture(scope="session", autouse=True)
def set_logger():
    logger_set = logging.getLogger()
    logger_set.setLevel(logging.DEBUG)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    console_handler.setFormatter(formatter)
    logger_set.addHandler(console_handler)
    logger_set.info("set logger, %s", logger)


@pytest.fixture(scope="session", autouse=True)
def test_mock_model_name() -> str:
    return MOCK_MODEL_NAME


@pytest.fixture(scope="session", autouse=True)
def test_mock_model_file() -> str:
    return MOCK_MODEL_FILE


@pytest.fixture(scope="session", autouse=True)
def test_mock_model_config() -> dict:
    return MOCK_MODEL_CONFIG


@pytest.fixture(scope="session", autouse=True)
def test_mock_mico_clib() -> _MockCLib:
    return _MockCLib()


@pytest.fixture(scope="session", autouse=True)
def test_mock_payload() -> dict:
    return TEST_PAYLOAD


@pytest.fixture(scope="session", autouse=True)
def test_mock_payload_stream() -> dict:
    return TEST_PAYLOAD_STREAM
