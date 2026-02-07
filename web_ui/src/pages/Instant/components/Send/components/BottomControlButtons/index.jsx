/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import React, { useCallback, useEffect } from 'react';
import { Button, Flex, Tooltip, Select } from 'antd';
import { useTranslation } from 'react-i18next';
import { SoundOutlined } from '@ant-design/icons';
import Icon from '@/components/Icon';
import { useChatStore } from '@/stores/chatStore';
import { UnifiedSelector } from '../';
import styles from '../../style.module.less';

const { Option } = Select;

/**
 * BottomControlButtons Component - Bottom control buttons for camera selection, MCP services, and mode switching
 * 底部控制按钮组件 - 专门处理底部的功能按钮（摄像头选择、MCP服务、模式切换等）
 *
 * @param {Object} props - Component props
 * @returns {JSX.Element} Bottom control buttons component
 */
const BottomControlButtons = () => {
  const { t } = useTranslation();
  const {
    cameraList,
    selectedCameraIds,
    isAnswering,
    sessionId,
    mcpList,
    mcpVisible,
    availableMcpServices,
    mcpLoading,
    cameraVisible,
    setMcpList,
    toggleMcpService,
    toggleMcpVisible,
    setCameraVisible,
    handleCameraSelect,
    handleCameraSelectAll,
    handleMcpSelectAll,
    handleMcpReconnect,
    // Speaker related
    speakerList,
    speakerLoading,
    selectedSpeakerId,
    fetchSpeakerList,
    setSelectedSpeakerId,
  } = useChatStore();

  // Fetch speaker list on mount
  useEffect(() => {
    fetchSpeakerList();
    // Refresh speaker list every 10 seconds
    const interval = setInterval(fetchSpeakerList, 10000);
    return () => clearInterval(interval);
  }, [fetchSpeakerList]);


  const availableMcpList = availableMcpServices.filter(service => mcpList.includes(service.client_id))
  const availableMcpListIds = availableMcpList.map(service => service.client_id)

  const onlineCameraList = cameraList.filter(item => item?.online || false);
  const autoSelect = onlineCameraList.length === 0 && selectedCameraIds.length === 0;

  return (
    <Flex justify="space-between" style={{ flex: 0 }} align="center" className={styles.sendFooter}>
      <Flex align="center" gap={8}>
        <div className={styles.buttonContainer}>
          <Tooltip title={t('instant.chat.cameraSelectTooltip')}>
            <Button
              className={styles.chatBtn + (isAnswering ? ' disabled' : '')}
              style={{
                backgroundColor: !autoSelect ? 'var(--bg-color-chat-button-active)' : '',
                color: !autoSelect ? 'rgba(255, 255, 255, 1)' : '',
              }}
              onClick={() => setCameraVisible(!cameraVisible)}
            >
              {t('instant.chat.cameraSelect')} {selectedCameraIds.length > 0 && `(${selectedCameraIds.length})`}
            </Button>
          </Tooltip>

          <UnifiedSelector
            showAutoSelect={true}
            visible={cameraVisible}
            items={cameraList}
            selectedIds={selectedCameraIds}
            loading={false}
            showSelectAll={true}
            emptyText={'instant.chat.noCamera'}
            onToggleItem={handleCameraSelect}
            onSelectAll={handleCameraSelectAll}
            onClose={() => setCameraVisible(false)}
            itemConfig={{
              idField: 'did',
              nameField: 'name',
              descField: 'model',
              disabledField: 'online'
            }}
          />
        </div>

        <div className={styles.buttonContainer}>
          <Button
            className={styles.chatBtn + (isAnswering ? ' disabled' : '')}
            style={{
              backgroundColor: mcpList.length > 0 ? 'var(--bg-color-chat-button-active)' : '',
              color: mcpList.length > 0 ? 'rgba(255, 255, 255, 1)' : '',
            }}
            onClick={toggleMcpVisible}
            icon={<Icon name="instantFooterMcp" size={13} />}
          >
            MCP {Array.isArray(mcpList) && availableMcpList.length > 0 && `(${availableMcpList.length})`}
          </Button>
          <UnifiedSelector
            visible={mcpVisible}
            items={availableMcpServices}
            selectedIds={availableMcpListIds}
            loading={mcpLoading}
            showSelectAll={true}
            emptyText={'instant.chat.noService'}
            loadingText={'common.loading'}
            onToggleItem={toggleMcpService}
            onSelectAll={handleMcpSelectAll}
            onClose={toggleMcpVisible}
            onReconnect={handleMcpReconnect}
            itemConfig={{
              idField: 'client_id',
              nameField: 'server_name',
              descField: 'description',
              disabledField: 'connected'
            }}
          />
        </div>

        {/* Speaker selection */}
        {speakerList.length > 0 && (
          <div className={styles.speakerSelectContainer}>
            <Tooltip title={t('xiaoai.selectSpeaker')}>
              <Select
                value={selectedSpeakerId}
                onChange={setSelectedSpeakerId}
                placeholder={t('xiaoai.speakerPlayback')}
                allowClear
                size="small"
                className={styles.speakerSelect}
                style={{ width: 140 }}
                suffixIcon={<SoundOutlined />}
                loading={speakerLoading}
              >
                {speakerList.map(speaker => (
                  <Option key={speaker.speaker_id} value={speaker.speaker_id}>
                    <span>{speaker.model || speaker.speaker_id}</span>
                  </Option>
                ))}
              </Select>
            </Tooltip>
          </div>
        )}
      </Flex>
    </Flex>
  );
};

export default BottomControlButtons;
