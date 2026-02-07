/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import React, { useState, useCallback, useEffect } from 'react';
import { Card, Empty, Spin, Modal, Tag, List, Descriptions, Badge, Alert, Tooltip } from 'antd';
import { AppstoreOutlined, EnvironmentOutlined, ApiOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { getHADevices, getHAAreas, getHADeviceEntities, getHAStates, getHAWsStatus } from '@/api';
import { message } from 'antd';
import styles from './index.module.less';

/**
 * HADeviceTab - Home Assistant 设备选项卡
 * 显示HA设备列表，点击设备显示实体详情
 *
 * @returns {JSX.Element} HA设备选项卡组件
 */
const HADeviceTab = () => {
  const { t } = useTranslation();
  const [devices, setDevices] = useState([]);
  const [areas, setAreas] = useState([]);
  const [states, setStates] = useState([]);
  const [loading, setLoading] = useState(false);
  const [wsStatus, setWsStatus] = useState({ configured: false, connected: false });
  
  // 实体弹框状态
  const [entityModalOpen, setEntityModalOpen] = useState(false);
  const [selectedDevice, setSelectedDevice] = useState(null);
  const [deviceEntities, setDeviceEntities] = useState(null);
  const [entityLoading, setEntityLoading] = useState(false);

  // 获取 WebSocket 状态
  const fetchWsStatus = useCallback(async () => {
    try {
      const res = await getHAWsStatus();
      if (res.code === 0) {
        setWsStatus(res.data || { configured: false, connected: false });
      }
    } catch (error) {
      console.error('获取HA WebSocket状态失败:', error);
    }
  }, []);

  // 获取设备和区域列表
  const fetchData = useCallback(async () => {
    if (!wsStatus.connected) return;
    
    try {
      setLoading(true);
      const [devicesRes, areasRes, statesRes] = await Promise.all([
        getHADevices(),
        getHAAreas(),
        getHAStates()
      ]);

      if (devicesRes.code === 0) {
        setDevices(devicesRes.data || []);
      } else {
        message.error(t('deviceManage.fetchHADevicesFailed'));
      }

      if (areasRes.code === 0) {
        setAreas(areasRes.data || []);
      }

      if (statesRes.code === 0) {
        setStates(statesRes.data || []);
      }
    } catch (error) {
      console.error('获取HA设备列表失败:', error);
      message.error(t('deviceManage.fetchHADevicesFailed'));
    } finally {
      setLoading(false);
    }
  }, [t, wsStatus.connected]);

  useEffect(() => {
    fetchWsStatus();
  }, [fetchWsStatus]);

  useEffect(() => {
    if (wsStatus.connected) {
      fetchData();
    }
  }, [fetchData, wsStatus.connected]);

  // 根据area_id获取区域名称
  const getAreaName = (areaId) => {
    if (!areaId) return t('deviceManage.unknownArea');
    const area = areas.find(a => a.area_id === areaId);
    return area?.name || t('deviceManage.unknownArea');
  };

  // 点击设备显示实体
  const handleDeviceClick = async (device) => {
    setSelectedDevice(device);
    setEntityModalOpen(true);
    setEntityLoading(true);
    
    try {
      const res = await getHADeviceEntities(device.id);
      if (res.code === 0) {
        setDeviceEntities(res.data);
      } else {
        message.error(t('deviceManage.fetchEntitiesFailed'));
      }
    } catch (error) {
      console.error('获取设备实体失败:', error);
      message.error(t('deviceManage.fetchEntitiesFailed'));
    } finally {
      setEntityLoading(false);
    }
  };

  // 关闭实体弹框
  const closeEntityModal = () => {
    setEntityModalOpen(false);
    setSelectedDevice(null);
    setDeviceEntities(null);
  };

  // 获取实体状态
  const getEntityState = (entityId) => {
    return states.find(s => s.entity_id === entityId);
  };

  // 渲染实体列表
  const renderEntityList = () => {
    if (!deviceEntities) return null;

    const entityIds = deviceEntities.entity || [];
    if (entityIds.length === 0) {
      return <Empty description={t('deviceManage.noEntities')} />;
    }

    return (
      <List
        dataSource={entityIds}
        renderItem={(entityId) => {
          const state = getEntityState(entityId);
          const entityName = state?.attributes?.friendly_name || entityId;
          const stateValue = state?.state || 'unknown';
          
          return (
            <List.Item key={entityId}>
              <List.Item.Meta
                avatar={<ApiOutlined className={styles.entityIcon} />}
                title={
                  <div className={styles.entityTitle}>
                    <Tooltip title={entityName} placement="topLeft">
                      <span className={styles.entityName}>{entityName}</span>
                    </Tooltip>
                    <Tooltip title={stateValue} placement="topRight">
                      <Tag 
                        color={stateValue === 'on' || stateValue === 'home' ? 'green' : 'default'}
                        className={styles.entityState}
                      >
                        {stateValue}
                      </Tag>
                    </Tooltip>
                  </div>
                }
                description={
                  <div className={styles.entityDesc}>
                    <span className={styles.entityId}>{entityId}</span>
                    {state?.attributes && (
                      <Tooltip 
                        title={Object.entries(state.attributes)
                          .filter(([key]) => !['friendly_name', 'icon'].includes(key))
                          .map(([key, value]) => {
                            const valueStr = typeof value === 'object' ? JSON.stringify(value) : String(value);
                            return `${key}: ${valueStr}`;
                          })
                          .join('、')
                        }
                      >
                        <div className={styles.attributes}>
                          {Object.entries(state.attributes)
                            .filter(([key]) => !['friendly_name', 'icon'].includes(key))
                            .map(([key, value], index, arr) => {
                              const valueStr = typeof value === 'object' ? JSON.stringify(value) : String(value);
                              return `${key}: ${valueStr}${index < arr.length - 1 ? '、' : ''}`;
                            })
                            .join('')
                          }
                        </div>
                      </Tooltip>
                    )}
                  </div>
                }
              />
            </List.Item>
          );
        }}
      />
    );
  };

  // 未连接状态
  if (!wsStatus.configured) {
    return (
      <div className={styles.statusContainer}>
        <Alert
          message={t('deviceManage.haNotConfigured')}
          description={t('deviceManage.haNotConfiguredDesc')}
          type="warning"
          showIcon
        />
      </div>
    );
  }

  if (!wsStatus.connected) {
    return (
      <div className={styles.statusContainer}>
        <Alert
          message={t('deviceManage.haNotConnected')}
          description={t('deviceManage.haNotConnectedDesc')}
          type="info"
          showIcon
        />
        <Spin className={styles.connectingSpin} />
      </div>
    );
  }

  if (loading) {
    return (
      <div className={styles.loadingContainer}>
        <Spin />
      </div>
    );
  }

  if (devices.length === 0) {
    return (
      <div className={styles.emptyContainer}>
        <Empty description={t('deviceManage.noHADevices')} />
      </div>
    );
  }

  return (
    <div className={styles.haDeviceTab}>
      {/* 连接状态显示 */}
      <div className={styles.statusBar}>
        <Badge status="success" text={t('deviceManage.haConnected')} />
        <span className={styles.deviceCount}>
          {t('deviceManage.deviceCount', { count: devices.length })}
        </span>
      </div>

      {/* 设备列表 */}
      <div className={styles.deviceGrid}>
        {devices.map((device) => (
          <Card
            key={device.id}
            className={styles.deviceCard}
            hoverable
            onClick={() => handleDeviceClick(device)}
            size="small"
          >
            <div className={styles.cardContent}>
              <div className={styles.deviceHeader}>
                <AppstoreOutlined className={styles.deviceIcon} />
                <span className={styles.deviceName}>
                  {device.name_by_user || device.name || device.id}
                </span>
              </div>
              
              <div className={styles.deviceDetails}>
                <div className={styles.detailItem}>
                  <EnvironmentOutlined />
                  <span>{getAreaName(device.area_id)}</span>
                </div>
                {device.manufacturer && (
                  <div className={styles.detailItem}>
                    <span className={styles.label}>{t('deviceManage.manufacturer')}:</span>
                    <span>{device.manufacturer}</span>
                  </div>
                )}
                {device.model && (
                  <div className={styles.detailItem}>
                    <span className={styles.label}>{t('deviceManage.model')}:</span>
                    <span>{device.model}</span>
                  </div>
                )}
              </div>
            </div>
          </Card>
        ))}
      </div>

      {/* 实体详情弹框 */}
      <Modal
        title={
          <div className={styles.modalTitle}>
            <AppstoreOutlined />
            <span>{selectedDevice?.name_by_user || selectedDevice?.name || t('deviceManage.deviceEntities')}</span>
          </div>
        }
        open={entityModalOpen}
        onCancel={closeEntityModal}
        footer={null}
        width={700}
        className={styles.entityModal}
      >
        {entityLoading ? (
          <div className={styles.modalLoading}>
            <Spin />
          </div>
        ) : (
          <div className={styles.entityContent}>
            {selectedDevice && (
              <Descriptions size="small" column={2} className={styles.deviceDesc}>
                <Descriptions.Item label={t('deviceManage.deviceId')}>
                  {selectedDevice.id}
                </Descriptions.Item>
                <Descriptions.Item label={t('deviceManage.area')}>
                  {getAreaName(selectedDevice.area_id)}
                </Descriptions.Item>
                {selectedDevice.manufacturer && (
                  <Descriptions.Item label={t('deviceManage.manufacturer')}>
                    {selectedDevice.manufacturer}
                  </Descriptions.Item>
                )}
                {selectedDevice.model && (
                  <Descriptions.Item label={t('deviceManage.model')}>
                    {selectedDevice.model}
                  </Descriptions.Item>
                )}
              </Descriptions>
            )}
            
            <div className={styles.entityList}>
              <div className={styles.entityListTitle}>{t('deviceManage.entityList')}</div>
              {renderEntityList()}
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
};

export default HADeviceTab;
