/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import React, { useEffect, useRef, useState } from 'react'
import { Spin, message } from 'antd'
import { useTranslation } from 'react-i18next';
import { isFirefox, sleep } from '@/utils/util';
import DefaultCameraBg from '@/assets/images/default-camera-bg.png'

/**
 * Detect video codec from binary data
 * 从二进制数据中检测视频编码格式
 *
 * @param {Uint8Array} data - Binary video data
 * @returns {string} Detected codec type ('h264', 'h265', or 'unknown')
 */
const detectCodec = (data) => {
  let i = 0;
  while (i < data.length - 6) {
    if (
      data[i] === 0x00 && data[i + 1] === 0x00 &&
      ((data[i + 2] === 0x00 && data[i + 3] === 0x01) || data[i + 2] === 0x01)
    ) {
      const nalStart = data[i + 2] === 0x01 ? i + 3 : i + 4;
      const h264Type = data[nalStart] & 0x1f;
      const h265Type = (data[nalStart] >> 1) & 0x3f;
      if ([5, 7, 8].includes(h264Type)) {return 'h264';}
      if ([32, 33, 34, 19, 20].includes(h265Type)) {return 'h265';}
    }
    i++;
  }
  return 'unknown';
}

/**
 * VideoPlayer Component - WebCodecs-based video player for camera streams
 * 视频播放器组件 - 基于WebCodecs的摄像头流视频播放器
 *
 * @param {Object} props - Component props
 * @param {string} [props.codec='avc1.42E01E'] - Video codec format
 * @param {string} [props.poster] - Poster image URL
 * @param {Object} [props.style] - Custom style object
 * @param {string} props.cameraId - Camera device ID
 * @param {number} [props.channel=0] - Camera channel number
 * @param {Function} [props.onCanvasRef] - Canvas ref callback function
 * @returns {JSX.Element} Video player component
 */
