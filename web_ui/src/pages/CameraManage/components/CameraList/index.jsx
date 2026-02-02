/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import React from 'react';
import { Card, Switch, Button, Popconfirm, Tag, Space, Tooltip } from 'antd';
import { EditOutlined, DeleteOutlined, ReloadOutlined, VideoCameraOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import styles from './index.module.less';

/**
 * CameraList Component - RTSP摄像头列表组件
 * 显示所有RTSP摄像头及其状态
 *
 * @param {Object} props - 组件属性
 * @param {Array} props.cameras - 摄像头列表
 * @param {Function} props.onSwitch - 启用/禁用回调
 * @param {Function} props.onEdit - 编辑回调
 * @param {Function} props.onDelete - 删除回调
 * @param {Function} props.onRefreshStatus - 刷新状态回调
 * @returns {JSX.Element} 摄像头列表组件
 */
const CameraList = ({ cameras, onSwitch, onEdit, onDelete, onRefreshStatus }) => {
  const { t } = useTranslation();

  return (
    <div className={styles.cameraList}>
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
                    <span className={styles.value + ' ' + styles.urlValue}>
                      {camera.rtsp_url_main}
                    </span>
                  </Tooltip>
                </div>
                {camera.rtsp_url_sub && (
                  <div className={styles.detailItem}>
                    <span className={styles.label}>{t('cameraManage.subStream')}:</span>
                    <Tooltip title={camera.rtsp_url_sub}>
                      <span className={styles.value + ' ' + styles.urlValue}>
                        {camera.rtsp_url_sub}
                      </span>
                    </Tooltip>
                  </div>
                )}
              </div>
            </div>

            <div className={styles.cameraActions}>
              <Switch
                checked={camera.enabled}
                onChange={(checked) => onSwitch(camera.id, checked)}
                size="small"
              />
              <Tooltip title={t('cameraManage.refreshStatus')}>
                <Button
                  type="text"
                  icon={<ReloadOutlined />}
                  onClick={() => onRefreshStatus(camera.id)}
                  size="small"
                />
              </Tooltip>
              <Button
                type="text"
                icon={<EditOutlined />}
                onClick={() => onEdit(camera)}
                size="small"
              />
              <Popconfirm
                title={t('common.confirmDelete')}
                onConfirm={() => onDelete(camera.id)}
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
  );
};

export default CameraList;
