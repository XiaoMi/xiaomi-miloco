/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import { useState, useCallback } from 'react';
import { Form } from 'antd';

/**
 * useCameraForm Hook - RTSP摄像头表单管理Hook
 * 处理表单的打开、关闭和状态管理
 *
 * @returns {Object} 表单管理相关的状态和方法
 */
const useCameraForm = () => {
  const [form] = Form.useForm();
  const [modalOpen, setModalOpen] = useState(false);
  const [editId, setEditId] = useState(null);
  const [submitLoading, setSubmitLoading] = useState(false);

  // 打开新增表单
  const openAddForm = useCallback(() => {
    form.resetFields();
    setEditId(null);
    setModalOpen(true);
  }, [form]);

  // 打开编辑表单
  const openEditForm = useCallback((camera) => {
    form.setFieldsValue({
      name: camera.name,
      location: camera.location,
      rtsp_url_main: camera.rtsp_url_main,
      rtsp_url_sub: camera.rtsp_url_sub,
      enabled: camera.enabled,
    });
    setEditId(camera.id);
    setModalOpen(true);
  }, [form]);

  // 关闭表单
  const closeForm = useCallback(() => {
    form.resetFields();
    setEditId(null);
    setModalOpen(false);
  }, [form]);

  // 设置加载状态
  const setLoading = useCallback((loading) => {
    setSubmitLoading(loading);
  }, []);

  return {
    form,
    modalOpen,
    editId,
    submitLoading,
    openAddForm,
    openEditForm,
    closeForm,
    setLoading,
  };
};

export default useCameraForm;
