/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import React, { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next';
import { Popconfirm } from 'antd';
import { CloseOutlined } from '@ant-design/icons';
import { useChatStore } from '@/stores/chatStore';
import { useGlobalSocket } from '@/hooks/useGlobalSocket';
import { Icon } from '@/components';
import DeviceList from './components/DeviceList'
import ChatDialog from './components/ChatDialog'
import styles from './index.module.less'

/**
 * Instant Page - Real-time chat interface with camera device management and history
 * 即时聊天页面 - 带有摄像头设备管理和历史记录的实时聊天界面
 *
 * @returns {JSX.Element} Instant chat page component
 */
const Instant = () => {
  const { t } = useTranslation();

  const [currentPlayingId, setCurrentPlayingId] = useState([])
  const [leftDrawerVisible, setLeftDrawerVisible] = useState(true)
  const [rightDrawerVisible, setRightDrawerVisible] = useState(false)

  const {
    cameraList,
    historyList,
    historyLoading,
    isAnswering,
    fetchCameraList,
    fetchMcpServices,
    refreshMiotInfo,
    fetchHistoryList,
    handleHistoryClick,
    deleteHistoryRecord
  } = useChatStore();

  const { globalCloseMessage } = useGlobalSocket();

  useEffect(() => {
    fetchCameraList()
    fetchMcpServices()
  }, [])



  // play/close video
  const playStream = (item) => {
    if (!item) {return}
    if (currentPlayingId.includes(item.did)) {
      setCurrentPlayingId(currentPlayingId.filter(id => id !== item.did))
      return
    }
    if (currentPlayingId.length >= 4) {
      setCurrentPlayingId(currentPlayingId.slice(1))
    }
    setCurrentPlayingId([...currentPlayingId, item.did])
  }

  const handleClickHistory = async (id) => {
    // Stop current query if there is one in progress
    if (isAnswering) {
      globalCloseMessage();
    }
    await handleHistoryClick(id);
    setRightDrawerVisible(false);
  }

  const handleDeleteHistory = async (sessionId, e) => {
    e.stopPropagation();
    await deleteHistoryRecord(sessionId);
  }

  return (
    <>
      <div className={styles.instantLayout}>
        {/* left camera device area */}
        <div className={styles.leftSidebar} style={{ width: leftDrawerVisible ? 320 : 0 }}>
          <DeviceList
            cameraList={cameraList}
            onPlay={playStream}
            currentPlayingId={currentPlayingId}
            onRefresh={refreshMiotInfo}
            onClose={() => {
              setLeftDrawerVisible(false)
            }}
          />
        </div>

        {/* middle chat area */}
        <div className={styles.chatDialogArea}>
          <ChatDialog />
        </div>



        {/* right history record area */}
        <div className={styles.rightSidebar} style={{ width: rightDrawerVisible ? '240px' : 0 }}>
          <div className={styles.historyContent}>
            <div className={styles.historyHeader}>
              <div>{t('instant.history.historyRecord')}</div>
              <div
                className={styles.closeButton}
                onClick={() => setRightDrawerVisible(false)}
              >
                <CloseOutlined style={{ fontSize: '12px' }} />
              </div>
            </div>
            <div className={styles.historyList}>
              {historyLoading ? (
                <div className={styles.loading}>{t('common.loading')}</div>
              ) : (
                historyList?.map(item => (
                  <div
                    key={item.session_id}
                    className={styles.historyItem}
                    onClick={() => handleClickHistory(item.session_id)}
                  >
                    <span className={styles.historyTitle}>
                      {item.title || t('instant.history.unnamed')}
                    </span>
                    <Popconfirm
                      title={t('instant.history.confirmDeleteTitle')}
                      description={t('instant.history.confirmDeleteDescription')}
                      onConfirm={(e) => handleDeleteHistory(item.session_id, e)}
                      onCancel={(e) => e?.stopPropagation()}
                      okText={t('common.confirm')}
                      cancelText={t('common.cancel')}
                    >
                      <div
                        className={styles.deleteIcon}
                        onClick={(e) => e.stopPropagation()}
                      >
                        <CloseOutlined style={{ fontSize: '10px' }} />
                      </div>
                    </Popconfirm>
                  </div>

                ))
              )}
            </div>
          </div>
        </div>

        {/* left expand button */}
        <div
          className={styles.leftToggleButton}
          style={{opacity: leftDrawerVisible ? 0 : 1}}
          onClick={() => setLeftDrawerVisible(true)}
        >
          <Icon name="instantCameraOpen" size={20}/>
        </div>

        {/* right expand button */}
        <div
          className={styles.rightToggleButton}
          style={{opacity: rightDrawerVisible ? 0 : 1}}
          onClick={() => {
            setRightDrawerVisible(true)
            fetchHistoryList()
          }}
        >
          <Icon name="instantGotoHistory" className={styles.toggleIcon} size={20}/>
        </div>
      </div>
    </>
  )
}

export default Instant
