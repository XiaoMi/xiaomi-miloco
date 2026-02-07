/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import React, { useState, useCallback, useMemo, useRef } from 'react'
import { message } from 'antd';
import { useTranslation } from 'react-i18next';
import { Icon } from '@/components';
import DefaultCameraBg from '@/assets/images/default-camera-bg.png'
import VideoPlayer from '../VideoPlayer/index'
import VideoModal from '../VideoModal/index'
import styles from './index.module.less'

/**
 * DeviceItem Component - Individual device item with video playback and controls
 * 设备项组件 - 带有视频播放和控制功能的单个设备项
 *
 * @param {Object} props - Component props
 * @param {Object} props.item - Device object containing device information
 * @param {Function} props.onPlay - Play/stop callback function
 * @param {boolean} props.playing - Whether the device is currently playing
 * @returns {JSX.Element} Device item component
 */
const DeviceItem = ({ item, onPlay, playing }) => {
  const [showVideo, setShowVideo] = useState(false)
  const [channel, setChannel] = useState(0)
  const [showVideoModal, setShowVideoModal] = useState(false)
  const canvasRef = useRef(null)
  const { t } = useTranslation();

  React.useEffect(() => {
    if (playing) {
      setShowVideo(false)
      setTimeout(() => {
        setShowVideo(true)
      }, 200)
    } else {
      setShowVideo(false)
    }
  }, [playing])

  const handleChannel = useCallback((channel) => {
    setChannel(channel)
  }, [])

  const handleVideoZoom = useCallback(() => {

    // if there is canvas content, it means the video is playing, directly display Modal
    if (canvasRef.current) {
      setShowVideoModal(true)
    } else {
      console.log('❌ no canvas content, do not display Modal')
    }
  }, [playing, showVideo, showVideoModal, item.did])

  const handleCloseVideoModal = useCallback(() => {
    setShowVideoModal(false)
  }, [])

  const handleCanvasRef = useCallback((ref) => {
    canvasRef.current = ref?.current
  }, [])

  const ChannelView = useMemo(() => {
    return (
      <>

        {item.channel_count > 1 && <div className={styles.channelView} onClick={() => {
          handleChannel((channel + 1) % item.channel_count)
        }}>
          <Icon style={{ cursor: 'pointer', color: 'rgba(255, 255, 255, 1)' }} name="instantCameraChange" width={38} height={38} />
        </div>
        }
        <div className={styles.zoomView} onClick={handleVideoZoom}>
          <Icon style={{ cursor: 'pointer', color: 'rgba(255, 255, 255, 1)' }} name="instantCameraZoomIn" width={38} height={38} />
        </div>
      </>

    )
  }, [item.channel, channel, t])


  const PlayView = ({ item }) => {
    const { online, is_set_pincode } = item
    if (is_set_pincode > 0) {
      return (
        <Icon name="lock"
          size={25}
          style={{
            width: '25px',
            height: '25px',
          }}
          onClick={() => {
            message.error(t('instant.deviceList.deviceLocked'))
          }}
        />

      )
    }
    if (!online) {
      return (
        <Icon name="cloud" size={24} style={{
          width: '24px',
          height: '24px',
        }}
          onClick={() => {
            message.error(t('instant.deviceList.deviceOffline'))
          }}
        />
      )
    }
    return (
      <div
        className={`${styles.playIcon}`}
        onClick={() => {
          onPlay && onPlay()
        }}>
        <Icon name="instantDevicePlay" width={10} height={14} />
      </div>
    )
  }

  return (
    <div className={styles.listItem}>
      {playing && showVideo && (
        <div
          className={styles.videoOverlay}
          style={{
            height: '154px',
            opacity: showVideo ? 1 : 0,
            overflow: 'hidden'
          }}>
          <VideoPlayer
            cameraId={item.did}
            channel={channel}
            codec={'hev1.1.6.L93.B0'}
            cameraType={item.camera_type || 'miot'}
            poster={item.cover}
            onCanvasRef={handleCanvasRef}
            // onReady={handleVideoReady}
            // onError={handleVideoError}
            style={{ width: '100%', height: '154px' }}
            onPlay={onPlay}
          />
        </div>
      )}
      {
        !playing && (
          <div className={styles.defaultCameraBg}>
            <img src={DefaultCameraBg} alt="default-camera-bg" />
          </div>
        )
      }
      <div className={styles.itemContent}>
        <div className={styles.itemContentWrap}>
          <div className={styles.info}>
            <div className={styles.infoTop}>
              <div className={styles.top}>
                <div className={`${styles.name} ${playing ? styles.playColor : ''}`}>{item.room_name || t('instant.deviceList.noDevice')}</div>
              </div>
              <div className={`${styles.bottom} ${playing ? styles.playColor : ''}`}>{item.name || t('instant.deviceList.noDevice')}</div>
            </div>
          </div>
          {
            playing && (
              <div className={styles.playIconWrap}>
                {
                  ChannelView
                }
                <div
                  className={`${styles.playIcon} ${styles.playPosition}`}
                  onClick={() => {
                    onPlay && onPlay()
                  }}>
                  <Icon name="instantDevicePause" width={11} height={12} />
                </div>
              </div>
            )
          }

          {
            !playing && (
              <div className={styles.playIconWrap}>
                <PlayView item={item} />
              </div>
            )
          }
        </div>


      </div>
      <VideoModal
        visible={showVideoModal}
        onClose={handleCloseVideoModal}
        sourceCanvasRef={canvasRef}
        deviceInfo={{
          room_name: item.room_name,
          name: item.name
        }}
        channelCount={item.channel_count}
        currentChannel={channel}
        onChannelChange={() => handleChannel((channel + 1) % item.channel_count)}
      />
    </div>
  )
}

export default DeviceItem
