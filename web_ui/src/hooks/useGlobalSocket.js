/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import { useEffect, useCallback, useRef } from 'react';
import { message } from 'antd';
import { useChatStore, useSessionChatStore, useSocketStore } from '@/stores/chatStore';
import { generateRequestId, createRequestMessage } from '@/utils/instruction';
import { SOCKET_STATUS } from '@/constants/messageTypes';
import { getMessageIsCameraImages, getMessageIsException, getMessageIsFinishChat, getMessageIsSaveRuleConfirm, getMessageIsSaveRuleConfirmResult, getMessageIsToastStream } from '@/utils/instruction/typeUtils';

// global socket management hook
export const useGlobalSocket = () => {
  const reconnectTimerRef = useRef(null);
  const socketWaitingTimerRef = useRef(null);

  // Zustand stores
  const {
    sessionId,
    isAnswering,
    socketWaiting,
    setSocketStatus,
    setIsSocketActive,
    addAnswerMessage,
    setAnswerMessages,
    setCurrentAnswer,
    setSessionId,
    setCurrentRequestId,
    setIsAnswering,
    setSocketWaiting
  } = useChatStore();

  const {
    socketRef,
    setSocketRef,
    setSocketStatus: setGlobalSocketStatus,
    setIsStreaming
  } = useSocketStore();

  const { saveTempChatState } = useSessionChatStore();

  // clear reconnect timer
  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  }, []);

  const updateBuildingAnswer = useCallback(() => {
    const currentState = useChatStore.getState();
    const { answerMessages } = currentState;
    if (Array.isArray(answerMessages) && answerMessages.length > 0) {
      const buildingAnswer = {
        type: 'answer',
        socketMessages: answerMessages,
        isBuilding: true,
        isComplete: false
      };

      const currentMessages = Array.isArray(currentState.messages) ? currentState.messages : [];

      const messagesWithoutBuilding = currentMessages.filter(msg => !msg.isBuilding);
      const newMessages = [...messagesWithoutBuilding, buildingAnswer];

      useChatStore.setState({
        messages: newMessages,
        currentAnswer: buildingAnswer
      });

    }
  }, []);

  const startSocketWaitingTimer = useCallback(() => {
    socketWaitingTimerRef.current = setTimeout(() => {
      setSocketWaiting(true);
    }, 500);
  }, [setSocketWaiting]);

  const clearSocketWaitingTimer = useCallback(() => {
    if (socketWaitingTimerRef.current) {
      clearTimeout(socketWaitingTimerRef.current);
      socketWaitingTimerRef.current = null;
    }
    setSocketWaiting(false);
  }, [setSocketWaiting]);

  // global message processing logic
  const handleGlobalSocketMessage = useCallback((messageData) => {
    clearSocketWaitingTimer();
    startSocketWaitingTimer();
    const { header, payload } = messageData;
    const { type, namespace, name } = header;

    try {
      if (getMessageIsToastStream(type, namespace, name)) {
        const currentState = useChatStore.getState();

        const currentAnswerMessages = Array.isArray(currentState.answerMessages) ? currentState.answerMessages : [];
        const newMessages = [...currentAnswerMessages];

        let lastToastStreamIndex = -1;
        if (newMessages.length > 0) {
          const lastMsg = newMessages[newMessages.length - 1];
          if (getMessageIsToastStream(lastMsg.header.type, lastMsg.header.namespace, lastMsg.header.name)) {
            lastToastStreamIndex = newMessages.length - 1;
          }
        }

        if (lastToastStreamIndex >= 0) {
          const existingMsg = newMessages[lastToastStreamIndex];
          const existingPayload = JSON.parse(existingMsg.payload);
          const newPayload = JSON.parse(payload);

          const accumulatedPayload = {
            ...existingPayload,
            stream: (existingPayload.stream || '') + (newPayload.stream || '')
          };

          newMessages[lastToastStreamIndex] = {
            ...existingMsg,
            payload: JSON.stringify(accumulatedPayload),
            isAccumulating: true,
            lastUpdated: Date.now()
          };
        } else {
          newMessages.push({
            ...messageData,
            isAccumulating: true,
            lastUpdated: Date.now()
          });
        }

        setAnswerMessages(newMessages);

        if (newMessages.length > 0) {
          const buildingAnswer = {
            type: 'answer',
            socketMessages: newMessages,
            isBuilding: true,
            isComplete: false
          };

          const currentMessages = Array.isArray(currentState.messages) ? currentState.messages : [];
          const messagesWithoutBuilding = currentMessages.filter(msg => !msg.isBuilding);
          const updatedMessages = [...messagesWithoutBuilding, buildingAnswer];

          useChatStore.setState({
            messages: updatedMessages,
            currentAnswer: buildingAnswer
          });

        }
      } else if (getMessageIsCameraImages(type, namespace, name)) {
        console.log('handle CameraImages message:', JSON.parse(payload));
        addAnswerMessage(messageData);
        setCurrentAnswer({
          type: 'answer'
        });
      } else if (getMessageIsSaveRuleConfirmResult(type, namespace, name)) {
        setAnswerMessages(prev => {
          const safeMessages = Array.isArray(prev) ? prev : [];

          // extract camera_options and action_options from SaveRuleConfirm message
          let cameraOptionsFromConfirm = null;
          let actionOptionsFromConfirm = null;
          safeMessages.forEach(msg => {
            const { type, namespace, name } = msg.header;
            if (getMessageIsSaveRuleConfirm(type, namespace, name)) {
              try {
                const payload = JSON.parse(msg.payload);
                if (payload.camera_options) {
                  cameraOptionsFromConfirm = payload.camera_options;
                }
                if (payload.action_options) {
                  actionOptionsFromConfirm = payload.action_options;
                }
              } catch (error) {
                console.warn('parse SaveRuleConfirm message payload failed:', error);
              }
            }
          });

          // filter out previous SaveRuleConfirm messages
          const filteredMessages = safeMessages.filter(msg => {
            const { type, namespace, name } = msg.header;
            const shouldRemove = getMessageIsSaveRuleConfirm(type, namespace, name);
            if (shouldRemove) {
              console.log('remove message:', name, 'request_id:', msg?.header?.request_id);
            }
            return !shouldRemove;
          });
          let mergedMessageData = messageData;
          if (cameraOptionsFromConfirm || actionOptionsFromConfirm) {

            try {
              const currentPayload = JSON.parse(messageData.payload);
              const mergedPayload = {
                ...currentPayload,
                ...(cameraOptionsFromConfirm && { camera_options: cameraOptionsFromConfirm }),
                ...(actionOptionsFromConfirm && { action_options: actionOptionsFromConfirm })
              };
              mergedMessageData = {
                ...messageData,
                payload: JSON.stringify(mergedPayload)
              };
            } catch (error) {
              console.warn('merge camera options failed:', error);
            }
          } else {
            console.log('no data to merge');
          }

          const finalMessages = [...filteredMessages, mergedMessageData];

          return finalMessages;
        });
      } else if (getMessageIsFinishChat(type, namespace, name)) {
        clearSocketWaitingTimer();
        const currentState = useChatStore.getState();
        const finalAnswerMessages = [...(Array.isArray(currentState.answerMessages) ? currentState.answerMessages : [])];

        // build final answer message
        const finalAnswer = {
          type: 'answer',
          socketMessages: finalAnswerMessages,
          isComplete: true,
          isBuilding: false
        };

        if (finalAnswerMessages.length > 0) {
          const currentMessages = Array.isArray(currentState.messages) ? currentState.messages : [];
          const filteredMessages = currentMessages.filter(msg => !msg.isBuilding);
          const newMessages = [...filteredMessages, finalAnswer];
          useChatStore.setState({
            messages: newMessages,
            currentAnswer: null,
            answerMessages: []
          });

          // 如果选择了音箱播报，提取AI回复文本并发送到音箱
          if (currentState.selectedSpeakerId) {
            try {
              let fullText = '';
              finalAnswerMessages.forEach(msg => {
                try {
                  const { type: mType, namespace: mNs, name: mName } = msg.header || {};
                  if (getMessageIsToastStream(mType, mNs, mName)) {
                    const msgPayload = JSON.parse(msg.payload);
                    fullText += (msgPayload.stream || '');
                  }
                } catch (e) { /* ignore */ }
              });
              if (fullText.trim()) {
                console.log('📢 发送AI回复到音箱:', currentState.selectedSpeakerId, fullText.substring(0, 50));
                useChatStore.getState().speakToSelectedSpeaker(fullText.trim());
              }
            } catch (speakerError) {
              console.error('音箱播报失败:', speakerError);
            }
          }
        }

        setIsAnswering(false);
        setIsSocketActive(false);
      } else {
        // getMessageIsException, getMessageIsSaveRuleConfirm, getMessageIsCallTool, getMessageIsCameraImages, getMessageIsCallToolResult
        setCurrentAnswer({
          type: 'answer'
        });
        addAnswerMessage(messageData);
      }

      const shouldUpdateBuilding = !(getMessageIsFinishChat(type, namespace, name)) &&
        !(getMessageIsToastStream(type, namespace, name));


      if (shouldUpdateBuilding) {
        updateBuildingAnswer();
      }
    } catch (error) {
      console.error('handle global socket message failed:', error);
    }
  }, []);

  const connect = useCallback((requestId, targetSessionId = null) => {
    if (socketRef?.readyState === WebSocket.OPEN) {
      socketRef.close();
    }

    setSocketStatus(SOCKET_STATUS.CONNECTING);
    setGlobalSocketStatus(SOCKET_STATUS.CONNECTING);

    const wsProtocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const baseUrl = `${wsProtocol}://${window.location.host}${import.meta.env.VITE_API_BASE || ''}/api/chat/ws/query`;

    const params = new URLSearchParams({ request_id: requestId });

    const useSessionId = targetSessionId || sessionId;
    if (useSessionId) {
      params.set('session_id', useSessionId);
      console.log('global socket connected, using session_id:', useSessionId);
    }

    const wsUrl = `${baseUrl}?${params.toString()}`;

    const socket = new WebSocket(wsUrl);

    socket.onopen = () => {
      console.log('global socket connected successfully');
      setSocketStatus(SOCKET_STATUS.CONNECTED);
      setGlobalSocketStatus(SOCKET_STATUS.CONNECTED);
      clearReconnectTimer();
    };

    socket.onmessage = (event) => {
      try {
        const messageData = JSON.parse(event.data);

        if (messageData.header?.session_id) {
          const newSessionId = messageData.header.session_id;
          if (newSessionId !== sessionId) {
            setSessionId(newSessionId);
            sessionStorage.setItem('chat_session_id', newSessionId);
          }
        }

        if (messageData.header?.request_id) {
          setCurrentRequestId(messageData.header.request_id);
        }

        setIsStreaming(true);
        setIsSocketActive(true);

        handleGlobalSocketMessage(messageData);
        const { type, namespace, name } = messageData.header;
        if (getMessageIsFinishChat(type, namespace, name)) {

          setIsStreaming(false);
          setIsSocketActive(false);
          setIsAnswering(false);

          setTimeout(() => {
            disconnectButKeepSession();
          }, 500);
        }

      } catch (error) {
        console.error('global socket message parse failed:', error);
      }
    };

    socket.onclose = (event) => {
      console.log('global socket connection closed:', event);
      setSocketStatus(SOCKET_STATUS.DISCONNECTED);
      setGlobalSocketStatus(SOCKET_STATUS.DISCONNECTED);
      setIsStreaming(false);
      clearSocketWaitingTimer();

      if (event.code !== 1000 && isAnswering && !event.wasClean) {
        console.log('detected abnormal disconnection, preparing to reconnect...');
        scheduleReconnect(requestId, targetSessionId);
      }
    };

    socket.onerror = (error) => {
      console.error('global socket error:', error);
      setSocketStatus(SOCKET_STATUS.ERROR);
      setGlobalSocketStatus(SOCKET_STATUS.ERROR);
      setIsStreaming(false);
      clearSocketWaitingTimer();
      message.error('connection failed, please try again');
    };

    const actions = {
      sendMessage: sendMessageDirect,
      disconnect,
      disconnectButKeepSession,
      resetSession,
      isConnected: () => socket.readyState === WebSocket.OPEN
    };

    setSocketRef(socket, actions);

    return socket;
  }, [sessionId, isAnswering, clearReconnectTimer, handleGlobalSocketMessage]);

  const scheduleReconnect = useCallback((requestId, targetSessionId, delay = 3000) => {
    clearReconnectTimer();

    reconnectTimerRef.current = setTimeout(() => {
      connect(requestId, targetSessionId);
    }, delay);
  }, [connect, clearReconnectTimer]);

  const sendMessage = useCallback((messageData) => {
    const requestId = generateRequestId();
    const socket = connect(requestId, sessionId);

    const waitForConnection = () => {
      if (socket.readyState === WebSocket.OPEN) {
        const message = createRequestMessage(requestId, sessionId, messageData);
        console.log('global socket send message:', message);
        socket.send(JSON.stringify(message));
        return requestId;
      } else if (socket.readyState === WebSocket.CONNECTING) {
        setTimeout(waitForConnection, 100);
      } else {
        console.error('Socket connection failed, cannot send message');
        setSocketStatus(SOCKET_STATUS.ERROR);
        setGlobalSocketStatus(SOCKET_STATUS.ERROR);
        return null;
      }
    };

    return waitForConnection();
  }, [connect, sessionId]);

  // directly send message (using shared socket reference)
  const sendMessageDirect = useCallback((messageData) => {
    if (!socketRef || socketRef.readyState !== WebSocket.OPEN) {
      console.error('Socket connection unavailable, cannot send message, socketRef:', socketRef);
      return null;
    }

    try {
      socketRef.send(JSON.stringify(messageData));
      console.log('send message directly:', messageData);
      return messageData.header?.request_id || generateRequestId();
    } catch (error) {
      console.error('send message failed:', error);
      return null;
    }
  }, [sessionId, socketRef]);

  // disconnect
  const disconnect = useCallback(() => {
    console.log('disconnect global socket');
    clearReconnectTimer();

    if (socketRef) {
      socketRef.close(1000, 'User disconnect');
    }

    setSocketStatus(SOCKET_STATUS.DISCONNECTED);
    setGlobalSocketStatus(SOCKET_STATUS.DISCONNECTED);
    setIsStreaming(false);
    setIsSocketActive(false);
    setSocketRef(null, null);
  }, [clearReconnectTimer, socketRef]);

  // disconnect but keep session
  const disconnectButKeepSession = useCallback(() => {
    console.log('disconnect global socket but keep session:', sessionId);
    clearReconnectTimer();

    if (socketRef) {
      socketRef.close(1000, 'Keep session');
    }

    setSocketStatus(SOCKET_STATUS.DISCONNECTED);
    setGlobalSocketStatus(SOCKET_STATUS.DISCONNECTED);
    setIsStreaming(false);
    setSocketRef(null, null);
    // do not clear isSocketActive, because there may be unfinished data
  }, [clearReconnectTimer, sessionId, socketRef]);

  // reset session
  const resetSession = useCallback(() => {
    console.log('reset global socket session');
    disconnect();
    setSessionId(null);
    setCurrentRequestId(null);
    sessionStorage.removeItem('chat_session_id');
    setSocketWaiting(false);
  }, [disconnect]);

  // save state before leave
  const saveStateBeforeLeave = useCallback(() => {
    const currentState = useChatStore.getState();
    if (currentState.messages.length > 0 || currentState.isAnswering) {
      saveTempChatState(currentState);
      console.log('save chat state to temporary storage');
    }
  }, []);

  // clean up when component unmount
  useEffect(() => {
    const handleBeforeUnload = () => {
      saveStateBeforeLeave();
    };

    window.addEventListener('beforeunload', handleBeforeUnload);

    return () => {
      window.removeEventListener('beforeunload', handleBeforeUnload);
      clearReconnectTimer();
      clearSocketWaitingTimer();
    };
  }, [clearReconnectTimer, saveStateBeforeLeave]);

  // global new chat
  const globalNewChat = useCallback(() => {
    console.log('global socket: execute new chat operation');
    const { globalNewChat: storeGlobalNewChat } = useChatStore.getState();
    const result = storeGlobalNewChat();

    if (result.needResetSocket) {
      resetSession();
    }

    return result;
  }, [resetSession]);

  const globalCloseMessage = useCallback(() => {
    console.log('global socket: execute stop message receive operation');
    const { globalCloseMessage: storeGlobalCloseMessage } = useChatStore.getState();
    const result = storeGlobalCloseMessage();

    if (result.needDisconnectSocket) {
      disconnectButKeepSession();
    }

    return result;
  }, [disconnectButKeepSession]);

  // save message to history
  const saveMessageToHistory = useCallback((messageData) => {
    // in global version, directly call global message processing logic
    if (handleGlobalSocketMessage) {
      handleGlobalSocketMessage(messageData);
    } else {
      console.log('global message processing logic not exist');
    }
  }, [handleGlobalSocketMessage]);


  return {
    isConnected: socketRef?.readyState === WebSocket.OPEN,
    isConnecting: socketRef?.readyState === WebSocket.CONNECTING,

    // socket operation
    sendMessage,
    sendMessageDirect,
    sendDirectMessage: sendMessageDirect,
    disconnect,
    disconnectButKeepSession,
    resetSession,

    // global chat operation
    globalNewChat,
    globalCloseMessage,

    // message processing
    saveMessageToHistory,

    // state management
    saveStateBeforeLeave,

    // socket reference (for debugging only)
    socketRef: socketRef
  };
};
