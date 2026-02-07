/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import { useState, useEffect, useCallback } from 'react';
import { message } from 'antd';
import { useTranslation } from 'react-i18next';
import {
  getRtspCameraList,
  createRtspCamera,
  updateRtspCamera,
  deleteRtspCamera,
  checkRtspCameraStatus,
} from '@/api';

/**
 * useCameras Hook - RTSP摄像头管理Hook
 * 处理摄像头的CRUD操作
 *
 * @returns {Object} 摄像头管理相关的状态和方法
 */
const useCameras = () => {
  const { t } = useTranslation();
  const [cameras, setCameras] = useState([]);
  const [loading, setLoading] = useState(false);

  // 获取摄像头列表
  const fetchCameras = useCallback(async () => {
    try {
      setLoading(true);
      const res = await getRtspCameraList();
      if (res.code === 0) {
        setCameras(res.data || []);
      } else {
        message.error(t('cameraManage.fetchCamerasFailed'));
      }
    } catch (error) {
      console.error('获取摄像头列表失败:', error);
      message.error(t('cameraManage.fetchCamerasFailed'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  // 初始化加载
  useEffect(() => {
    fetchCameras();
  }, [fetchCameras]);

  // 启用/禁用摄像头
  const handleSwitch = useCallback(async (cameraId, enabled) => {
    try {
      const res = await updateRtspCamera(cameraId, { enabled });
      if (res.code === 0) {
        message.success(enabled ? t('cameraManage.cameraEnabled') : t('cameraManage.cameraDisabled'));
        fetchCameras();
      } else {
        message.error(t('cameraManage.updateFailed'));
      }
    } catch (error) {
      console.error('更新摄像头状态失败:', error);
      message.error(t('cameraManage.updateFailed'));
    }
  }, [t, fetchCameras]);

  // 删除摄像头
  const handleDelete = useCallback(async (cameraId) => {
    try {
      const res = await deleteRtspCamera(cameraId);
      if (res.code === 0) {
        message.success(t('common.deleteSuccess'));
        fetchCameras();
      } else {
        message.error(t('common.deleteFail'));
      }
    } catch (error) {
      console.error('删除摄像头失败:', error);
      message.error(t('common.deleteFail'));
    }
  }, [t, fetchCameras]);

  // 创建摄像头
  const createCamera = useCallback(async (data) => {
    try {
      const res = await createRtspCamera(data);
      if (res.code === 0) {
        message.success(t('common.addSuccess'));
        fetchCameras();
        return { success: true };
      } else {
        message.error(t('common.addFail'));
        return { success: false };
      }
    } catch (error) {
      console.error('创建摄像头失败:', error);
      message.error(t('common.addFail'));
      return { success: false };
    }
  }, [t, fetchCameras]);

  // 更新摄像头
  const updateCamera = useCallback(async (cameraId, data) => {
    try {
      const res = await updateRtspCamera(cameraId, data);
      if (res.code === 0) {
        message.success(t('common.editSuccess'));
        fetchCameras();
        return { success: true };
      } else {
        message.error(t('common.editFail'));
        return { success: false };
      }
    } catch (error) {
      console.error('更新摄像头失败:', error);
      message.error(t('common.editFail'));
      return { success: false };
    }
  }, [t, fetchCameras]);

  // 刷新摄像头状态
  const refreshStatus = useCallback(async (cameraId) => {
    try {
      const res = await checkRtspCameraStatus(cameraId);
      if (res.code === 0) {
        message.success(t('cameraManage.statusRefreshed'));
        fetchCameras();
      } else {
        message.error(t('cameraManage.statusRefreshFailed'));
      }
    } catch (error) {
      console.error('刷新摄像头状态失败:', error);
      message.error(t('cameraManage.statusRefreshFailed'));
    }
  }, [t, fetchCameras]);

  return {
    cameras,
    loading,
    handleSwitch,
    handleDelete,
    createCamera,
    updateCamera,
    refreshStatus,
    fetchCameras,
  };
};

export default useCameras;
