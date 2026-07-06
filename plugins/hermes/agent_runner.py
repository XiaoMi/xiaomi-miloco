import concurrent.futures
import logging
import threading

__all__ = [
    "AgentSessionPool",
]

logger = logging.getLogger(__name__)


def _resolve_runtime():
    from hermes_cli.config import cfg_get, load_config
    from hermes_cli.runtime_provider import resolve_runtime_provider

    runtime = resolve_runtime_provider()
    cfg = load_config()
    model = cfg_get(cfg, "model", "default", default="")
    if not model:
        import os
        model = os.getenv("HERMES_MODEL", "")
    runtime["model"] = model
    return runtime


class AgentSessionPool:
    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self._agents: dict[str, object] = {}
        self._lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="miloco-agent",
        )

    @classmethod
    def instance(cls):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @property
    def executor(self):
        return self._executor

    def get_or_create(
        self,
        *,
        session_key,
        extra_system_prompt=None,
    ):
        with self._lock:
            existing = self._agents.get(session_key)
            if existing is not None:
                return existing

            from run_agent import AIAgent
            from hermes_state import SessionDB

            runtime = _resolve_runtime()

            db = SessionDB()
            session_id = "miloco_{}".format(session_key)
            db.create_session(
                session_id=session_id,
                source="miloco",
                model=runtime.get("model", ""),
                user_id="miloco",
            )
            agent = AIAgent(
                model=runtime.get("model", ""),
                api_key=runtime.get("api_key"),
                base_url=runtime.get("base_url"),
                provider=runtime.get("provider"),
                max_iterations=90,
                disabled_toolsets=["cronjob"],
                platform="miloco",
                session_id=session_id,
                session_db=db,
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                ephemeral_system_prompt=extra_system_prompt,
            )
            self._agents[session_key] = agent
            logger.info("created AIAgent for session_key=%s", session_key)
            return agent

    def delete(self, session_key):
        with self._lock:
            agent = self._agents.pop(session_key, None)
        if agent is None:
            return False
        agent.close()
        from agent.auxiliary_client import cleanup_stale_async_clients

        cleanup_stale_async_clients()
        return True
