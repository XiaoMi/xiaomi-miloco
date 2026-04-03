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
 * Check if the data is a key frame
 */
const isKeyFrame = (data, codec) => {
  if (codec.startsWith('avc1') || codec.startsWith('h264')) {
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
  return true;
}

/**
 * VideoPlayer Component - WebCodecs-based video player with MJPEG fallback
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
  const autoCodecRef = useRef(null)
  const mjpegModeRef = useRef(false)

  useEffect(() => {
    const checkSupport = () => {
      const supported = (
        typeof window !== 'undefined' &&
        'VideoDecoder' in window &&
        'VideoFrame' in window &&
        'ImageBitmap' in window
      )
      setIsSupported(supported)
      return supported
    }
    checkSupport()
  }, [])

  useEffect(() => {
    if (onCanvasRef && canvasRef.current) {
      onCanvasRef(canvasRef)
    }
  }, [onCanvasRef, show])

  useEffect(() => {
    const cleanup = () => {
      if (wsRef.current) {
        try { wsRef.current.close && wsRef.current.close(1000, 'close_by_user'); } catch (e) {}
        wsRef.current = null;
      }
      if (decoderRef.current) {
        try { decoderRef.current.close && decoderRef.current.close(); } catch (e) {}
        decoderRef.current = null;
      }
    }

    const startMjpegStream = async () => {
      cleanup()
      const wsProtocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
      const wsUrl = `${wsProtocol}://${window.location.host}${import.meta.env.VITE_API_BASE || ''}/api/miot/ws/mjpeg_stream?camera_id=${encodeURIComponent(cameraId)}&channel=${encodeURIComponent(channel)}`

      console.log('Starting MJPEG stream for camera:', cameraId)
      setLoading(true)
      setError(null)
      setShow(false)
      let ready = false
      const canvas = canvasRef.current
      const ctx = canvas.getContext('2d')

      wsRef.current = new window.WebSocket(wsUrl)
      wsRef.current.binaryType = 'arraybuffer'

      wsRef.current.onerror = () => {
        setError(t('instant.deviceList.deviceConnectFailed'))
        message.error(t('instant.deviceList.deviceConnectFailed'))
        onPlay && onPlay()
      }
      wsRef.current.onclose = (event) => {
        if (event.reason !== 'close_by_user') {
          onPlay && onPlay()
        }
      }
      wsRef.current.onmessage = (e) => {
        if (!(e.data instanceof ArrayBuffer)) return
        const blob = new Blob([e.data], { type: 'image/jpeg' })
        const url = URL.createObjectURL(blob)
        const img = new Image()
        img.onload = () => {
          canvas.width = img.naturalWidth
          canvas.height = img.naturalHeight
          ctx.drawImage(img, 0, 0)
          URL.revokeObjectURL(url)
          if (!ready) {
            setLoading(false)
            setShow(true)
            if (onCanvasRef && canvasRef.current) {
              onCanvasRef(canvasRef)
            }
            ready = true
          }
        }
        img.onerror = () => URL.revokeObjectURL(url)
        img.src = url
      }
    }

    const startWebCodecsStream = async () => {
      cleanup()
      autoCodecRef.current = null

      const wsProtocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
      const wsUrl = `${wsProtocol}://${window.location.host}${import.meta.env.VITE_API_BASE || ''}/api/miot/ws/video_stream?camera_id=${encodeURIComponent(cameraId)}&channel=${encodeURIComponent(channel)}`

      setLoading(true)
      setError(null)
      setShow(false)
      let ready = false
      let decoderReady = false
      let waitForKeyFrame = true
      let decodeFailed = false
      const canvas = canvasRef.current
      const ctx = canvas.getContext('2d')
      await sleep(1000)

      wsRef.current = new window.WebSocket(wsUrl)
      wsRef.current.binaryType = 'arraybuffer'

      wsRef.current.onerror = () => {
        setError(t('instant.deviceList.deviceConnectFailed'))
        message.error(t('instant.deviceList.deviceConnectFailed'))
        onPlay && onPlay()
      }
      wsRef.current.onclose = (event) => {
        if (!decodeFailed && event.reason !== 'close_by_user') {
          onPlay && onPlay()
        }
      }

      wsRef.current.onmessage = async (e) => {
        if (!(e.data instanceof ArrayBuffer) || decodeFailed) return
        const uint8 = new Uint8Array(e.data)

        if (!autoCodecRef.current) {
          const detected = detectCodec(uint8)
          if (detected === 'unknown') return

          const codecStr = detected === 'h264' ? 'avc1.42E01E' : 'hev1.1.6.L120.B0'
          console.log('Detected codec:', detected, '->', codecStr)

          // Check if browser supports this codec
          try {
            const support = await VideoDecoder.isConfigSupported({
              codec: codecStr,
              hardwareAcceleration: 'prefer-hardware',
            })
            if (!support.supported) {
              console.warn('Codec not supported, falling back to MJPEG:', codecStr)
              decodeFailed = true
              mjpegModeRef.current = true
              startMjpegStream()
              return
            }
          } catch (err) {
            console.warn('isConfigSupported failed, falling back to MJPEG:', err)
            decodeFailed = true
            mjpegModeRef.current = true
            startMjpegStream()
            return
          }

          autoCodecRef.current = codecStr
          try {
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
                    ready = true
                  }
                })
              },
              error: (err) => {
                console.error('VideoDecoder error, falling back to MJPEG:', err)
                decodeFailed = true
                mjpegModeRef.current = true
                startMjpegStream()
              }
            })
            decoderRef.current.configure({
              codec: codecStr,
              hardwareAcceleration: 'prefer-hardware',
            })
            decoderReady = true
          } catch (err) {
            console.error('Create decoder failed, falling back to MJPEG:', err)
            decodeFailed = true
            mjpegModeRef.current = true
            startMjpegStream()
            return
          }
        }

        if (!decoderReady || !decoderRef.current) return

        const isKey = isKeyFrame(uint8, autoCodecRef.current)
        if (waitForKeyFrame) {
          if (!isKey) return
          waitForKeyFrame = false
        }

        try {
          decoderRef.current.decode(new EncodedVideoChunk({
            type: isKey ? 'key' : 'delta',
            timestamp: performance.now(),
            data: uint8
          }))
        } catch (err) {
          console.error('Decode error, falling back to MJPEG:', err)
          decodeFailed = true
          mjpegModeRef.current = true
          startMjpegStream()
        }
      }
    }

    const init = async () => {
      if (!cameraId || isSupported === null) return

      if (isFirefox()) {
        // Firefox doesn't support WebCodecs, use MJPEG directly
        mjpegModeRef.current = true
        startMjpegStream()
        return
      }

      if (!isSupported || mjpegModeRef.current) {
        startMjpegStream()
        return
      }

      startWebCodecsStream()
    }

    init()
    return cleanup
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
