/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import { Spin, message } from 'antd';
import { memo } from 'react';
import { useTranslation } from 'react-i18next';
import { CopyOutlined } from '@ant-design/icons';
import styles from './index.module.less';

/**
 * LoadingScreen Component - Full screen loading state display
 * 加载屏幕组件 - 显示全屏加载状态
 *
 * @param {Object} props - Component props
 * @param {string} [props.title='common.loading'] - Loading title text
 * @param {string} [props.size='small'] - Loading spinner size: 'small' | 'default' | 'large'
 * @param {string} [props.loginUrl=''] - Login URL to display and copy
 * @returns {JSX.Element} Loading screen component
 */
const LoadingScreen = ({ title = 'common.loading', size = 'small', loginUrl = '' }) => {
  const { t } = useTranslation();

  const handleCopyUrl = async () => {
    if (!loginUrl) { return null }

    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(loginUrl);
        message.success(t('common.copySuccess'));
      } else {
        const textArea = document.createElement('textarea');
        textArea.value = loginUrl;
        textArea.style.position = 'fixed';
        textArea.style.opacity = '0';
        document.body.appendChild(textArea);
        textArea.select();
        document.execCommand('copy');
        document.body.removeChild(textArea);
        console.log('Copied to clipboard:', loginUrl);
        message.success(t('common.copySuccess'));
      }
    } catch (error) {
      console.error('Failed to copy URL:', error);
      message.error(t('common.copyFailed'));
    }
  };

  return (
    <div className={styles.loadingScreen}>
      <div className={styles.loadingContent}>
        <Spin size={size} />
        {title && <div className={styles.loadingMessage}>{t(title)}</div>}
        {loginUrl && (
          <div className={styles.loginUrlContainer}>
            <div className={styles.loginUrlTip}>
              {t('common.loginUrlTip')}
            </div>
            <div className={styles.loginUrlLabel}>
              {t('common.loginUrl')}
            </div>
            <div className={styles.loginUrlContent}>
              <a
                href={loginUrl}
                target="_blank"
                rel="noopener noreferrer"
                className={styles.loginUrlLink}
              >
                {loginUrl}
              </a>
              <button
                onClick={handleCopyUrl}
                className={styles.copyButton}
                title={t('common.copy')}
              >
                <CopyOutlined />
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default LoadingScreen;
