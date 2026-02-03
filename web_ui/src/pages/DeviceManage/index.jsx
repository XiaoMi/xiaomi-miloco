/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import React, { useState } from 'react';
import { Tabs } from 'antd';
import { useTranslation } from 'react-i18next';
import { Header, Icon, PageContent } from '@/components';
import MijiaDeviceTab from './components/MijiaDeviceTab';
import RTSPCameraTab from './components/RTSPCameraTab';
import HADeviceTab from './components/HADeviceTab';
import styles from './index.module.less';

/**
 * DeviceManage Page - 设备管理页面
 * 包含三个选项卡：米家设备、RTSP摄像头、HA设备
 *
 * @returns {JSX.Element} 设备管理页面组件
 */
const DeviceManage = () => {
  const { t } = useTranslation();
  const [activeTab, setActiveTab] = useState('mijia');
  const [refreshKey, setRefreshKey] = useState(0);

  // 刷新当前tab
  const handleRefresh = () => {
    setRefreshKey(prev => prev + 1);
  };

  const tabItems = [
    {
      key: 'mijia',
      label: t('deviceManage.mijiaDevices'),
      children: <MijiaDeviceTab key={`mijia-${refreshKey}`} />,
    },
    {
      key: 'rtsp',
      label: t('deviceManage.rtspCameras'),
      children: <RTSPCameraTab key={`rtsp-${refreshKey}`} />,
    },
    {
      key: 'ha',
      label: t('deviceManage.haDevices'),
      children: <HADeviceTab key={`ha-${refreshKey}`} />,
    },
  ];

  return (
    <div className={styles.deviceManageContainer}>
      <PageContent
        Header={(
          <Header
            title={t('home.menu.deviceManage')}
            rightContent={
              <div
                className={styles.refreshButton}
                onClick={handleRefresh}
              >
                <Icon
                  name="refresh"
                  size={15}
                  style={{ color: 'var(--text-color)' }}
                />
                <span className={styles.refreshText}>{t('common.refresh')}</span>
              </div>
            }
          />
        )}
      >
        <div className={styles.tabsContainer}>
          <Tabs
            activeKey={activeTab}
            onChange={setActiveTab}
            items={tabItems}
            className={styles.deviceTabs}
          />
        </div>
      </PageContent>
    </div>
  );
};

export default DeviceManage;
