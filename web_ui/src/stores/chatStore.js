/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import { create } from 'zustand'
import { persist, subscribeWithSelector } from 'zustand/middleware'
import { getMCPStatus, getCameraList, refreshMiotCamera, getHistoryList, getHistoryDetail, reconnectMCPService, deleteChatHistory, getXiaoAISpeakers, speakToXiaoAI } from '@/api'
import { message } from 'antd'
import { processHistorySocketMessages } from '@/utils/instruction'

// chat state management store
export const useChatStore = create(
  subscribeWithSelector(
    persist(
      (set, get) => ({
        // === message related state ===
        messages: [],
        currentAnswer: null,
        answerMessages: [],
        isAnswering: false,
        isHistoryMode: false,
        socketWaiting: false,

        // === session related state ===
        sessionId: null,
        currentRequestId: null,
        historyItem: null,

        // === configuration related state ===
        cameraList: [],
        selectedCameraIds: [],
        mcpList: [],
        input: '',

        // === UI state ===
        isScrollToBottom: true,
        welcomeMessages: false,
        // MCP selector UI state
        mcpVisible: false,
        availableMcpServices: [],
        mcpLoading: false,
        // camera selector UI state
        cameraVisible: false,
        // history related state
        historyList: [],
        historyLoading: false,
        // refresh state
        isRefreshing: false,
        // XiaoAI speaker state
        speakerVisible: false,
        speakerList: [],
        speakerLoading: false,
        selectedSpeakerId: null, // selected speaker ID for playback

        // === Socket related state ===
        socketStatus: 'DISCONNECTED',
        isSocketActive: false,

        // === Actions - message related ===
        setMessages: (messages) => set({ messages: Array.isArray(messages) ? messages : [] }),
        addMessage: (message) => set((state) => ({
          messages: [...(Array.isArray(state.messages) ? state.messages : []), message]
        })),
        updateLastMessage: (updater) => set((state) => {
          const messages = [...(Array.isArray(state.messages) ? state.messages : [])];
          if (messages.length > 0) {
            const lastIndex = messages.length - 1;
            messages[lastIndex] = typeof updater === 'function'
              ? updater(messages[lastIndex])
              : updater;
          }
          return { messages };
        }),
        clearMessages: () => set({
          messages: [],
          currentAnswer: null,
          answerMessages: []
        }),

        setCurrentAnswer: (answer) => set({ currentAnswer: answer }),
        setAnswerMessages: (messagesOrUpdater) => set((state) => {
          // support two calling ways: directly pass array or pass update function
          if (typeof messagesOrUpdater === 'function') {
            // functional update: setAnswerMessages(prev => {...})
            const currentMessages = Array.isArray(state.answerMessages) ? state.answerMessages : [];
            const newMessages = messagesOrUpdater(currentMessages);
            return { answerMessages: Array.isArray(newMessages) ? newMessages : [] };
          } else {
            // directly update: setAnswerMessages([...])
            return { answerMessages: Array.isArray(messagesOrUpdater) ? messagesOrUpdater : [] };
          }
        }),
        addAnswerMessage: (message) => set((state) => ({
          answerMessages: [...(Array.isArray(state.answerMessages) ? state.answerMessages : []), message]
        })),

        setIsAnswering: (isAnswering) => set({ isAnswering }),
        setIsHistoryMode: (isHistoryMode) => set({ isHistoryMode }),

        // === Actions - session related ===
        setSessionId: (sessionId) => set({ sessionId }),
        setCurrentRequestId: (requestId) => set({ currentRequestId: requestId }),
        setHistoryItem: (item) => set({ historyItem: item }),

        // === Actions - configuration related ===
        setCameraList: (list) => {
          const sortedList = Array.isArray(list) ? list.sort((a, b) => {
            if (a.online !== b.online) {
              return b.online - a.online;
            }
            return (b.order_time || 0) - (a.order_time || 0);
          }) : [];
          set({ cameraList: sortedList });
        },
        setSelectedCameraIds: (ids) => set({ selectedCameraIds: ids }),
        toggleCamera: (id) => set((state) => {
          const isSelected = state.selectedCameraIds.includes(id);
          return {
            selectedCameraIds: isSelected
              ? state.selectedCameraIds.filter(cameraId => cameraId !== id)
              : [...state.selectedCameraIds, id]
          };
        }),

        // camera select handler function
        handleCameraSelect: (did) => {
          const { toggleCamera } = get();
          toggleCamera(did);
        },

        handleCameraSelectAll: (checked) => {
          const { cameraList, setSelectedCameraIds } = get();
          if (checked) {
            const onlineCameraList = cameraList.filter(item => item.online);
            setSelectedCameraIds(onlineCameraList.map(item => item.did));
          } else {
            setSelectedCameraIds([]);
          }
        },

        setMcpList: (list) => set({ mcpList: list }),
        toggleMcpService: (serviceId) => set((state) => {
          const isSelected = state.mcpList.includes(serviceId);
          return {
            mcpList: isSelected
              ? state.mcpList.filter(id => id !== serviceId)
              : [...state.mcpList, serviceId]
          };
        }),

        // MCP select all handler function
        handleMcpSelectAll: (checked) => {
          const { availableMcpServices, setMcpList } = get();
          if (checked) {
            const allServiceIds = availableMcpServices.map(service => service.client_id);
            setMcpList(allServiceIds);
          } else {
            setMcpList([]);
          }
        },

        setSocketWaiting: (waiting) => set({ socketWaiting: waiting }),

        setInput: (input) => set({ input }),

        // === Actions - UI state ===
        setIsScrollToBottom: (isScrollToBottom) => set({ isScrollToBottom }),
        setWelcomeMessages: (welcomeMessages) => set({ welcomeMessages }),

        // MCP UI state management
        setMcpVisible: (visible) => set({ mcpVisible: visible }),
        toggleMcpVisible: () => set((state) => ({ mcpVisible: !state.mcpVisible })),
        setAvailableMcpServices: (services) => set({ availableMcpServices: Array.isArray(services) ? services : [] }),
        setMcpLoading: (loading) => set({ mcpLoading: loading }),

        // camera UI state management
        setCameraVisible: (visible) => set({ cameraVisible: visible }),
        toggleCameraVisible: () => set((state) => ({ cameraVisible: !state.cameraVisible })),

        // XiaoAI speaker UI state management
        setSpeakerVisible: (visible) => set({ speakerVisible: visible }),
        toggleSpeakerVisible: () => set((state) => ({ speakerVisible: !state.speakerVisible })),
        setSpeakerList: (list) => set({ speakerList: Array.isArray(list) ? list : [] }),
        setSpeakerLoading: (loading) => set({ speakerLoading: loading }),
        setSelectedSpeakerId: (speakerId) => set({ selectedSpeakerId: speakerId }),

        // XiaoAI speaker fetch
        fetchSpeakerList: async () => {
          try {
            set({ speakerLoading: true });
            const response = await getXiaoAISpeakers();
            if (response && response.code === 0) {
              const speakers = response?.data || [];
              set({ speakerList: Array.isArray(speakers) ? speakers : [] });
            } else {
              set({ speakerList: [] });
            }
          } catch (error) {
            console.error('fetch speaker list failed:', error);
            set({ speakerList: [] });
          } finally {
            set({ speakerLoading: false });
          }
        },

        // speak text to selected speaker
        // filter_for_tts: 根据音箱TTS配置过滤内容（思考过程、工具调用等）
        speakToSelectedSpeaker: async (text, filterForTts = true) => {
          const { selectedSpeakerId } = get();
          if (!selectedSpeakerId || !text) {
            return false;
          }
          try {
            const response = await speakToXiaoAI({
              speaker_id: selectedSpeakerId,
              text: text,
              blocking: false,
              filter_for_tts: filterForTts,
            });
            return response?.code === 0;
          } catch (error) {
            console.error('speak to speaker failed:', error);
            return false;
          }
        },

        // history related state management
        setHistoryList: (list) => set({ historyList: Array.isArray(list) ? list : [] }),
        setHistoryLoading: (loading) => set({ historyLoading: loading }),

        // refresh state management
        setIsRefreshing: (loading) => set({ isRefreshing: loading }),

        // === Actions - MCP service get ===
        fetchMcpServices: async () => {
          try {
            set({ mcpLoading: true });
            const response = await getMCPStatus();
            if (response && response.code === 0) {
              const services = response?.data?.clients || [];
              set({ availableMcpServices: Array.isArray(services) ? services : [] });
            } else {
              set({ availableMcpServices: [] });
            }
          } catch (error) {
            console.error('fetch MCP service failed:', error);
            set({ availableMcpServices: [] });
          } finally {
            set({ mcpLoading: false });
          }
        },

        handleMcpReconnect: async (serviceId) => {
          console.log('reconnect MCP service:', serviceId);
          const response = await reconnectMCPService(serviceId);
          if (response?.code === 0) {
            message.success(response?.message || 'reconnect MCP service success');
            await get().fetchMcpServices();
          } else {
            message.error('reconnect MCP service failed');
          }
        },

        // === Actions - camera management ===
        fetchCameraList: async () => {
          try {
            const response = await getCameraList();
            const { code, data } = response || {};
            const list = data || [];

            if (code === 0) {
              const { setCameraList } = get();
              setCameraList(list);
            } else {
              set({ cameraList: [] });
              message.error('fetch camera list failed');
            }
          } catch (error) {
            set({ cameraList: [] });
            message.error('fetch camera list failed');
          }
        },

        refreshMiotInfo: async () => {
          try {
            set({ isRefreshing: true });
            const response = await refreshMiotCamera();
            const { code } = response || {};

            if (code === 0) {
              const state = get();
              await state.fetchCameraList();
              message.success('refresh device info success');
            } else {
              message.error('refresh device list failed');
            }
          } catch (error) {
            message.error('refresh device list failed');
          } finally {
            set({ isRefreshing: false });
          }
        },

        // === Actions - history record get ===
        fetchHistoryList: async () => {
          try {
            set({ historyLoading: true });
            const response = await getHistoryList();
            const { code, data } = response || {};

            if (code === 0) {
              set({ historyList: Array.isArray(data) ? data : [] });
            } else {
              set({ historyList: [] });
              message.error('fetch history list failed');
            }
          } catch (error) {
            set({ historyList: [] });
            message.error('fetch history list failed');
          } finally {
            set({ historyLoading: false });
          }
        },

        handleHistoryClick: async (sessionId) => {
          const { historyList } = get();
          const selectedHistory = historyList.find(item => item.session_id === sessionId);
          console.log('click history record:', sessionId, selectedHistory);

          if (!selectedHistory) {
            console.error('no corresponding history record');
            return;
          }

          try {
            // get history record detail
            const response = await getHistoryDetail(sessionId);
            const { code, data } = response || {};

            if (code !== 0) {
              message.error('fetch history detail failed');
              return;
            }

            const { session = {} } = data || {};
            const { data: sessionData } = session;

            if (!sessionData) {
              message.error('history record data is empty');
              return;
            }

            console.log('start loading history record:', session);

            if (sessionData && Array.isArray(sessionData)) {
              const { messages, sessionId: historySessionId, latestConfig } = processHistorySocketMessages(sessionData);

              console.log('processed history messages:', messages);
              console.log('history record configuration:', latestConfig);

              set({
                messages: messages || [],
                currentAnswer: null,
                answerMessages: [],
                historyItem: selectedHistory,
                sessionId: historySessionId || sessionId,
                isHistoryMode: false,
                isAnswering: false,
                isScrollToBottom: true,
                selectedCameraIds: latestConfig?.cameraIds || [],
                mcpList: latestConfig?.mcpList || [],
              });
              console.log('✅ sessionId:', historySessionId || sessionId);
            } else {
              message.error('history record format not supported');
            }
          } catch (error) {
            message.error('load history record failed');
          }
        },

        // === Actions - delete history record ===
        deleteHistoryRecord: async (sessionId) => {
          try {
            const response = await deleteChatHistory(sessionId);
            const { code, message: responseMessage } = response || {};

            if (code === 0) {
              const { historyList, setHistoryList } = get();
              const updatedList = historyList.filter(item => item.session_id !== sessionId);
              setHistoryList(updatedList);
              message.success('delete history record success');
            } else {
              console.error('delete history record failed:', response);
              message.error(responseMessage || 'delete history record failed');
            }
          } catch (error) {
            message.error('delete history record failed');
          }
        },

        // === Actions - Socket related ===
        setSocketStatus: (status) => set({ socketStatus: status }),
        setIsSocketActive: (active) => set({ isSocketActive: active }),

        globalSendMessage: async (messageText, customMcpList = null, callbacks = {}) => {
          const {
            input, isAnswering, selectedCameraIds, mcpList,
            setIsScrollToBottom, setCurrentAnswer, setAnswerMessages, startNewRound, setMcpList
          } = get();

          if (isAnswering) {return null;}

          const inputText = messageText || input.trim();
          if (!inputText) {
            message.warning('please input your question');
            return null;
          }

          console.log('global send message:', {
            text: inputText,
            selectedCameraIds,
            customMcpList,
            currentMcpList: mcpList,
          });

          startNewRound(inputText);
          setIsScrollToBottom(true);
          setCurrentAnswer(null);
          setAnswerMessages([]);

          let finalMcpList;
          // if there is a custom MCP list, use it and sync to global state
          if (customMcpList !== null) {
            finalMcpList = Array.isArray(customMcpList) ? customMcpList : [];
            setMcpList(finalMcpList);
          } else {
            finalMcpList = Array.isArray(mcpList) ? mcpList : [];
          }

          // build message data
          const messageData = {
            query: inputText,
            camera_ids: selectedCameraIds,
            mcp_list: finalMcpList,
          };

          // execute send before callback
          if (callbacks.onBeforeSend) {
            callbacks.onBeforeSend();
          }
          return messageData;
        },

        // === Composite Actions ===
        newChat: () => set({
          messages: [],
          currentAnswer: null,
          answerMessages: [],
          sessionId: null,
          currentRequestId: null,
          historyItem: null,
          isHistoryMode: false,
          isAnswering: false,
          input: '',
          isScrollToBottom: true,
          welcomeMessages: false
        }),

        loadHistory: (historyData, processedMessages) => set({
          messages: processedMessages || [],
          currentAnswer: null,
          answerMessages: [],
          historyItem: historyData,
          isHistoryMode: true,
          isAnswering: false,
          isScrollToBottom: true
        }),

        resetSession: () => set((state) => ({
          messages: [],
          currentAnswer: null,
          answerMessages: [],
          sessionId: null,
          currentRequestId: null,
          historyItem: null,
          isHistoryMode: false,
          isAnswering: false,
          input: '',
          isScrollToBottom: true,
          selectedCameraIds: state.selectedCameraIds,
          mcpList: state.mcpList,
        })),

        startNewRound: (question) => set((state) => ({
          messages: [...(Array.isArray(state.messages) ? state.messages : []), { type: 'question', text: question }],
          currentAnswer: null,
          answerMessages: [],
          isAnswering: true,
          input: '',
          isScrollToBottom: true,
          isHistoryMode: false
        })),

        // global chat operation method
        globalNewChat: () => {
          console.log('execute global new chat operation');
          // directly use set to execute newChat logic
          set({
            messages: [],
            currentAnswer: null,
            answerMessages: [],
            sessionId: null,
            currentRequestId: null,
            historyItem: null,
            isHistoryMode: false,
            isAnswering: false,
            input: '',
            isScrollToBottom: true,
            welcomeMessages: false
          });
          return { needResetSocket: true };
        },

        globalCloseMessage: () => {
          console.log('execute global stop message receive');
          set({
            currentAnswer: null,
            answerMessages: [],
            isAnswering: false,
            isSocketActive: false
          });
          return { needDisconnectSocket: true };
        }
      }),
      {
        name: 'chat-store',
        partialize: (state) => ({
          availableMcpServices: state.availableMcpServices,
        })
      }
    )
  )
)

