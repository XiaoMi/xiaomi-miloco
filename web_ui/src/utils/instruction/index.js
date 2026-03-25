/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import { MESSAGE_NAMESPACE, MESSAGE_NLP_NAME, MESSAGE_TYPES } from '@/constants/messageTypes';
import { getMessageIsException, getMessageIsFinishChat, getMessageIsRequest, getMessageIsSaveRuleConfirm, getMessageIsSaveRuleConfirmResult, getMessageIsToastStream } from './typeUtils';

// process socket messages in history record
export const processHistorySocketMessages = (session) => {
  if (!session || !Array.isArray(session)) {
    console.warn('history record data format is incorrect:', session);
    return [];
  }

  const messages = [];
  let currentQuestion = null;
  let currentAnswer = null;
  let currentAnswerMessages = [];
  let sessionId = null;

  let latestCameraIds = [];
  let latestMcpList = [];

  session.forEach((messageData) => {
    const { header, payload } = messageData;
    const { type, namespace, name } = header;
    if (header.session_id && !sessionId) {
      sessionId = header.session_id;
    }
    try {
      if (getMessageIsRequest(type, namespace, name)) {
        if (currentQuestion && currentAnswer) {
          messages.push(currentQuestion);
          messages.push({
            ...currentAnswer,
            socketMessages: currentAnswerMessages
          });
        }
        const requestData = JSON.parse(payload);
        const { query, mcp_list, camera_ids, chat_mode } = requestData;

        if (camera_ids !== undefined) {latestCameraIds = Array.isArray(camera_ids) ? camera_ids : [];}
        if (mcp_list !== undefined) {latestMcpList = Array.isArray(mcp_list) ? mcp_list : [];}


        currentQuestion = {
          type: 'question',
          text: query,
          mcpList: mcp_list || [],
          timestamp: header.timestamp,
          requestId: header.request_id,
          sessionId: header.session_id
        };

        // reset current answer state
        currentAnswer = null;
        currentAnswerMessages = [];

      } else if (getMessageIsToastStream(type, namespace, name)) {
        if (!currentQuestion) {
          console.warn('received instruction message but no corresponding user question');
          return;
        }
        let lastToastStreamIndex = -1;
        if (currentAnswerMessages.length > 0) {
          const lastMsg = currentAnswerMessages[currentAnswerMessages.length - 1];
          const { type, namespace, name } = lastMsg.header;
          if (getMessageIsToastStream(type, namespace, name)) {
            lastToastStreamIndex = currentAnswerMessages.length - 1;
          } else {
          }
        }

        if (lastToastStreamIndex >= 0) {
          const existingMsg = currentAnswerMessages[lastToastStreamIndex];
          const existingPayload = JSON.parse(existingMsg.payload);
          const newPayload = JSON.parse(payload);

          const accumulatedPayload = {
            ...existingPayload,
            stream: (existingPayload.stream || '') + (newPayload.stream || '')
          };

          currentAnswerMessages[lastToastStreamIndex] = {
            ...existingMsg,
            payload: JSON.stringify(accumulatedPayload),
            isAccumulating: true,
            lastUpdated: Date.now()
          };
        } else {
          currentAnswerMessages.push({
            ...messageData,
            isAccumulating: true,
            lastUpdated: Date.now()
          });
        }

        // create or update current answer state
        if (!currentAnswer) {
          currentAnswer = {
            type: 'answer',
            socketMessages: []
          };
        }
      } else if (getMessageIsSaveRuleConfirm(type, namespace, name)) {
        // ensure there is a current question
        if (!currentQuestion) {
          return;
        }
        currentAnswerMessages.push(messageData);
        if (!currentAnswer) {
          currentAnswer = {
            type: 'answer',
            socketMessages: []
          };
        }
      } else if (getMessageIsSaveRuleConfirmResult(type, namespace, name)) {
        if (!currentQuestion) {
          return;
        }
        let cameraOptionsFromConfirm = null;
        currentAnswerMessages.forEach(msg => {
          const { type, namespace, name } = msg.header;
          if (getMessageIsSaveRuleConfirm(type, namespace, name)) {
            try {
              const payload = JSON.parse(msg.payload);
              if (payload.camera_options) {
                cameraOptionsFromConfirm = payload.camera_options;
              }
            } catch (error) {
              console.warn('parse history confirm message payload failed:', error);
            }
          }
        });

        const filteredMessages = currentAnswerMessages.filter(msg => {
          const { type, namespace, name } = msg.header;
          const shouldRemove = getMessageIsSaveRuleConfirm(type, namespace, name);
          if (shouldRemove) {
          }
          return !shouldRemove;
        });

        if (cameraOptionsFromConfirm) {
          try {
            const currentPayload = JSON.parse(messageData.payload);
            const mergedPayload = {
              ...currentPayload,
              camera_options: cameraOptionsFromConfirm
            };
            messageData = {
              ...messageData,
              payload: JSON.stringify(mergedPayload)
            };
          } catch (error) {
            console.warn('merge camera options failed:', error);
          }
        }
        currentAnswerMessages = [...filteredMessages, messageData];
        if (!currentAnswer) {
          currentAnswer = {
            type: 'answer',
            socketMessages: []
          };
        }
      } else if (getMessageIsException(type, namespace, name)) {
        currentAnswerMessages.push(messageData);
        if (!currentAnswer) {
          currentAnswer = {
            type: 'answer',
            socketMessages: []
          };
        }
      } else if (getMessageIsFinishChat(type, namespace, name)) {
        currentAnswerMessages.push(messageData);
        if (!currentAnswer) {
          currentAnswer = {
            type: 'answer',
            socketMessages: []
          };
        }
        // dialog finished, mark current answer as complete state
        if (currentAnswer) {
          currentAnswer.isComplete = true;
          currentAnswer.isBuilding = false;
          let success = true;
          try {
            const finishData = JSON.parse(messageData.payload);
            success = finishData.success !== false;
          } catch (error) {
            console.warn('parse history finish message failed:', error);
          }
          currentAnswer.success = success;
        }
      } else {
        currentAnswerMessages.push(messageData);
        if (!currentAnswer) {
          currentAnswer = {
            type: 'answer',
            socketMessages: []
          };
        }
      }
    } catch (error) {
      console.error('process history socket message failed:', error);
    }
  });
  if (currentQuestion && currentAnswer) {
    messages.push(currentQuestion);
    messages.push({
      ...currentAnswer,
      socketMessages: currentAnswerMessages
    });
  } else if (currentQuestion && !currentAnswer) {
    messages.push(currentQuestion);
  }

  return {
    messages,
    sessionId,
    latestConfig: {
      cameraIds: latestCameraIds,
      mcpList: latestMcpList,
    }
  };
};

// generate request ID
export const generateRequestId = () => {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
    const r = Math.random() * 16 | 0;
    const v = c == 'x' ? r : (r & 0x3 | 0x8);
    return v.toString(16);
  });
};

// create send message format
export const createRequestMessage = (requestId, sessionId = null, data = {}) => {
  return {
    header: {
      type: MESSAGE_TYPES.EVENT,
      namespace: MESSAGE_NAMESPACE.NLP,
      name: MESSAGE_NLP_NAME.REQUEST,
      timestamp: Math.floor(Date.now() / 1000),
      request_id: requestId,
      session_id: sessionId
    },
    payload: JSON.stringify({
      ...data
    })
  };
};
