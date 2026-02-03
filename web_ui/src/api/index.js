/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import { getApi, postApi, putApi, deleteApi } from "@/utils/http";

// auth API
export const getJudgeLogin = () => getApi('/api/auth/register-status');
export const getUserLoginOut = () => getApi('/api/auth/logout');
export const setInitPinCode = (data) => postApi('/api/auth/register', data);
export const getPinLogin = (data) => postApi('/api/auth/login', data);
export const setLanguage = (data) => postApi('/api/auth/language', data);
export const getLanguage = () => getApi('/api/auth/language');

// miot API
export const getUserLoginStatus = () => getApi('/api/miot/login_status');
export const getUserInfo = () => getApi('/api/miot/user_info');
export const getCameraList = () => getApi('/api/miot/camera_list');
export const getDeviceList = () => getApi('/api/miot/device_list');
export const getScenesList = () => getApi('/api/miot/scenes');
export const getRefreshMiotInfo = () => getApi('/api/miot/refresh_miot_info');
export const getMiotSceneActions = () => getApi('/api/miot/miot_scene_actions');
export const sendNotification = (data) => getApi(`/api/miot/send_notify?notify=${data}`);
export const refreshMiotDevices = () => getApi('/api/miot/refresh_miot_devices');
export const refreshMiotScenes = () => getApi('/api/miot/refresh_miot_scenes');
export const refreshMiotCamera = () => getApi('/api/miot/refresh_miot_cameras');
export const getRefreshMiotAllInfo = () => getApi('/api/miot/refresh_miot_all_info');

// trigger API
export const saveSmartRule = (data) => postApi('/api/trigger/rule', data);
export const updateSmartRule = (ruleId, data) => putApi(`/api/trigger/rule/${ruleId}`, data);
export const deleteSmartRule = (id) => deleteApi(`/api/trigger/rule/${id}`);

export const getSmartRules = () => getApi('/api/trigger/rules');
export const executeSceneActions = (data) => postApi('/api/trigger/execute_actions', data);
export const getRuleTriggerLogs = (limit = 500) => getApi(`/api/trigger/logs?limit=${limit}`);

// model API
export const getAllModels = () => getApi('/api/model');
export const createModel = (data) => postApi('/api/model', data);
export const getModelDetail = (modelId) => getApi(`/api/model/${modelId}`);
export const updateModel = (modelId, data) => putApi(`/api/model/${modelId}`, data);
export const deleteModel = (modelId) => deleteApi(`/api/model/${modelId}`);
export const getVendorModels = (data) => postApi('/api/model/get_vendor_models', data);
export const setCurrentModel = (modelId, purpose = '') => getApi(`/api/model/set_current_model?${purpose ? `purpose=${purpose}` : ''}${modelId ? `&model_id=${modelId}` : ''}`);
export const getModelPurposes = () => getApi('/api/model/model_purposes');
export const getCudaInfo = () => getApi('/api/model/get_cuda_info');
export const setModelLoad = (data) => postApi('/api/model/load', data, 60000);
// Home Assistant API
export const setHAAuth = (data) => postApi('/api/ha/set_config', data);
export const getHAAuth = () => getApi('/api/ha/get_config');
export const getHaList = () => getApi('/api/ha/automations');
export const getHaAutomationActions = () => getApi('/api/ha/automation_actions');
export const refreshHaAutomation = () => getApi('/api/ha/refresh_ha_automations');
// Home Assistant WebSocket API
export const getHAWsStatus = () => getApi('/api/ha/ws_status');
export const getHADevices = () => getApi('/api/ha/devices');
export const getHAAreas = () => getApi('/api/ha/areas');
export const getHADeviceEntities = (deviceId) => getApi(`/api/ha/device/${deviceId}/entities`);
export const getHAStates = () => getApi('/api/ha/states');
export const getHAEntityRegistry = () => getApi('/api/ha/entity_registry');

// mcp
export const getMCPService = () => getApi('/api/mcp');
export const setMCPService = (data) => postApi('/api/mcp', data);
export const updateMCPService = (id, data) => putApi(`/api/mcp/${id}`, data);
export const deleteMCPService = (id) => deleteApi(`/api/mcp/${id}`);
export const getMCPStatus = () => getApi('/api/mcp/clients/status');
export const reconnectMCPService = (id) => postApi(`/api/mcp/reconnect/${id}`);

// history API
export const getHistoryList = () => getApi('/api/chat/historys');
export const getHistoryDetail = (id) => getApi(`/api/chat/history/${id}`);
export const deleteChatHistory = (id) => deleteApi(`/api/chat/history/${id}`);

// RTSP摄像头 API
export const getRtspCameraList = (enabledOnly = false) => getApi(`/api/rtsp_camera?enabled_only=${enabledOnly}`);
export const createRtspCamera = (data) => postApi('/api/rtsp_camera', data);
export const getRtspCamera = (cameraId) => getApi(`/api/rtsp_camera/${cameraId}`);
export const updateRtspCamera = (cameraId, data) => putApi(`/api/rtsp_camera/${cameraId}`, data);
export const deleteRtspCamera = (cameraId) => deleteApi(`/api/rtsp_camera/${cameraId}`);
export const checkRtspCameraStatus = (cameraId) => getApi(`/api/rtsp_camera/${cameraId}/status`);
export const refreshRtspCameraStatus = () => postApi('/api/rtsp_camera/refresh_status');

// Memory API 记忆管理
export const getMemoryList = (params = {}) => {
  const { userId = 'default', includeInactive = false, page = 1, pageSize = 20 } = params;
  return getApi(`/api/memory/list?user_id=${userId}&include_inactive=${includeInactive}&page=${page}&page_size=${pageSize}`);
};
export const addMemory = (data, userId = 'default') => postApi(`/api/memory/add?user_id=${userId}`, data);
export const updateMemory = (memoryId, data) => putApi(`/api/memory/${memoryId}`, data);
export const deleteMemory = (memoryId, softDelete = true) => deleteApi(`/api/memory/${memoryId}?soft_delete=${softDelete}`);
export const searchMemory = (data, userId = 'default') => postApi(`/api/memory/search?user_id=${userId}`, data);
export const handleMemoryCommand = (data, userId = 'default') => postApi(`/api/memory/command?user_id=${userId}`, data);
export const getMemoryStats = (userId = 'default') => getApi(`/api/memory/stats?user_id=${userId}`);
export const getMemoryContext = (query, userId = 'default', limit = 5) => getApi(`/api/memory/context?query=${encodeURIComponent(query)}&user_id=${userId}&limit=${limit}`);
export const getMemoryTypes = () => getApi('/api/memory/types');
