/**
 * Copyright (C) 2025 willianfu
 * 小爱音箱设置组件
 */

import React, { useState, useEffect, useCallback } from 'react';
import { 
  Form, Input, Switch, Button, Select, InputNumber, 
  Space, message, Tooltip, Collapse, Tag, Table, Modal, Checkbox
} from 'antd';
import { useTranslation } from 'react-i18next';
import { 
  SoundOutlined, ReloadOutlined, PlayCircleOutlined, 
  PauseCircleOutlined, QuestionCircleOutlined,
  SettingOutlined, RobotOutlined, MessageOutlined
} from '@ant-design/icons';
import { Card } from '@/components';
import { 
  getXiaoAIStatus, getXiaoAIConfig, updateXiaoAIConfig,
  startXiaoAIService, stopXiaoAIService, restartXiaoAIService,
  getMCPService, getCameraList, getHAAuth
} from '@/api';
import styles from './index.module.less';

const { TextArea } = Input;
const { Option, OptGroup } = Select;
const { Panel } = Collapse;

// 内置MCP客户端ID（与后端 LocalMcpClientId 保持一致）
const BUILTIN_MCP = {
  MIOT_MANUAL_SCENES: { id: 'miot_manual_scenes', nameKey: 'xiaoai.miotManualScenes', requiresHA: false },
  MIOT_DEVICES: { id: 'miot_devices', nameKey: 'xiaoai.miotDevices', requiresHA: false },
  HA_AUTOMATIONS: { id: 'ha_automations', nameKey: 'xiaoai.haAutomations', requiresHA: true },
  HA_DEVICES: { id: 'ha_devices', nameKey: 'xiaoai.haDevices', requiresHA: true },
};

/**
 * 小爱音箱设置组件
 */
