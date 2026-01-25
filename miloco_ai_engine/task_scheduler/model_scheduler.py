# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Task scheduler module for managing task scheduling and execution."""
from thespian.actors import Actor, ActorAddress
from miloco_ai_engine.config.config_info import ModelConfig
from miloco_ai_engine.config.config import SERVER_CONCURRENCY
from typing import List, Dict
import threading
import queue
from miloco_ai_engine.schema.actor_message import RequestMessage, TaskSchedulerAction, TaskAction, actor_system
import uuid
from miloco_ai_engine.schema.models_schema import ChatCompletionRequest
import asyncio
from miloco_ai_engine.task_scheduler.scheduler_task import Task
from miloco_ai_engine.middleware.exceptions import ModelSchedulerException
import time
from concurrent.futures import Future

import logging
logger = logging.getLogger(__name__)


class TaskScheduler(Actor):
    """Task Scheduler"""

    _MAX_IDLE_TIME = 10 * 60  # Thread idle time 10 minutes
    # _DEFAULT_WORKER_COUNT = 10  # Default number of threads

    def __init__(self, _model_name: str, model_config: ModelConfig):
        super().__init__()
        # Mapping from task ID to task
        self.tasks: Dict[str, ActorAddress] = {}

        # model_config cannot be transferred properly during Actor init
        self.model_config = model_config
        self.running = False
        self.handle = None

        self.max_queue_size = SERVER_CONCURRENCY["max_queue_size"]
        self.abandon_low_priority = SERVER_CONCURRENCY.get(
            "abandon_low_priority", True)

        self.max_priority = 0

        self.workers: List[threading.Thread] = []
        # Mapping from thread name to thread object
        self.worker_threads: Dict[str, threading.Thread] = {}
        self.worker_loops: Dict[str, asyncio.AbstractEventLoop] = {}
        self.current_worker_count = 0

        self.default_worker_prefix = "DefaultWorker"
        self.default_woker_names: List[str] = []
        self.task_queue = queue.PriorityQueue(maxsize=self.max_queue_size)

    def receiveMessage(self, msg: RequestMessage, sender: ActorAddress):
        logger.debug("model_scheduler ReceiveMessage:  %s", msg)

        if msg.action == TaskSchedulerAction.START:
            self._start(msg.data)

        elif msg.action == TaskSchedulerAction.STOP:
            self._stop()

        elif msg.action == TaskSchedulerAction.SUBMIT_TASK:
            self._handle_submit_task(msg)

        elif msg.action == TaskSchedulerAction.CLEANUP:
            self._cleanup()

        else:
            logger.error("Unknown task scheduler action: %s", msg.action)

    def _start(self, handle):
        """
        Start task scheduler
        """
        if self.running:
            return

        self.running = True
        self.handle = handle
        self.max_workers = self.model_config.n_seq_max - \
            self.model_config.cache_seq_num  # seq parallel
        self.default_worker_count = self.max_workers  # Default number of queues

        # Initialize default threads
        for i in range(self.default_worker_count):
            worker_name = f"{self.default_worker_prefix}-{i}"
            self.default_woker_names.append(worker_name)
            self._create_worker(worker_name)

        logger.info(
            "Task scheduler %s started, initialized %s concurrent task threads",
            self.model_config.model_name, self.max_workers)

    def _stop(self):
        """
        Stop task scheduler
        """
        if not self.running:
            return

        self.running = False
        self.handle = None

        alive = [worker for worker in self.workers if worker.is_alive()]
        for worker in alive:
            worker.join(timeout=5)        # Task queue auto-cleanup

        logger.info("Task scheduler %s stopped", self.model_config.model_name)

    def _create_worker_loop(self, worker_name: str):
        """
        Manage event loop
        """
        # Create independent event loop for each thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.worker_loops[worker_name] = loop
        try:
            loop.run_until_complete(self._work_loop(worker_name))
        finally:
            loop.close()
            self._cleanup_worker(worker_name)

    async def _work_loop(self, worker_name: str):
        """
        Worker thread loop
        """
        last_task_time = time.time()
        loop = self.worker_loops[worker_name]
        while self.running:
            try:
                # Get task from central queue (500ms polling to detect stop)
                priority, task_id = await asyncio.to_thread(self.task_queue.get, timeout=0.5)
                priority = -priority
            except Exception:  # pylint: disable=broad-exception-caught
                # Check if idle time exceeded
                current_time = time.time()
                if current_time - last_task_time > self._MAX_IDLE_TIME:
                    if worker_name not in self.default_woker_names:
                        logger.info(
                            "Worker thread %s idle for more than %ss, auto-terminating",
                            worker_name, self._MAX_IDLE_TIME)
                        break
                continue

            last_task_time = time.time()
            task = self.tasks.get(task_id)
            if not task:
                continue

            future: Future = actor_system.ask(
                task,
                RequestMessage(action=TaskAction.START, data=loop))
            try:
                # Convert concurrent.futures.Future to asyncio.Future to avoid blocking
                asyncio_future = asyncio.wrap_future(future)
                result: bool = await asyncio_future
                if result:
                    logger.debug("Worker thread %s task completed: %s", worker_name, task_id)
                else:
                    raise ModelSchedulerException("Execution failed return")
            except Exception as e:  # pylint: disable=broad-except
                logger.error(
                    "Worker thread %s failed to get task response: %s %s", worker_name, task_id, e)
            finally:
                self.tasks.pop(task_id, None)

    def _create_worker(self, worker_name: str):
        """
        Create a single worker thread
        """
        worker = threading.Thread(target=self._create_worker_loop,
                                  name=worker_name,
                                  args=(worker_name, ))
        worker.daemon = True
        worker.start()

        self.workers.append(worker)
        self.worker_threads[worker_name] = worker
        self.current_worker_count += 1

    def _cleanup_worker(self, worker_name: str):
        """
        Cleanup thread resources
        """
        if worker_name in self.worker_threads:
            del self.worker_threads[worker_name]

        if worker_name in self.worker_loops:
            del self.worker_loops[worker_name]

        tmp_workers = [
            worker for worker in self.workers if worker.name != worker_name]
        self.workers = tmp_workers

        self.current_worker_count = max(0, self.current_worker_count - 1)

        logger.info(
            "Worker thread %s cleaned up, current thread count: %s",
            worker_name, self.current_worker_count)

    def _handle_submit_task(self, message: RequestMessage):
        """
        Handle submit task
        """
        task_id = str(uuid.uuid4())

        request: ChatCompletionRequest = message.data
        task_priority = request.priority or 0
        task_label = self.default_worker_prefix
        # Do not distinguish different task queues for now
        task = actor_system.createActor(
            lambda: Task(task_id, task_label, self.handle, self.myAddress, request, message
                         .call_back_message, task_priority))
        self.tasks[task_id] = task

        try:
            self.task_queue.put_nowait((-task_priority, task_id))
        except Exception as exc: # pylint: disable=broad-exception-caught
            logger.error("Task %s-%s queue full, submit failed: %s", task_id, task_label, exc)
            raise ModelSchedulerException(f"Task queue full, submit failed: {str(exc)}") from exc

    def _create_dynamic_worker(self, worker_name: str):
        """
        Dynamically create new thread
        """
        if self.current_worker_count >= self.max_workers:
            logger.warning(
                "Thread count reached max recommended value %s, creating new thread %s may cause stuttering, "
                "current thread count: %s",
                self.max_workers, worker_name, self.current_worker_count)

        self._create_worker(worker_name)
        logger.info(
            "Dynamically created new thread: %s, current thread count: %s",
            worker_name, self.current_worker_count)
        return True

    def _cleanup(self):
        """
        Cleanup task scheduler
        """
        pass
