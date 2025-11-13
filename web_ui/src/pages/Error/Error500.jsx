/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import React from 'react';
import { Button, Result } from 'antd';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

/**
 * Error500 Component - 500 server error page with reload functionality
 * 500错误页面组件 - 带有重新加载功能的500服务器错误页面
 *
 * @returns {JSX.Element} Error 500 page component
 */
const Error500 = () => {
  const navigate = useNavigate();
  const { t } = useTranslation();

  const handleReload = () => {
    navigate('/');
  };

  return (
    <div style={{ height: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', background: 'var(--bg-color)' }}>
      <Result
        status="500"
        title="500"
        subTitle={t('common.serverError')}
        extra={<Button type="primary" onClick={handleReload}>{t('common.retry')}</Button>}
      />
    </div>
  );
};

export default Error500;