const VideoPlayer = ({ codec = 'avc1.42E01E', poster, style, cameraId, channel, onCanvasRef, onPlay }) => {
  const { t } = useTranslation();
  const canvasRef = useRef(null)
  const wsRef = useRef(null)
  const decoderRef = useRef(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [show, setShow] = useState(false)
  const [isSupported, setIsSupported] = useState(null)
  const [autoCodec, setAutoCodec] = useState(null);

  // detect WebCodecs support
  useEffect(() => {
    const checkSupport = () => {
      console.log('Current environment:', {
        userAgent: navigator.userAgent,
        isSecureContext: window.isSecureContext,
        location: window.location.href,
        hasWindow: typeof window !== 'undefined',
        windowType: typeof window
      })

      const supported = (
        typeof window !== 'undefined' &&
        'VideoDecoder' in window &&
        'VideoFrame' in window &&
        'ImageBitmap' in window
      )

      console.log('WebCodecs API detection:', {
        hasWindow: typeof window !== 'undefined',
        hasVideoDecoder: typeof window !== 'undefined' && 'VideoDecoder' in window,
        hasVideoFrame: typeof window !== 'undefined' && 'VideoFrame' in window,
        hasImageBitmap: typeof window !== 'undefined' && 'ImageBitmap' in window,
        supported
      })

      if (!supported) {
        console.warn('⚠️ WebCodecs not supported, possible reasons:')
        console.warn('1. WebCodecs is not supported in this browser (Chrome 94+, Edge 94+)')
        console.warn('2. Vite hot update environment limit, please try to force refresh the page (F5)')
        console.warn('3. WebCodecs needs to be enabled in chrome://flags')
        console.warn('4. Needs HTTPS or localhost environment')
      }

      setIsSupported(supported)
      return supported
    }

    checkSupport()
  }, [])

  /**
   * Check if the data is a key frame
   * @param {Uint8Array} data - Binary video data
   * @param {string} codec - Video codec format
   * @returns {boolean} Whether the data is a key frame
   */
  const isKeyFrame = (data, codec) => {
    if (codec.startsWith('avc1') || codec.startsWith('h264')) {
      // H264
      let i = 0;
      while (i < data.length - 4) {
        if (
          data[i] === 0x00 && data[i + 1] === 0x00 &&
          ((data[i + 2] === 0x00 && data[i + 3] === 0x01) || data[i + 2] === 0x01)
        ) {
          const nalUnitType = data[i + 2] === 0x01 ? data[i + 3] & 0x1f : data[i + 4] & 0x1f;
          return nalUnitType === 5;
        }
        i++;
      }
      return false;
    } else if (codec.startsWith('hvc1') || codec.startsWith('hev1') || codec.startsWith('h265')) {
      // H265/HEVC
      let i = 0;
      while (i < data.length - 6) {
        if (
          data[i] === 0x00 && data[i + 1] === 0x00 &&
          ((data[i + 2] === 0x00 && data[i + 3] === 0x01) || data[i + 2] === 0x01)
        ) {
          const nalStart = data[i + 2] === 0x01 ? i + 3 : i + 4;
          const nalUnitType = (data[nalStart] >> 1) & 0x3f;
          if ([16, 17, 18, 19, 20].includes(nalUnitType)) {return true;}
        }
        i++;
      }
      return false;
    }
    // default to handle key frame
    return true;
  }

  useEffect(() => {
    if (onCanvasRef && canvasRef.current) {
      onCanvasRef(canvasRef)
    }
  }, [onCanvasRef, show])

  useEffect(() => {
    const init = async () => {
      if (!cameraId || isSupported === null) {return} // wait for support detection to complete

      if (isFirefox()) {
        setError(t('instant.deviceList.browserNotSupport'))
        message.error(t('instant.deviceList.browserNotSupport'))
        onPlay && onPlay()
        return
      }

      if (!isSupported) {
        setError(t('instant.deviceList.deviceNotSupport'))
        message.error(t('instant.deviceList.deviceNotSupport'))
        onPlay && onPlay()
        return
      }

      if (wsRef.current) {
        try {
          wsRef.current.close && wsRef.current.close();
        } catch (e) {
          console.error('Close WebSocket exception:', e);
        }
        wsRef.current = null;
      }
      if (decoderRef.current) {
        try {
          decoderRef.current.close && decoderRef.current.close();
        } catch (e) {
          if (e.name !== 'InvalidStateError') {
            console.error('Close VideoDecoder exception:', e);
          }
        }
        decoderRef.current = null;
      }
      const wsProtocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
      const wsUrl = `${wsProtocol}://${window.location.host}${import.meta.env.VITE_API_BASE || ''}/api/miot/ws/video_stream?camera_id=${encodeURIComponent(cameraId)}&channel=${encodeURIComponent(channel)}`
      setLoading(true)
      setError(null)
      setShow(false)
      let ready = false
      const canvas = canvasRef.current
      const ctx = canvas.getContext('2d')
      await sleep(1000)

      // here assume wsUrl pushes H264 AnnexB format
      wsRef.current = new window.WebSocket(wsUrl)
      wsRef.current.binaryType = 'arraybuffer'

      // connection failed handling
      wsRef.current.onerror = (err) => {
        console.log('video player: WebSocket connection failed', err)
        setError(t('instant.deviceList.deviceConnectFailed'))
        message.error(t('instant.deviceList.deviceConnectFailed'))
        wsRef.current && wsRef.current?.close?.()
        onPlay && onPlay()
      }
      // connection closed handling
      wsRef.current.onclose = (event) => {
        console.log('video player: WebSocket connection closed')
        if (!error) {
          setError(t('instant.deviceList.deviceConnectClosed'))
          // message.error(t('instant.deviceList.deviceConnectClosed'))
        }
        const { reason = '' } = event;
        if (reason !== 'close_by_user') {
          onPlay && onPlay()
        }
      }

      decoderRef.current = new window.VideoDecoder({
        output: frame => {
          createImageBitmap(frame).then(bitmap => {
            canvas.width = frame.codedWidth
            canvas.height = frame.codedHeight
            ctx.drawImage(bitmap, 0, 0)
            frame.close()
            bitmap.close && bitmap.close()
            if (!ready) {
              setLoading(false)
              setShow(true)
              if (onCanvasRef && canvasRef.current) {
                onCanvasRef(canvasRef)
              }
              // handleReady()
              ready = true
            }
          })
        },
        error: () => {
          setError(t('instant.deviceList.deviceDecodeFailed'))
          message.error(t('instant.deviceList.deviceDecodeFailed'))
        }
      })
      decoderRef.current.configure({
        codec,
        hardwareAcceleration: 'prefer-hardware',
      })
      wsRef.current.onmessage = e => {
        if (e.data instanceof ArrayBuffer) {
          const uint8 = new Uint8Array(e.data);
          if (!autoCodec) {
            const detected = detectCodec(uint8);
            if (detected !== 'unknown') {
              setAutoCodec(detected === 'h264' ? 'avc1.42E01E' : 'hvc1.1.6.L93.B0');
            }
          }
          const useCodec = autoCodec || codec;
          if (decoderRef.current._waitForKeyFrame === undefined) {
            decoderRef.current._waitForKeyFrame = true;
          }
          const isKey = isKeyFrame(uint8, useCodec);

          if (decoderRef.current._waitForKeyFrame) {
            if (!isKey) {
              return;
            } else {
              decoderRef.current._waitForKeyFrame = false;
            }
          }
          try {
            decoderRef.current.decode(new EncodedVideoChunk({
              type: isKey ? 'key' : 'delta',
              timestamp: performance.now(),
              data: uint8
            }))
          } catch (err) {
            setError(t('instant.deviceList.deviceDecodeFailed'))
          }
        }
      }
    }
    init()
    return () => {
      if (wsRef.current) {
        try {
          wsRef.current.close && wsRef.current.close(1000, 'close_by_user');
        } catch (e) {
          console.error('Close WebSocket exception:', e);
        }
        wsRef.current = null;
      }
      if (decoderRef.current) {
        try {
          decoderRef.current.close && decoderRef.current.close();
        } catch (e) {
          if (e.name !== 'InvalidStateError') {
            console.error('Close VideoDecoder exception:', e);
          }
        }
        decoderRef.current = null;
      }
    }
  }, [codec, isSupported, cameraId, channel])

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%', ...style }}>
      {loading && (
        <div style={{
          backgroundColor: 'rgba(0,0,0,0.1)',
          position: 'absolute', left: 0, top: 0, width: '100%', height: '100%', zIndex: 2,
          display: 'flex', alignItems: 'center', justifyContent: 'center', borderRadius: 8
        }}>
           <img
            src={DefaultCameraBg}
            alt="default-camera-bg"
            style={{ width: '100%',
              height: '100%',
              objectFit: 'cover',
              borderRadius: 8,
              position: 'absolute',
              top: 0,
              left: 0,
              zIndex: -1,
            }}
          />
          <Spin tip={t('common.loading')} />
        </div>
      )}
      {!show && poster && (
        <img src={poster} alt="poster" style={{ width: '100%', height: '100%', objectFit: 'cover', borderRadius: 8 }} />
      )}
      <canvas
        ref={canvasRef}
        style={{
          width: '100%', height: '100%', borderRadius: 8, objectFit: 'cover',
          opacity: show ? 1 : 0, transition: 'opacity 0.4s cubic-bezier(.4,0,.2,1)'
        }}
      />
    </div>
  )
}

export default VideoPlayer
