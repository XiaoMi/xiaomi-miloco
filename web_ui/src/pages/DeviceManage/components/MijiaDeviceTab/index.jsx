/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import React from 'react';
import { Empty, Spin } from 'antd';
import { useTranslation } from 'react-i18next';
import DeviceCard from '../DeviceCard';
import { useDevices } from '../../hooks/useDevices';
import styles from './index.module.less';

/**
 * MijiaDeviceTab - 米家设备选项卡
 * 显示米家设备列表
 *
 * @returns {JSX.Element} 米家设备选项卡组件
 */
const MijiaDeviceTab = () => {
  const { t } = useTranslation();
  const { devices, loading } = useDevices();

  if (loading) {
    return (
      <div className={styles.loadingContainer}>
        <Spin />
      </div>
    );
  }

  if (!devices || devices.length === 0) {
    return (
      <div className={styles.emptyContainer}>
        <Empty description={t('deviceManage.noDevice')} />
      </div>
    );
  }

  return (
    <div className={styles.deviceGrid}>
      {devices.map((device) => (
        <DeviceCard key={device.did} device={device} />
      ))}
    </div>
  );
};

export default MijiaDeviceTab;
