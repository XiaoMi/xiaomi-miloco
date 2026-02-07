/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import React, { useState, useEffect } from 'react';
import {Select, Switch, Button, Form, Input, Modal, message, Divider, Space, Typography, Segmented} from 'antd';
import { useTranslation } from 'react-i18next';
import { SettingOutlined, GlobalOutlined, BulbOutlined, KeyOutlined, ToolOutlined } from '@ant-design/icons';
import { setHAAuth, getHAAuth, getLanguage, setLanguage, getHAWsStatus } from '@/api';
import { useTheme } from '@/contexts/ThemeContext';
import { useSettingStore } from '@/stores/settingStore';
import { Card, Header } from '@/components';
import XiaoAISetting from './components/XiaoAISetting';
import styles from './index.module.less';

const { Title, Text } = Typography;
const { Option } = Select;

/**
 * Setting Page - Application settings page for language, theme, and authorization configuration
 * 设置页面 - 用于语言、主题和授权配置的应用设置页面
 *
 * @returns {JSX.Element} Settings page component
 */
const Setting = () => {
  const { i18n, t } = useTranslation();
  const { themeMode, changeTheme } = useTheme();
  const {
    language: storeLanguage,
    themeMode: storeThemeMode,
    setLanguage: setStoreLanguage,
    setThemeMode: setStoreThemeMode
  } = useSettingStore();
  const [form] = Form.useForm();
  const [haModalVisible, setHaModalVisible] = useState(false);
  const [haFormValues, setHaFormValues] = useState({
    base_url: '',
    token: ''
  });
  const [haWsStatus, setHaWsStatus] = useState({ configured: false, connected: false });


  // language options
  const languageOptions = [
    { key: 'zh', label: '简体中文' },
    { key: 'en', label: 'English' },
  ];

  // theme mode options
  const themeOptions = [
    { key: 'light', label: t('setting.lightMode'), icon: '☀️' },
    { key: 'dark', label: t('setting.darkMode'), icon: '🌙' },
    { key: 'system', label: t('setting.systemMode'), icon: '🔄' },
  ];


  useEffect(() => {
    const fetchServerLanguage = async () => {
      try {
        const res = await getLanguage();
        if (res && res?.code === 0) {
          const serverLanguage = res?.data?.language;
          if (serverLanguage && serverLanguage !== i18n.language) {
            setStoreLanguage(serverLanguage);
            i18n.changeLanguage(serverLanguage);
          }
        }
      } catch (error) {
        console.warn('Failed to get server language setting:', error);
        if (storeLanguage && storeLanguage !== i18n.language) {
          i18n.changeLanguage(storeLanguage);
        }
      }
    };
    fetchServerLanguage();
  }, []); 

  useEffect(() => {
    if (storeLanguage && storeLanguage !== i18n.language) {
      i18n.changeLanguage(storeLanguage);
    }
  }, [storeLanguage, i18n]);

  // get Home Assistant authorization information and WebSocket status
  useEffect(() => {
    const fetchHAAuth = async () => {
      try {
        const res = await getHAAuth();
        if (res && res?.code === 0) {
          setHaFormValues(res?.data || {});
        }
      } catch (error) {
        console.error(t('setting.getHAAuthFailed'), error);
      }
    };
    
    const fetchHAWsStatus = async () => {
      try {
        const res = await getHAWsStatus();
        if (res && res?.code === 0) {
          setHaWsStatus(res?.data || { configured: false, connected: false });
        }
      } catch (error) {
        console.error('Failed to get HA WebSocket status:', error);
      }
    };
    
    fetchHAAuth();
    fetchHAWsStatus();
    
    // 定期刷新 WebSocket 状态
    const interval = setInterval(fetchHAWsStatus, 5000);
    return () => clearInterval(interval);
  }, []);


  // handle language change
  const handleLanguageChange = async (value) => {
    try {
      setStoreLanguage(value);
      i18n.changeLanguage(value);

      const res = await setLanguage({ language: value });
      if (res && res?.code === 0) {
        const languageName = languageOptions.find(opt => opt.key === value)?.label;
        message.success(`${t('setting.languageChanged')} ${languageName}`);
      } else {
        message.error(res?.message || t('setting.languageChangeFailed'));
      }
    } catch (error) {
      console.error('Failed to change language:', error);
      message.error(t('setting.languageChangeFailed'));
    }
  };

  // handle theme mode change
  const handleThemeChange = (value) => {
    setStoreThemeMode(value);
    changeTheme(value);
    message.success(`${t('setting.themeChanged')} ${themeOptions.find(opt => opt.key === value)?.label} ${t('setting.mode')}`);
  };

  // handle Home Assistant authorization configuration
  const handleHaAuthConfig = () => {
    setHaModalVisible(true);
  };

  // handle Home Assistant authorization confirm
  const handleHaAuthConfirm = async () => {
    try {
      const values = await form.validateFields();
      const res = await setHAAuth(values);
      if (res && res?.code === 0) {
        message.success(t('setting.haAuthConfigSavedSuccess'));
        setHaModalVisible(false);
        form.resetFields();
        setHaFormValues(values);
      } else {
        message.error(res?.message || t('setting.haAuthConfigSavedFailed'));
      }
    } catch (error) {
      console.error('handleHaAuthConfirm failed:', error);
    }
  };

  // handle Home Assistant authorization cancel
  const handleHaAuthCancel = () => {
    setHaModalVisible(false);
    form.resetFields();
  };


  return (
    <div className={styles.settingContainer}>
      <div className={styles.settingContent}>
        <Header title={t('home.menu.setting')} />

        {/* regular setting */}
        <Card className={styles.settingCard} contentClassName={styles.settingCardContent}>
          <div className={styles.settingCardTitle}>{t('setting.regularSetting')}</div>
          <div className={styles.settingCardItemList}>
          <div className={styles.settingItem}>
            <div className={styles.settingLabel}>
              <GlobalOutlined /> {t('setting.language')}
            </div>
            <Select
              value={storeLanguage || i18n.language}
              onChange={handleLanguageChange}
              style={{ width: 382 }}
              placeholder={t('setting.pleaseSelectLanguage')}
            >
              {languageOptions.map(option => (
                <Option key={option.key} value={option.key}>
                  {option.label}
                </Option>
              ))}
            </Select>
          </div>

          <div className={styles.settingItem}>
            <div className={styles.settingLabel}>
              <BulbOutlined /> {t('setting.themeMode')}
            </div>
            <Segmented
              value={storeThemeMode || themeMode}
              onChange={handleThemeChange}
              options={themeOptions.map(option => ({
                label: (
                  <div className={styles.segmentedOption}>
                    {/* <span className={styles.segmentedIcon}>{option.icon}</span> */}
                    <span>{option.label}</span>
                  </div>
                ),
                value: option.key
              }))}
              className={styles.themeSegmented}
            />
          </div>
          </div>
        </Card>

        {/* advanced setting */}
        <Card className={styles.settingCard} contentClassName={styles.settingCardContent}>
          <div className={styles.settingCardTitle}>{t('setting.authorizationSetting')}</div>
          <div className={styles.settingCardItemList}>
          <div className={styles.settingItem}>
            <div className={styles.settingLabel}>
              <KeyOutlined /> {t('setting.miHomeAuthorization')}
            </div>
            <Space>
              <Button>{t('setting.configured')}</Button>
            </Space>
          </div>

          <div className={styles.settingItem}>
            <div className={styles.settingLabel}>
              <KeyOutlined /> {t('setting.homeAssistantAuthorization')}
            </div>
            <Space>
              {haFormValues?.base_url && (
                <span className={haWsStatus.connected ? styles.wsConnected : styles.wsDisconnected}>
                  {haWsStatus.connected ? t('setting.wsConnected') : t('setting.wsDisconnected')}
                </span>
              )}
              <Button onClick={handleHaAuthConfig}>
                {haFormValues?.base_url ? t('setting.configured') : t('setting.configure')}
              </Button>
            </Space>
            </div>
          </div>
        </Card>

        {/* XiaoAI Speaker Settings */}
        <XiaoAISetting />

      </div>

      {/* Home Assistant authorization configuration modal */}
      <Modal
        title={t('setting.homeAssistantAuthorizationConfig')}
        open={haModalVisible}
        onOk={handleHaAuthConfirm}
        onCancel={handleHaAuthCancel}
        okText={t('common.confirm')}
        cancelText={t('common.cancel')}
      >
        <Form
          form={form}
          layout="vertical"
          initialValues={haFormValues}
        >
          <Form.Item
            name="base_url"
            label="URL"
            rules={[
              { required: true, message: t('setting.pleaseEnterUrl') },
              { type: 'url', message: t('setting.pleaseEnterValidUrl') }
            ]}
          >
            <Input placeholder={t('setting.pleaseEnterHomeAssistantUrl')} />
          </Form.Item>
          <Form.Item
            name="token"
            label="Token"
            rules={[
              { required: true, message: t('setting.pleaseEnterToken') }
            ]}
          >
            <Input.Password placeholder={t('setting.pleaseEnterHomeAssistantToken')} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default Setting;
