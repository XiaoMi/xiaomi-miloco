# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
Trigger rule dynamic executor module
"""
import logging
import asyncio
from typing import Optional

from fastapi import WebSocket
from miloco_server.schema.miot_schema import CameraImgSeq
from thespian.actors import Actor, ActorAddress, ActorExitRequest

from miloco_server.dao.trigger_rule_log_dao import TriggerRuleLogDAO
from miloco_server.utils.chat_companion import ChatCachedData
from miloco_server import actor_system
from miloco_server.agent.dynamic_execute_agent import ActionDescriptionDynamicExecuteAgent
from miloco_server.schema.chat_schema import Event, Instruction, InstructionPayload, Internal, Nlp, Confirmation
from miloco_server.schema.chat_history_schema import ChatHistorySession
from miloco_server.schema.trigger_log_schema import AiRecommendDynamicExecuteResult, ExecuteResult
from miloco_server.schema.trigger_schema import TriggerRule

logger = logging.getLogger(__name__)

START = "start"

class RegisterWebSocket():
    """Register WebSocket"""
    def __init__(self, web_socket: WebSocket):
        self.web_socket = web_socket

class TriggerRuleDynamicExecutor(Actor):
    """
    TriggerRuleDynamicExecutor Actor - Actor model implemented using thespian
    Concurrent component for handling WebSocket messages and event dispatching
    """

    def __init__(self,
                 request_id: str,
                 trigger_rule: TriggerRule,
                 trigger_rule_log_dao: TriggerRuleLogDAO,
                 camera_motion_dict: dict[str, dict[int,
                                           tuple[bool,
                                                 Optional[CameraImgSeq]]]],
                 ):
        super().__init__()

        from miloco_server.service.manager import get_manager  # pylint: disable=import-outside-toplevel
        self._manager = get_manager()
        self._chat_companion = self._manager.chat_companion
        self.request_id = request_id
        self._trigger_rule = trigger_rule
        self._trigger_rule_log_dao = trigger_rule_log_dao
        self.session_id = "no session id"
        self._session: ChatHistorySession = ChatHistorySession()
        self._web_sockets: list[WebSocket] = []
        self._future: Optional[asyncio.Future] = None
        self._camera_motion_dict = camera_motion_dict
        logger.info("[%s] TriggerRuleDynamicExecutor init", self.request_id)


    def receiveMessage(self, msg, sender):
        """
        Actor message receiving method, handles received messages
        """
        try:
            if isinstance(msg, str) and msg == START:
                self._future = asyncio.Future()
                self.send(sender, self._future)
                self._handle_start()
            elif isinstance(msg, InstructionPayload):
                self._handle_instruction_payload(msg)
            elif isinstance(msg, RegisterWebSocket):
                self._handle_register_web_socket(msg)
            elif isinstance(msg, ActorExitRequest):
                logger.info("[%s] ActorExitRequest received", self.request_id)
                self._handle_exit_request()
            else:
                logger.warning(
                    "[%s] Invalid message format: %s", self.request_id, msg)
        except Exception as e:  # pylint: disable=broad-except
            logger.error(
                "[%s] Error in receiveMessage method: %s", self.request_id, e, exc_info=True)
            self._close_web_sockets()
            if self._future:
                self._future.set_result(False)
                self._future = None

    def _handle_start(self):
        """
        Start to send msg
        """
        self._chat_agent: Optional[ActorAddress] = actor_system.createActor(
            lambda: ActionDescriptionDynamicExecuteAgent(
                self.request_id, self.myAddress, None,
            ))
        mock_event_payload = Nlp.ActionDescriptionDynamicExecute(
            action_descriptions=self._trigger_rule.execute_info.ai_recommend_action_descriptions,
            mcp_list=self._trigger_rule.execute_info.mcp_list,
            camera_ids=self._trigger_rule.cameras,
        )
        mock_event = Event.build_event(mock_event_payload, self.request_id, self.session_id)

        camera_images = self._get_camera_images(self._trigger_rule.cameras)
        self._chat_companion.set_chat_data(self.request_id, ChatCachedData(
            camera_images=camera_images,
        ))

        actor_system.tell(self._chat_agent, mock_event)


    def _get_camera_images(self, camera_ids: list[str]) -> list[CameraImgSeq]:
        """Get camera images"""
        camera_images: list[CameraImgSeq] = []
        for camera_id in camera_ids:
            channel_images = self._camera_motion_dict[camera_id]
            for _, (is_motion, camera_img_seq) in channel_images.items():
                if is_motion and camera_img_seq:
                    camera_images.append(camera_img_seq)
        return camera_images


    def _handle_instruction_payload(self, instruction_payload: InstructionPayload) -> None:
        """
        Handle Instruction object
        """
        if isinstance(instruction_payload, Internal.Dispatcher):
            return
        elif isinstance(instruction_payload, Confirmation.AiGeneratedActions):
            # this instruction is not need to be stored
            return

        instruction = Instruction.build_instruction(instruction_payload, self.request_id, self.session_id)

        self._session.add_instruction(instruction)
        asyncio.create_task(self._send_instruction(instruction))



    async def _send_instruction(self, instruction: Instruction):
        """
        Send instruction
        """
        msg = instruction.model_dump_json()
        logger.info("send_instruction: %s", msg)
        for web_socket in self._web_sockets:
            await self._send_message(web_socket, msg)
        if instruction.judge_type("Dialog", "Finish"):
            logger.info("[%s] Dialog.Finish received, requesting Actor exit", self.request_id)
            actor_system.tell(self.myAddress, ActorExitRequest())

    async def _send_message(self, web_socket: WebSocket, message: str):
        """
        Send message
        """
        try:
            await web_socket.send_text(message)
        except Exception as e:  # pylint: disable=broad-except
            logger.error("[%s] Error sending message: %s", self.request_id, e)
            pass

    async def _send_all_sessions(self, web_socket: WebSocket):
        """
        Send all sessions to web socket in order, then add to web_sockets list
        Uses index tracking to ensure no messages are missed
        """
        try:
            sent_index = 0
            # Keep sending until no new data arrives
            # No sleep needed: self._session.data modifications are synchronous and immediately visible
            while True:
                current_count = len(self._session.data)
                # Send all new sessions
                while sent_index < current_count:
                    session = self._session.data[sent_index]
                    session_json = session.model_dump_json()
                    logger.info("send session to web socket: %s", session_json)
                    await self._send_message(web_socket, session_json)
                    sent_index += 1

                if len(self._session.data) == current_count:
                    break

            logger.info("append web socket after sending all sessions")
            self._web_sockets.append(web_socket)
        except Exception as e:  # pylint: disable=broad-except
            logger.error("[%s] Error sending all sessions: %s", self.request_id, e)
            # Still add to list on error to avoid blocking
            if web_socket not in self._web_sockets:
                self._web_sockets.append(web_socket)

    def _handle_register_web_socket(self, register_web_socket: RegisterWebSocket):
        """Handle Register WebSocket"""
        asyncio.create_task(self._send_all_sessions(register_web_socket.web_socket))

    def _handle_exit_request(self):
        """Handle Actor exit request"""
        self._close_web_sockets()
        self._store_chat_history_session()
        if self._future:
            self._future.set_result(True)
            self._future = None
        logger.info("[%s] Exit request handled successfully", self.request_id)

    def _store_chat_history_session(self):
        """Store chat history session"""
        execute_result, _ = self._trigger_rule_log_dao.get_execute_result(self.request_id)
        if execute_result and execute_result.ai_recommend_dynamic_execute_result:
            execute_result.ai_recommend_dynamic_execute_result.chat_history_session = self._session
            execute_result.ai_recommend_dynamic_execute_result.is_done = True
        else:
            execute_result = ExecuteResult(
                ai_recommend_execute_type=self._trigger_rule.execute_info.ai_recommend_execute_type,
                ai_recommend_dynamic_execute_result=AiRecommendDynamicExecuteResult(
                    is_done=True,
                    ai_recommend_action_descriptions=self._trigger_rule.execute_info.ai_recommend_action_descriptions,
                    chat_history_session=self._session,
                ),
            )

        self._trigger_rule_log_dao.update_execute_result(self.request_id, execute_result)


    def _close_web_sockets(self):
        try:
            if not self._web_sockets:
                return
            for web_socket in self._web_sockets:
                if (hasattr(web_socket, "client_state")
                        and web_socket.client_state.value == 3):
                    logger.info("[%s] WebSocket already closed", self.request_id)
                    continue

                asyncio.create_task(web_socket.close())
        except Exception as e:  # pylint: disable=broad-except
            logger.error("[%s] Error closing WebSocket: %s", self.request_id, e)



