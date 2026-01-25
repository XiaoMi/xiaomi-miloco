# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

# pylint: disable=import-outside-toplevel, unused-argument, missing-function-docstring, line-too-long, C0114, C0103, W0212
import pytest
from unittest.mock import MagicMock, patch, call
from miloco_ai_engine.task_scheduler.model_scheduler import TaskScheduler
from miloco_ai_engine.task_scheduler.scheduler_task import Task, TaskStatus
from miloco_ai_engine.schema.actor_message import RequestMessage, TaskSchedulerAction
from miloco_ai_engine.schema.models_schema import ChatCompletionRequest, ChatMessage
from thespian.actors import ActorAddress
import time
import uuid
import asyncio
import logging

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.unit]


@patch("miloco_ai_engine.task_scheduler.model_scheduler.threading.Thread")
def test_task_scheduler_start_stop(MockThread):
    """Test the start and stop functions of the task scheduler"""
    # Create mock configuration
    mock_config = MagicMock()
    mock_config.n_seq_max = 4
    mock_config.cache_seq_num = 1

    # Create scheduler instance
    scheduler = TaskScheduler("test_model", mock_config)

    # Simulate starting
    mock_handle = MagicMock()
    scheduler.receiveMessage(
        RequestMessage(action=TaskSchedulerAction.START, data=mock_handle),
        None)

    # Verify startup status
    assert scheduler.running is True
    assert scheduler.handle == mock_handle
    assert scheduler.max_workers == 3  # n_seq_max - cache_seq_num
    assert len(scheduler.workers) == 3
    assert MockThread.call_count == 3

    # Simulate stopping
    scheduler.receiveMessage(RequestMessage(action=TaskSchedulerAction.STOP),
                             None)

    # Verify stop status
    assert scheduler.running is False
    assert scheduler.handle is None


@patch("miloco_ai_engine.task_scheduler.model_scheduler.actor_system.createActor")
def test_task_submission_with_priority(MockCreateActor):
    """Test task submission with priority from request"""
    mock_task_priority = 5
    # Create mock configuration
    mock_config = MagicMock()
    mock_config.n_seq_max = 2
    mock_config.cache_seq_num = 0

    # Create scheduler instance and start
    scheduler = TaskScheduler("test_model", mock_config)
    scheduler.receiveMessage(
        RequestMessage(action=TaskSchedulerAction.START, data=MagicMock()),
        None)

    # Create test request with priority
    request = ChatCompletionRequest(
        model="test_model",
        messages=[ChatMessage(role="user", content="Query Beijing weather")],
        stream=False,
        priority=mock_task_priority)

    # Submit task
    submit_msg = RequestMessage(action=TaskSchedulerAction.SUBMIT_TASK,
                                data=request)
    scheduler.receiveMessage(submit_msg, None)

    # Verify task creation
    assert len(scheduler.tasks) == 1
    task_id = list(scheduler.tasks.keys())[0]
    MockCreateActor.assert_called_once()

    # Verify task priority from request
    assert scheduler.task_queue.qsize() == 1
    priority, queued_task_id = scheduler.task_queue.get()
    assert queued_task_id == task_id
    assert priority == -mock_task_priority


@patch("miloco_ai_engine.task_scheduler.model_scheduler.actor_system.ask")
def test_worker_thread_task_processing(MockAsk):
    """Test the workflow of worker thread processing tasks"""
    # Create mock configuration
    mock_config = MagicMock()
    mock_config.n_seq_max = 1
    mock_config.cache_seq_num = 0

    # Create scheduler instance and start
    scheduler = TaskScheduler("test_model", mock_config)
    scheduler.receiveMessage(
        RequestMessage(action=TaskSchedulerAction.START, data=MagicMock()),
        None)

    # Add test task
    task_id = str(uuid.uuid4())
    scheduler.tasks[task_id] = MagicMock(spec=ActorAddress)
    scheduler.task_queue.put((-1, task_id))  # Priority 1

    # Execute worker thread
    worker_name = list[str](scheduler.worker_threads.keys())[0]
    time.sleep(0.5)
    # Verify task processing
    MockAsk.assert_called_once()

    # Verify cleanup after task completion
    assert task_id not in scheduler.tasks

    scheduler.receiveMessage(RequestMessage(action=TaskSchedulerAction.STOP),
                             None)

    assert worker_name not in scheduler.worker_threads or not scheduler.worker_threads[worker_name].is_alive(
    )


