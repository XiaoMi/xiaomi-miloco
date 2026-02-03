/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import React, { useState, useCallback, useEffect } from 'react';
import { Card, Switch, Button, Popconfirm, Tag, Space, Tooltip, Empty, Spin, Modal } from 'antd';
import { EditOutlined, DeleteOutlined, ReloadOutlined, VideoCameraOutlined, PlayCircleOutlined, PlusOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { Form, Input } from 'antd';
import VideoPlayer from '@/pages/Instant/components/VideoPlayer';
import {
  getRtspCameraList,
  createRtspCamera,
  updateRtspCamera,
  deleteRtspCamera,
  checkRtspCameraStatus,
} from '@/api';
import { message } from 'antd';
import styles from './index.module.less';

/**
 * RTSPCameraTab - RTSP摄像头选项卡
 * 显示RTSP摄像头列表，支持添加、编辑、删除和播放
 *
 * @returns {JSX.Element} RTSP摄像头选项卡组件
 */
const RTSPCameraTab = () => {
  const { t } = useTranslation();
  const [cameras, setCameras] = useState([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editId, setEditId] = useState(null);
  const [submitLoading, setSubmitLoading] = useState(false);
  const [form] = Form.useForm();
  
  // 视频播放状态
  const [playingCamera, setPlayingCamera] = useState(null);
  const [playingChannel, setPlayingChannel] = useState(0);
  const [videoModalOpen, setVideoModalOpen] = useState(false);

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

  useEffect(() => {
    fetchCameras();
  }, [fetchCameras]);

  // 启用/禁用摄像头
  const handleSwitch = async (cameraId, enabled) => {
    try {
      const res = await updateRtspCamera(cameraId, { enabled });
      if (res.code === 0) {
        message.success(enabled ? t('cameraManage.cameraEnabled') : t('cameraManage.cameraDisabled'));
        fetchCameras();
      } else {
        message.error(t('cameraManage.updateFailed'));
      }
    } catch (error) {
      message.error(t('cameraManage.updateFailed'));
    }
  };

  // 删除摄像头
  const handleDelete = async (cameraId) => {
    try {
      const res = await deleteRtspCamera(cameraId);
      if (res.code === 0) {
        message.success(t('common.deleteSuccess'));
        fetchCameras();
      } else {
        message.error(t('common.deleteFail'));
      }
    } catch (error) {
      message.error(t('common.deleteFail'));
    }
  };

  // 刷新状态
  const handleRefreshStatus = async (cameraId) => {
    try {
      const res = await checkRtspCameraStatus(cameraId);
      if (res.code === 0) {
        message.success(t('cameraManage.statusRefreshed'));
        fetchCameras();
      } else {
        message.error(t('cameraManage.statusRefreshFailed'));
      }
    } catch (error) {
      message.error(t('cameraManage.statusRefreshFailed'));
    }
  };

  // 打开添加表单
  const openAddForm = () => {
    form.resetFields();
    setEditId(null);
    setModalOpen(true);
  };

  // 打开编辑表单
  const openEditForm = (camera) => {
    form.setFieldsValue({
      name: camera.name,
      location: camera.location,
      rtsp_url_main: camera.rtsp_url_main,
      rtsp_url_sub: camera.rtsp_url_sub,
      enabled: camera.enabled,
    });
    setEditId(camera.id);
    setModalOpen(true);
  };

  // 关闭表单
  const closeForm = () => {
    form.resetFields();
    setEditId(null);
    setModalOpen(false);
  };

  // 提交表单
  const handleFormSubmit = async () => {
    try {
      setSubmitLoading(true);
      const values = await form.validateFields();

      if (editId) {
        const res = await updateRtspCamera(editId, values);
        if (res.code === 0) {
          message.success(t('common.editSuccess'));
          closeForm();
          fetchCameras();
        } else {
          message.error(t('common.editFail'));
        }
      } else {
        const res = await createRtspCamera(values);
        if (res.code === 0) {
          message.success(t('common.addSuccess'));
          closeForm();
          fetchCameras();
        } else {
          message.error(t('common.addFail'));
        }
      }
    } catch (error) {
      console.error('保存摄像头失败:', error);
    } finally {
      setSubmitLoading(false);
    }
  };

  // 播放视频
  const handlePlay = (camera, channel = 0) => {
    setPlayingCamera(camera);
    setPlayingChannel(channel);
    setVideoModalOpen(true);
  };

  // 关闭视频播放
  const handleCloseVideo = () => {
    setVideoModalOpen(false);
    setPlayingCamera(null);
  };

  if (loading) {
    return (
      <div className={styles.loadingContainer}>
        <Spin />
      </div>
    );
  }

  return (
    <div className={styles.rtspCameraTab}>
      {/* 添加按钮 */}
      <div className={styles.toolbar}>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={openAddForm}
        >
          {t('cameraManage.addCamera')}
        </Button>
      </div>

      {/* 摄像头列表 */}
      {cameras.length === 0 ? (
        <div className={styles.emptyContainer}>
          <Empty description={t('cameraManage.noCameras')} />
        </div>
      ) : (
        <div className={styles.cameraGrid}>
          {cameras.map((camera) => (
            <Card
              key={camera.id}
              className={styles.cameraCard}
              size="small"
            >
              <div className={styles.cardContent}>
                <div className={styles.cameraInfo}>
                  <div className={styles.cameraHeader}>
                    <VideoCameraOutlined className={styles.cameraIcon} />
                    <span className={styles.cameraName}>{camera.name}</span>
                    <Space size={4}>
                      {camera.online_main && (
                        <Tag color="green">{t('cameraManage.mainStreamOnline')}</Tag>
                      )}
                      {camera.online_sub && (
                        <Tag color="blue">{t('cameraManage.subStreamOnline')}</Tag>
                      )}
                      {!camera.online_main && !camera.online_sub && (
                        <Tag color="default">{t('cameraManage.offline')}</Tag>
                      )}
                    </Space>
                  </div>
                  
                  <div className={styles.cameraDetails}>
                    {camera.location && (
                      <div className={styles.detailItem}>
                        <span className={styles.label}>{t('cameraManage.location')}:</span>
                        <span className={styles.value}>{camera.location}</span>
                      </div>
                    )}
                    <div className={styles.detailItem}>
                      <span className={styles.label}>{t('cameraManage.mainStream')}:</span>
                      <Tooltip title={camera.rtsp_url_main}>
                        <span className={`${styles.value} ${styles.urlValue}`}>
                          {camera.rtsp_url_main}
                        </span>
                      </Tooltip>
                    </div>
                  </div>
                </div>

                <div className={styles.cameraActions}>
                  {/* 播放按钮 */}
                  {(camera.online_main || camera.online_sub) && (
                    <Tooltip title={t('deviceManage.playVideo')}>
                      <Button
                        type="primary"
                        icon={<PlayCircleOutlined />}
                        onClick={() => handlePlay(camera, camera.online_main ? 0 : 1)}
                        size="small"
                      />
                    </Tooltip>
                  )}
                  <Switch
                    checked={camera.enabled}
                    onChange={(checked) => handleSwitch(camera.id, checked)}
                    size="small"
                  />
                  <Tooltip title={t('cameraManage.refreshStatus')}>
                    <Button
                      type="text"
                      icon={<ReloadOutlined />}
                      onClick={() => handleRefreshStatus(camera.id)}
                      size="small"
                    />
                  </Tooltip>
                  <Button
                    type="text"
                    icon={<EditOutlined />}
                    onClick={() => openEditForm(camera)}
                    size="small"
                  />
                  <Popconfirm
                    title={t('common.confirmDelete')}
                    onConfirm={() => handleDelete(camera.id)}
                    okText={t('common.confirm')}
                    cancelText={t('common.cancel')}
                  >
                    <Button
                      type="text"
                      danger
                      icon={<DeleteOutlined />}
                      size="small"
                    />
                  </Popconfirm>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}

      {/* 添加/编辑摄像头弹框 */}
      <Modal
        title={editId ? t('cameraManage.editCamera') : t('cameraManage.addCamera')}
        open={modalOpen}
        onCancel={closeForm}
        onOk={handleFormSubmit}
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

      {/* 视频播放弹框 */}
      <Modal
        title={playingCamera?.name || t('deviceManage.playVideo')}
        open={videoModalOpen}
        onCancel={handleCloseVideo}
        footer={null}
        width={800}
        destroyOnClose
        className={styles.videoModal}
      >
        {playingCamera && (
          <div className={styles.videoContainer}>
            <VideoPlayer
              cameraId={playingCamera.id}
              channel={playingChannel}
              cameraType="rtsp"
              style={{ width: '100%', height: '450px' }}
            />
            {/* 通道切换按钮 */}
            {playingCamera.rtsp_url_sub && (
              <div className={styles.channelSwitch}>
                <Button
                  type={playingChannel === 0 ? 'primary' : 'default'}
                  onClick={() => setPlayingChannel(0)}
                  disabled={!playingCamera.online_main}
                >
                  {t('cameraManage.mainStream')}
                </Button>
                <Button
                  type={playingChannel === 1 ? 'primary' : 'default'}
                  onClick={() => setPlayingChannel(1)}
                  disabled={!playingCamera.online_sub}
                >
                  {t('cameraManage.subStream')}
                </Button>
              </div>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
};

export default RTSPCameraTab;