const XiaoAISetting = () => {
  const { t } = useTranslation();
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState({ running: false, enabled: false, connected_speakers: [] });
  const [mcpServices, setMcpServices] = useState([]);
  const [cameras, setCameras] = useState([]);
  const [configModalVisible, setConfigModalVisible] = useState(false);
  const [needsRestart, setNeedsRestart] = useState(false);
  const [haConfigured, setHaConfigured] = useState(false);
  const [cameraAutoSelect, setCameraAutoSelect] = useState(true);

  // 获取状态和配置
  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      // 获取服务状态
      const statusRes = await getXiaoAIStatus();
      if (statusRes?.code === 0) {
        setStatus(statusRes.data);
      }

      // 获取配置
      const configRes = await getXiaoAIConfig();
      if (configRes?.code === 0) {
        const config = configRes.data;
        const cameraIds = config.camera_ids || [];
        // 检查是否为自动选择（空数组表示自动选择）
        const isAutoSelect = cameraIds.length === 0;
        setCameraAutoSelect(isAutoSelect);
        
        form.setFieldsValue({
          enabled: config.enabled ?? false,
          host: config.host || '0.0.0.0',
          port: config.port ?? 4399,
          mcp_list: config.mcp_list || [],
          camera_ids: cameraIds,
          system_prompt: config.system_prompt || '',
          history_max_length: config.history_max_length ?? 20,
          call_ai_keywords: (config.call_ai_keywords || []).join('\n'),
          tts_max_length: config.tts_max_length ?? 120,
          playback_timeout: config.playback_timeout ?? 600,
          enable_interruption: config.enable_interruption ?? true,
          connection_announcement: config.connection_announcement || '已连接',
          clear_commands: (config.session_commands?.clear_commands || []).join('\n'),
          save_and_new_commands: (config.session_commands?.save_and_new_commands || []).join('\n'),
          share_session_with_web: config.share_session_with_web ?? false,
          compression_enabled: config.context_compression?.enabled ?? true,
          max_messages: config.context_compression?.max_messages ?? 20,
          max_tokens: config.context_compression?.max_tokens ?? 8000,
          compression_strategy: config.context_compression?.strategy || 'auto',
          keep_recent: config.context_compression?.keep_recent ?? 5,
          play_thinking: config.tts_playback?.play_thinking ?? false,
          play_tool_calls: config.tts_playback?.play_tool_calls ?? false,
          auto_save_session: config.auto_save_session ?? false,
          // 全部接管模式
          takeover_enabled: config.takeover_mode?.enabled ?? false,
          takeover_enter_keywords: (config.takeover_mode?.enter_keywords || []).join('\n'),
          takeover_exit_keywords: (config.takeover_mode?.exit_keywords || []).join('\n'),
        });
      }

      // 获取MCP服务列表
      const mcpRes = await getMCPService();
      if (mcpRes?.code === 0 && mcpRes?.data) {
        const { configs = [] } = mcpRes.data;
        setMcpServices(Array.isArray(configs) ? configs : []);
      }

      // 获取摄像头列表
      const cameraRes = await getCameraList();
      if (cameraRes?.code === 0) {
        setCameras(Array.isArray(cameraRes.data) ? cameraRes.data : []);
      }

      // 检查HA配置状态
      const haRes = await getHAAuth();
      if (haRes?.code === 0 && haRes?.data?.base_url) {
        setHaConfigured(true);
      } else {
        setHaConfigured(false);
      }
    } catch (error) {
      console.error('获取小爱音箱数据失败:', error);
      message.error(t('xiaoai.loadConfigFailed'));
    } finally {
      setLoading(false);
    }
  }, [form, t]);

  useEffect(() => {
    fetchData();
    
    // 每5秒刷新一次状态
    const interval = setInterval(async () => {
      try {
        const statusRes = await getXiaoAIStatus();
        if (statusRes?.code === 0) {
          setStatus(statusRes.data);
        }
      } catch (error) {
        // 状态刷新失败时静默处理
      }
    }, 5000);

    return () => clearInterval(interval);
  }, [fetchData]);

  // 服务控制处理
  const handleStart = async () => {
    try {
      const res = await startXiaoAIService();
      if (res?.code === 0) {
        message.success(t('xiaoai.startSuccess'));
        fetchData();
      } else {
        message.error(res?.message || t('xiaoai.operationFailed'));
      }
    } catch (error) {
      message.error(t('xiaoai.operationFailed'));
    }
  };

  const handleStop = async () => {
    try {
      const res = await stopXiaoAIService();
      if (res?.code === 0) {
        message.success(t('xiaoai.stopSuccess'));
        fetchData();
      } else {
        message.error(res?.message || t('xiaoai.operationFailed'));
      }
    } catch (error) {
      message.error(t('xiaoai.operationFailed'));
    }
  };

  const handleRestart = async () => {
    try {
      const res = await restartXiaoAIService();
      if (res?.code === 0) {
        message.success(t('xiaoai.restartSuccess'));
        setNeedsRestart(false);
        fetchData();
      } else {
        message.error(res?.message || t('xiaoai.operationFailed'));
      }
    } catch (error) {
      message.error(t('xiaoai.operationFailed'));
    }
  };

  // 处理摄像头自动选择切换
  const handleCameraAutoSelectChange = (checked) => {
    setCameraAutoSelect(checked);
    if (checked) {
      form.setFieldValue('camera_ids', []);
    }
  };

  // 保存配置
  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      setSaving(true);

      // 如果启用自动选择，camera_ids 应为空数组
      const cameraIds = cameraAutoSelect ? [] : (values.camera_ids || []);

      const config = {
        enabled: values.enabled,
        host: values.host,
        port: values.port,
        mcp_list: values.mcp_list || [],
        camera_ids: cameraIds,
        system_prompt: values.system_prompt || null,
        history_max_length: values.history_max_length,
        call_ai_keywords: (values.call_ai_keywords || '').split('\n').filter(s => s.trim()),
        tts_max_length: values.tts_max_length,
        playback_timeout: values.playback_timeout,
        enable_interruption: values.enable_interruption,
        connection_announcement: values.connection_announcement,
        session_commands: {
          clear_commands: (values.clear_commands || '').split('\n').filter(s => s.trim()),
          save_and_new_commands: (values.save_and_new_commands || '').split('\n').filter(s => s.trim()),
        },
        share_session_with_web: values.share_session_with_web,
        context_compression: {
          enabled: values.compression_enabled,
          max_messages: values.max_messages,
          max_tokens: values.max_tokens,
          strategy: values.compression_strategy,
          keep_recent: values.keep_recent,
        },
        tts_playback: {
          play_thinking: values.play_thinking || false,
          play_tool_calls: values.play_tool_calls || false,
        },
        auto_save_session: values.auto_save_session || false,
        takeover_mode: {
          enabled: values.takeover_enabled || false,
          enter_keywords: (values.takeover_enter_keywords || '').split('\n').filter(s => s.trim()),
          exit_keywords: (values.takeover_exit_keywords || '').split('\n').filter(s => s.trim()),
        },
      };

      const res = await updateXiaoAIConfig(config);
      if (res?.code === 0) {
        message.success(t('xiaoai.saveSuccess'));
        if (res.data?.needs_restart) {
          setNeedsRestart(true);
          message.info(t('xiaoai.needsRestart'));
        }
        setConfigModalVisible(false);
      } else {
        message.error(res?.message || t('xiaoai.saveFailed'));
      }
    } catch (error) {
      console.error('保存配置失败:', error);
      message.error(t('xiaoai.saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  // 获取内置MCP选项（根据HA配置状态过滤）
  const getBuiltinMcpOptions = () => {
    const options = [
      BUILTIN_MCP.MIOT_MANUAL_SCENES,
      BUILTIN_MCP.MIOT_DEVICES,
    ];
    if (haConfigured) {
      options.push(BUILTIN_MCP.HA_AUTOMATIONS);
      options.push(BUILTIN_MCP.HA_DEVICES);
    }
    return options;
  };

  // 音箱状态表格列配置
  const speakerColumns = [
    {
      title: t('xiaoai.speakerId'),
      dataIndex: 'speaker_id',
      key: 'speaker_id',
      ellipsis: true,
    },
    {
      title: t('xiaoai.speakerModel'),
      dataIndex: 'model',
      key: 'model',
    },
    {
      title: t('xiaoai.speakerStatus'),
      dataIndex: 'status',
      key: 'status',
      render: (status) => {
        const statusMap = {
          playing: { color: 'processing', text: t('xiaoai.playing') },
          paused: { color: 'warning', text: t('xiaoai.paused') },
          idle: { color: 'default', text: t('xiaoai.idle') },
        };
        const s = statusMap[status] || { color: 'default', text: status };
        return <Tag color={s.color}>{s.text}</Tag>;
      },
    },
  ];

  return (
    <>
      <Card className={styles.settingCard} contentClassName={styles.settingCardContent}>
        <div className={styles.settingCardTitle}>
          <SoundOutlined style={{ marginRight: 8 }} />
          {t('xiaoai.title')}
        </div>
        
        <div className={styles.settingCardItemList}>
          {/* 服务状态 */}
          <div className={styles.settingItem}>
            <div className={styles.settingLabel}>
              {t('xiaoai.serviceStatus')}
            </div>
            <Space>
              <span className={status.running ? styles.wsConnected : styles.wsDisconnected}>
                {status.running ? t('xiaoai.running') : t('xiaoai.stopped')}
              </span>
              {status.running ? (
                <Button onClick={handleStop} danger>
                  {t('xiaoai.stopService')}
                </Button>
              ) : (
                <Button type="primary" onClick={handleStart}>
                  {t('xiaoai.startService')}
                </Button>
              )}
              {needsRestart && (
                <Button icon={<ReloadOutlined />} onClick={handleRestart}>
                  {t('xiaoai.restartService')}
                </Button>
              )}
            </Space>
          </div>

          {/* 已连接音箱 */}
          <div className={styles.settingItemVertical}>
            <div className={styles.settingLabel}>
              {t('xiaoai.connectedSpeakers')}
              <span className={styles.speakerCount}>
                ({status.connected_speakers?.length || 0})
              </span>
            </div>
            {status.connected_speakers?.length > 0 ? (
              <Table
                columns={speakerColumns}
                dataSource={status.connected_speakers}
                rowKey="speaker_id"
                size="small"
                pagination={false}
                className={styles.speakerTable}
              />
            ) : (
              <div className={styles.noSpeakers}>{t('xiaoai.noSpeakers')}</div>
            )}
          </div>

          {/* 配置按钮 */}
          <div className={styles.settingItem}>
            <div className={styles.settingLabel}>
              <SettingOutlined />
              {t('xiaoai.serverConfig')}
            </div>
            <Button onClick={() => setConfigModalVisible(true)}>
              {t('setting.configure')}
            </Button>
          </div>
        </div>
      </Card>

      {/* 配置弹窗 */}
      <Modal
        title={
          <span>
            <SoundOutlined style={{ marginRight: 8 }} />
            {t('xiaoai.title')} - {t('xiaoai.serverConfig')}
          </span>
        }
        open={configModalVisible}
        onCancel={() => setConfigModalVisible(false)}
        width={700}
        footer={[
          <Button key="cancel" onClick={() => setConfigModalVisible(false)}>
            {t('common.cancel')}
          </Button>,
          <Button key="save" type="primary" loading={saving} onClick={handleSave}>
            {t('xiaoai.saveConfig')}
          </Button>,
        ]}
      >
        <Form
          form={form}
          layout="vertical"
          className={styles.configForm}
        >
          <Collapse defaultActiveKey={['basic', 'ai']} bordered={false}>
            {/* 基础设置 */}
            <Panel header={t('xiaoai.serverConfig')} key="basic">
              <Form.Item
                name="enabled"
                label={t('xiaoai.enabled')}
                valuePropName="checked"
                tooltip={t('xiaoai.enabledTip')}
              >
                <Switch />
              </Form.Item>

              <Space style={{ width: '100%' }} align="start">
                <Form.Item name="host" label={t('xiaoai.host')} style={{ width: 200 }}>
                  <Input placeholder="0.0.0.0" />
                </Form.Item>
                <Form.Item 
                  name="port" 
                  label={t('xiaoai.port')}
                  tooltip={t('xiaoai.portTip')}
                >
                  <InputNumber min={1} max={65535} />
                </Form.Item>
              </Space>

              <Form.Item
                name="connection_announcement"
                label={t('xiaoai.connectionAnnouncement')}
              >
                <Input placeholder={t('xiaoai.connectionAnnouncementPlaceholder')} />
              </Form.Item>
            </Panel>

            {/* AI 配置 */}
            <Panel header={t('xiaoai.aiConfig')} key="ai">
              <Form.Item
                name="mcp_list"
                label={t('xiaoai.mcpServices')}
                tooltip={t('xiaoai.mcpServicesTip')}
              >
                <Select mode="multiple" placeholder={t('xiaoai.mcpServicesTip')}>
                  <OptGroup label={t('xiaoai.builtinMcp')}>
                    {getBuiltinMcpOptions().map(mcp => (
                      <Option key={mcp.id} value={mcp.id}>
                        <Tag color="blue" style={{ marginRight: 4 }}>{t('xiaoai.builtin')}</Tag>
                        {t(mcp.nameKey)}
                      </Option>
                    ))}
                  </OptGroup>
                  {(mcpServices || []).length > 0 && (
                    <OptGroup label={t('xiaoai.customMcp')}>
                      {(mcpServices || []).map(mcp => (
                        <Option key={mcp.id} value={mcp.id}>{mcp.name}</Option>
                      ))}
                    </OptGroup>
                  )}
                </Select>
              </Form.Item>

              <Form.Item
                label={t('xiaoai.cameras')}
                tooltip={t('xiaoai.camerasTip')}
              >
                <div className={styles.cameraSelectContainer}>
                  <Checkbox
                    checked={cameraAutoSelect}
                    onChange={(e) => handleCameraAutoSelectChange(e.target.checked)}
                    style={{ marginBottom: 8 }}
                  >
                    {t('common.autoSelect')}
                  </Checkbox>
                  {!cameraAutoSelect && (
                    <Form.Item name="camera_ids" noStyle>
                      <Select mode="multiple" placeholder={t('xiaoai.camerasTip')}>
                        {(cameras || []).map(cam => (
                          <Option key={cam.did} value={cam.did}>{cam.name || cam.did}</Option>
                        ))}
                      </Select>
                    </Form.Item>
                  )}
                </div>
              </Form.Item>

              <Form.Item
                name="system_prompt"
                label={t('xiaoai.systemPrompt')}
              >
                <TextArea 
                  rows={3} 
                  placeholder={t('xiaoai.systemPromptPlaceholder')} 
                />
              </Form.Item>

              <Form.Item
                name="history_max_length"
                label={t('xiaoai.historyLength')}
                tooltip={t('xiaoai.historyLengthTip')}
              >
                <InputNumber min={1} max={100} />
              </Form.Item>
            </Panel>

            {/* 语音控制配置 */}
            <Panel header={t('xiaoai.voiceConfig')} key="voice">
              {/* 全部接管模式设置 */}
              <div className={styles.takeoverSection}>
                <Form.Item
                  name="takeover_enabled"
                  label={t('xiaoai.takeoverEnabled')}
                  valuePropName="checked"
                  tooltip={t('xiaoai.takeoverEnabledTip')}
                >
                  <Switch />
                </Form.Item>

                <Form.Item
                  noStyle
                  shouldUpdate={(prevValues, currentValues) => 
                    prevValues.takeover_enabled !== currentValues.takeover_enabled
                  }
                >
                  {({ getFieldValue }) => 
                    getFieldValue('takeover_enabled') ? (
                      <>
                        <Form.Item
                          name="takeover_enter_keywords"
                          label={t('xiaoai.takeoverEnterKeywords')}
                          tooltip={t('xiaoai.takeoverEnterKeywordsTip')}
                        >
                          <TextArea 
                            rows={2} 
                            placeholder={t('xiaoai.takeoverEnterKeywordsPlaceholder')} 
                          />
                        </Form.Item>

                        <Form.Item
                          name="takeover_exit_keywords"
                          label={t('xiaoai.takeoverExitKeywords')}
                          tooltip={t('xiaoai.takeoverExitKeywordsTip')}
                        >
                          <TextArea 
                            rows={2} 
                            placeholder={t('xiaoai.takeoverExitKeywordsPlaceholder')} 
                          />
                        </Form.Item>
                      </>
                    ) : null
                  }
                </Form.Item>
              </div>

              {/* 关键词触发设置（非全部接管模式使用） */}
              <Form.Item
                name="call_ai_keywords"
                label={t('xiaoai.aiKeywords')}
                tooltip={t('xiaoai.aiKeywordsTip')}
              >
                <TextArea 
                  rows={3} 
                  placeholder={t('xiaoai.aiKeywordsPlaceholder')} 
                />
              </Form.Item>

              <Space style={{ width: '100%' }}>
                <Form.Item
                  name="tts_max_length"
                  label={t('xiaoai.ttsMaxLength')}
                  tooltip={t('xiaoai.ttsMaxLengthTip')}
                >
                  <InputNumber min={50} max={500} />
                </Form.Item>

                <Form.Item
                  name="playback_timeout"
                  label={t('xiaoai.playbackTimeout')}
                >
                  <InputNumber min={60} max={3600} />
                </Form.Item>
              </Space>

              <Form.Item
                name="enable_interruption"
                label={t('xiaoai.enableInterruption')}
                valuePropName="checked"
                tooltip={t('xiaoai.enableInterruptionTip')}
              >
                <Switch />
              </Form.Item>
            </Panel>

            {/* TTS播报控制 */}
            <Panel header={t('xiaoai.ttsPlaybackConfig')} key="tts_playback">
              <Form.Item
                name="play_thinking"
                label={t('xiaoai.playThinking')}
                valuePropName="checked"
                tooltip={t('xiaoai.playThinkingTip')}
              >
                <Switch />
              </Form.Item>

              <Form.Item
                name="play_tool_calls"
                label={t('xiaoai.playToolCalls')}
                valuePropName="checked"
                tooltip={t('xiaoai.playToolCallsTip')}
              >
                <Switch />
              </Form.Item>
            </Panel>

            {/* 会话管理 */}
            <Panel header={t('xiaoai.sessionConfig')} key="session">
              <Form.Item
                name="clear_commands"
                label={t('xiaoai.clearCommands')}
                tooltip={t('xiaoai.clearCommandsTip')}
              >
                <TextArea 
                  rows={2} 
                  placeholder={t('xiaoai.clearCommandsPlaceholder')} 
                />
              </Form.Item>

              <Form.Item
                name="save_and_new_commands"
                label={t('xiaoai.saveAndNewCommands')}
                tooltip={t('xiaoai.saveAndNewCommandsTip')}
              >
                <TextArea 
                  rows={2} 
                  placeholder={t('xiaoai.saveAndNewCommandsPlaceholder')} 
                />
              </Form.Item>

              <Form.Item
                name="share_session_with_web"
                label={t('xiaoai.shareSessionWithWeb')}
                valuePropName="checked"
                tooltip={t('xiaoai.shareSessionWithWebTip')}
              >
                <Switch />
              </Form.Item>

              <Form.Item
                name="auto_save_session"
                label={t('xiaoai.autoSaveSession')}
                valuePropName="checked"
                tooltip={t('xiaoai.autoSaveSessionTip')}
              >
                <Switch />
              </Form.Item>
            </Panel>

            {/* 上下文压缩 */}
            <Panel header={t('xiaoai.contextCompression')} key="compression">
              <Form.Item
                name="compression_enabled"
                label={t('xiaoai.compressionEnabled')}
                valuePropName="checked"
                tooltip={t('xiaoai.compressionEnabledTip')}
              >
                <Switch />
              </Form.Item>

              <Space style={{ width: '100%' }}>
                <Form.Item
                  name="max_messages"
                  label={t('xiaoai.maxMessages')}
                  tooltip={t('xiaoai.maxMessagesTip')}
                >
                  <InputNumber min={5} max={100} />
                </Form.Item>

                <Form.Item
                  name="max_tokens"
                  label={t('xiaoai.maxTokens')}
                  tooltip={t('xiaoai.maxTokensTip')}
                >
                  <InputNumber min={1000} max={100000} step={1000} />
                </Form.Item>
              </Space>

              <Space style={{ width: '100%' }}>
                <Form.Item
                  name="compression_strategy"
                  label={t('xiaoai.compressionStrategy')}
                  style={{ width: 200 }}
                >
                  <Select>
                    <Option value="auto">{t('xiaoai.strategyAuto')}</Option>
                    <Option value="summary">{t('xiaoai.strategySummary')}</Option>
                    <Option value="truncate">{t('xiaoai.strategyTruncate')}</Option>
                    <Option value="sliding">{t('xiaoai.strategySliding')}</Option>
                  </Select>
                </Form.Item>

                <Form.Item
                  name="keep_recent"
                  label={t('xiaoai.keepRecent')}
                  tooltip={t('xiaoai.keepRecentTip')}
                >
                  <InputNumber min={1} max={20} />
                </Form.Item>
              </Space>
            </Panel>
          </Collapse>
        </Form>
      </Modal>
    </>
  );
};

export default XiaoAISetting;