@patch("miloco_ai_engine.task_scheduler.scheduler_task.llama_mico")
@patch("miloco_ai_engine.task_scheduler.scheduler_task.Task._call_model_wrapper")
def test_task_timeout_handling(MockCallModelWrapper, MockLlama):
    """Test the handling logic of task timeout"""
    # Create task instance
    task_id = str(uuid.uuid4())
    task = Task(task_id=task_id,
                table="test_table",
                handle=MagicMock(),
                task_scheduler=MagicMock(),
                request=ChatCompletionRequest(
                    model="test_model",
                    messages=[ChatMessage(role="user", content="hello")],
                    stream=False),
                respone_message=MagicMock(),
                priority=1)

    # Set task creation time to before timeout (default timeout 10 seconds)
    task.task_info.created_at = time.time() - 20

    # Execute task
    asyncio.run(task._handle_start_task())

    # Verify task status
    assert task.task_info.status == TaskStatus.CANCELLED
    assert "wait timeout" in task.task_info.error

    # Verify callback call
    MockCallModelWrapper.assert_called_once()


@patch("miloco_ai_engine.task_scheduler.scheduler_task.llama_mico")
@patch("miloco_ai_engine.task_scheduler.scheduler_task.Task._call_model_wrapper")
def test_task_successful_execution(MockCallModelWrapper, MockLlama):
    """Test the process of successful task execution"""
    # Create task instance
    task_id = str(uuid.uuid4())
    task = Task(task_id=task_id,
                table="test_table",
                handle=MagicMock(),
                task_scheduler=MagicMock(),
                request=ChatCompletionRequest(
                    model="test_model",
                    messages=[ChatMessage(role="user", content="hello")],
                    stream=False),
                respone_message=MagicMock(),
                priority=1)

    # Set mock response
    mock_response = MagicMock()
    MockLlama.chat_completion.return_value = mock_response

    # Execute task
    asyncio.run(task._handle_start_task())

    # Verify task status
    assert task.task_info.status == TaskStatus.COMPLETED
    assert task.task_info.error is None

    # Verify model call
    MockLlama.chat_completion.assert_called_once()

    # Verify callback call
    MockCallModelWrapper.assert_called_once_with(mock_response)


@patch("miloco_ai_engine.task_scheduler.scheduler_task.llama_mico")
@patch("miloco_ai_engine.task_scheduler.scheduler_task.Task._call_model_wrapper")
def test_streaming_response_handling(MockCallModelWrapper, MockLlama):
    """Test the handling of streaming responses"""
    # Create task instance (streaming request)
    task_id = str(uuid.uuid4())
    task = Task(task_id=task_id,
                table="test_table",
                handle=MagicMock(),
                task_scheduler=MagicMock(),
                request=ChatCompletionRequest(
                    model="test_model",
                    messages=[ChatMessage(role="user", content="hello")],
                    stream=True),
                respone_message=MagicMock(),
                priority=1)

    # Set mock streaming response
    chunk1 = MagicMock()
    chunk2 = MagicMock()
    MockLlama.chat_completion.return_value = [chunk1, chunk2]

    # Execute task
    asyncio.run(task._handle_start_task())

    # Verify task status
    assert task.task_info.status == TaskStatus.COMPLETED

    # Verify the number of callback calls
    assert MockCallModelWrapper.call_count == 2
    MockCallModelWrapper.assert_has_calls([call(chunk1), call(chunk2)])
