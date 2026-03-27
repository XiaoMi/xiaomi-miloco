/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import { useEffect, useRef } from 'react';
import { useGlobalSocket } from '@/hooks/useGlobalSocket';

/**
 * GlobalSocketProvider Component - Global Socket provider for application-level initialization
 * 全局Socket提供者组件 - 确保Socket在应用级别初始化，支持跨页面持久连接
 */
const GlobalSocketProvider = ({ children }) => {
  const isInitialized = useRef(false);

  // Initialize global socket without using the result - socket persists via store subscription
  useGlobalSocket();

  useEffect(() => {
    if (!isInitialized.current) {
      isInitialized.current = true;
    }

    return () => {
    };
  }, []);

  return children;
};

export default GlobalSocketProvider;
