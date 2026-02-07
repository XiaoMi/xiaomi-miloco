/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

/**
 * OAuth authentication config
 */
export const AUTH_CONFIG = {
  /** maximum retry attempts */
  MAX_RETRY_ATTEMPTS: 150,

  /** retry interval time(milliseconds) */
  RETRY_INTERVAL: 2000,

  /** authentication timeout time(milliseconds) */
  AUTH_TIMEOUT: 60000,
};

/**
 * sidebar width config
 */
export const SIDEBAR_WIDTH = {
  /** expanded state width */
  EXPANDED: 160,
  /** collapsed state width */
  COLLAPSED: 64,
  /** minimum width */
  MIN: 64,
  /** maximum width */
  MAX: 160,
};

/**
 * layout dimensions config
 */
export const LAYOUT_DIMENSIONS = {
  /** header height */
  HEADER_HEIGHT: 64,

  /** main content area minimum width */
  CONTENT_MIN_WIDTH: 800,

  /** splitter width */
  SPLITTER_WIDTH: 2,

  /** user avatar size */
  AVATAR_SIZE: 32,
};

/**
 * main navigation menu config
 * @type {Array<Object>} menu item config array
 */
export const MENU_ITEMS = [
  {
    key: '1',
    label: 'home.menu.instantInquiry',
    icon: 'menuInstant',
    selectedIcon: 'menuInstantSelected',
    path: '/home/instant?from=menu',
  },
  {
    key: '2',
    label: 'home.menu.modalManage',
    icon: 'menuModal',
    selectedIcon: 'menuModalSelected',
    path: '/home/modelManage',
  },
  {
    key: '3',
    label: 'home.menu.triggerRule',
    icon: 'menuSmart',
    selectedIcon: 'menuSmartSelected',
    path: '/home/smartCenter',
  },
  {
    key: '4',
    label: 'home.menu.executionManage',
    icon: 'menuExecution',
    selectedIcon: 'menuExecutionSelected',
    path: '/home/executionManage',
  },
  {
    key: '5',
    label: 'home.menu.mcpService',
    icon: 'menuMcp',
    selectedIcon: 'menuMcpSelected',
    path: '/home/mcpService',
  },
  {
    key: '6',
    label: 'home.menu.deviceManage',
    icon: 'menuDevice',
    selectedIcon: 'menuDeviceSelected',
    path: '/home/deviceManage',
  },
  {
    key: '7',
    label: 'home.menu.logManage',
    icon: 'menuLog',
    selectedIcon: 'menuLogSelected',
    path: '/home/logManage',
  },
  {
    key: '8',
    label: 'home.menu.memoryManage',
    icon: 'menuModal',
    selectedIcon: 'menuModalSelected',
    path: '/home/memoryManage',
  },
  {
    key: '9',
    label: 'home.menu.setting',
    icon: 'menuSetting',
    selectedIcon: 'menuSettingSelected',
    path: '/home/setting',
  }
];

/**
 * default selected menu item
 */
export const DEFAULT_SELECTED_MENU_KEY = '1';

