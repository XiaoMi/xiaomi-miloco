/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import React from 'react';
import { Modal, Form, Input, Switch } from 'antd';
import { useTranslation } from 'react-i18next';
import styles from './index.module.less';

/**
 * CameraForm Component - RTSP摄像头表单组件
 * 用于添加和编辑RTSP摄像头
 *
 * @param {Object} props - 组件属性
 * @param {boolean} props.modalOpen - 模态框是否打开
 * @param {string} props.editId - 编辑的摄像头ID（null表示新增）
 * @param {Object} props.form - antd表单实例
 * @param {boolean} props.submitLoading - 提交加载状态
 * @param {Function} props.onCancel - 取消回调
 * @param {Function} props.onOk - 确认回调
 * @returns {JSX.Element} 摄像头表单组件
 */
const CameraForm = ({
  modalOpen,
  editId,
  form,
  submitLoading,
  onCancel,
  onOk,
}) => {
  const { t } = useTranslation();

  return (
    <Modal
      title={editId ? t('cameraManage.editCamera') : t('cameraManage.addCamera')}
      open={modalOpen}
      onCancel={onCancel}
      onOk={onOk}
      confirmLoading={submitLoading}
      okText={t('common.save')}
      cancelText={t('common.cancel')}
      destroyOnClose
      width={560}
    >
      <Form
        form={form}
        layout="vertical"
        className={styles.cameraForm}
        initialValues={{ enabled: true }}
      >
        <Form.Item
          name="name"
          label={t('cameraManage.cameraName')}
          rules={[
            { required: true, message: t('cameraManage.pleaseEnterCameraName') }
          ]}
        >
          <Input placeholder={t('cameraManage.cameraNamePlaceholder')} />
        </Form.Item>

        <Form.Item
          name="location"
          label={t('cameraManage.location')}
        >
          <Input placeholder={t('cameraManage.locationPlaceholder')} />
        </Form.Item>

        <Form.Item
          name="rtsp_url_main"
          label={t('cameraManage.mainStreamUrl')}
          rules={[
            { required: true, message: t('cameraManage.pleaseEnterMainStreamUrl') }
          ]}
          extra={t('cameraManage.rtspUrlHint')}
        >
          <Input placeholder="rtsp://username:password@192.168.1.100:554/stream1" />
        </Form.Item>

        <Form.Item
          name="rtsp_url_sub"
          label={t('cameraManage.subStreamUrl')}
          extra={t('cameraManage.subStreamHint')}
        >
          <Input placeholder="rtsp://username:password@192.168.1.100:554/stream2" />
        </Form.Item>

        <Form.Item
          name="enabled"
          label={t('cameraManage.enabled')}
          valuePropName="checked"
        >
          <Switch />
        </Form.Item>
      </Form>
    </Modal>
  );
};

export default CameraForm;
