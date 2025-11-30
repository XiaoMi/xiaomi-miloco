/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import React, { useEffect, useState, useRef } from 'react'
import { useTranslation } from 'react-i18next';
import { Popconfirm } from 'antd';
import { CloseOutlined } from '@ant-design/icons';
import { useParams, useNavigate, useLocation } from 'react-router-dom';
import { useChatStore } from '@/stores/chatStore';
import { getHistoryDetail } from '@/api';
import { processHistorySocketMessages } from '@/utils/instruction';
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
  const { sessionId: urlSessionId } = useParams();
  const navigate = useNavigate();
  const location = useLocation();

  const [currentPlayingId, setCurrentPlayingId] = useState([])
  const [leftDrawerVisible, setLeftDrawerVisible] = useState(true)
  const [rightDrawerVisible, setRightDrawerVisible] = useState(false)

  const {
    cameraList,
    historyList,
    historyLoading,
    sessionId,
    fetchCameraList,
    fetchMcpServices,
    refreshMiotInfo,
    fetchHistoryList,
    handleHistoryClick,
    deleteHistoryRecord
  } = useChatStore();

  const loadingSessionIdRef = useRef(null);
  const isClickingHistoryRef = useRef(false);
  useEffect(() => {
    fetchCameraList()
    fetchMcpServices()
  }, [])

  useEffect(() => {
    if (sessionId && sessionId !== urlSessionId && !loadingSessionIdRef.current && !isClickingHistoryRef.current) {
      const searchParams = new URLSearchParams(location.search);
      const newPath = `/home/instant/${sessionId}${searchParams.toString() ? `?${searchParams.toString()}` : ''}`;
      navigate(newPath, { replace: true });
    }
  }, [sessionId, urlSessionId, navigate, location.search]);

  // get session
  useEffect(() => {
    const loadHistoryFromSessionId = async () => {
      if (urlSessionId && urlSessionId !== sessionId && urlSessionId !== loadingSessionIdRef.current && !isClickingHistoryRef.current) {
        loadingSessionIdRef.current = urlSessionId;
        
        try {
          const response = await getHistoryDetail(urlSessionId);
          const { code, data } = response || {};

          if (code !== 0) {
            console.warn('Failed to fetch history from sessionId:', urlSessionId);
            loadingSessionIdRef.current = null;
            return;
          }

          const { session = {} } = data || {};
          const { data: sessionData } = session;

          if (!sessionData) {
            console.warn('History data is empty for sessionId:', urlSessionId);
            loadingSessionIdRef.current = null;
            return;
          }

          if (sessionData && Array.isArray(sessionData)) {
            const { messages, sessionId: historySessionId, latestConfig } = processHistorySocketMessages(sessionData);

            useChatStore.setState({
              messages: messages || [],
              currentAnswer: null,
              answerMessages: [],
              sessionId: historySessionId || urlSessionId,
              isHistoryMode: false,
              isAnswering: false,
              isScrollToBottom: true,
              selectedCameraIds: latestConfig?.cameraIds || [],
              mcpList: latestConfig?.mcpList || [],
            });
          }
        } catch (error) {
          console.error('Failed to load history from sessionId:', error);
        } finally {
          loadingSessionIdRef.current = null;
        }
      }
    };

    loadHistoryFromSessionId();
  }, [urlSessionId, sessionId]);


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
    if (id === sessionId && id === urlSessionId) {
      setRightDrawerVisible(false);
      return;
    }
    
    isClickingHistoryRef.current = true;
    
    const searchParams = new URLSearchParams(location.search);
    const newPath = `/home/instant/${id}${searchParams.toString() ? `?${searchParams.toString()}` : ''}`;
    navigate(newPath, { replace: true });
    
    try {
      await handleHistoryClick(id);
    } catch (error) {
      console.error('Failed to load history:', error);
    } finally {
      setTimeout(() => {
        isClickingHistoryRef.current = false;
      }, 200);
    }
    
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