// temporary session storage (for keeping message state when routing changes)
export const useSessionChatStore = create((set, get) => ({
  tempMessages: [],
  tempCurrentAnswer: null,
  tempAnswerMessages: [],
  tempIsAnswering: false,
  tempSessionId: null,

  saveTempChatState: (state) => set({
    tempMessages: Array.isArray(state.messages) ? state.messages : [],
    tempCurrentAnswer: state.currentAnswer,
    tempAnswerMessages: Array.isArray(state.answerMessages) ? state.answerMessages : [],
    tempIsAnswering: state.isAnswering,
    tempSessionId: state.sessionId
  }),

  restoreTempChatState: () => {
    const tempState = get();
    return {
      messages: Array.isArray(tempState.tempMessages) ? tempState.tempMessages : [],
      currentAnswer: tempState.tempCurrentAnswer,
      answerMessages: Array.isArray(tempState.tempAnswerMessages) ? tempState.tempAnswerMessages : [],
      isAnswering: tempState.tempIsAnswering,
      sessionId: tempState.tempSessionId
    };
  },

  clearTempChatState: () => set({
    tempMessages: [],
    tempCurrentAnswer: null,
    tempAnswerMessages: [],
    tempIsAnswering: false,
    tempSessionId: null
  }),

  hasTempChatState: () => {
    const state = get();
    return (Array.isArray(state.tempMessages) ? state.tempMessages.length > 0 : false) || state.tempIsAnswering;
  }
}))


export const useSocketStore = create((set, get) => ({
  socketRef: null,
  socketActions: null,
  status: 'DISCONNECTED',
  isStreaming: false,

  setSocketRef: (socketRef, actions) => set({
    socketRef,
    socketActions: actions
  }),

  setSocketStatus: (status) => set({ status }),
  setIsStreaming: (isStreaming) => set({ isStreaming }),

  getSocketActions: () => get().socketActions,

  isSocketReady: () => {
    const { socketRef, status } = get();
    return socketRef && status === 'CONNECTED';
  },

  clearSocket: () => set({
    socketRef: null,
    socketActions: null,
    status: 'DISCONNECTED',
    isStreaming: false
  })
}))
