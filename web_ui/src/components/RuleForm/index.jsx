/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import React, { useState, useEffect } from 'react';
import { Select, Input, Button, Checkbox, Form, Tooltip, Spin, message, Switch } from 'antd';
import { QuestionCircleOutlined, ReloadOutlined, UpOutlined, DownOutlined, InfoCircleOutlined } from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import TimeSelector from '@/components/TimeSelector';
import {
  TRIGGER_PERIOD_OPTIONS,
  TRIGGER_INTERVAL_OPTIONS,
  formDataUtils,
} from '@/utils/ruleFormUtils';
import { useRuleFormData, convertFormDataToBackend } from '@/hooks/useRuleFormData';
import { useRuleFormActions } from '@/hooks/useRuleFormActions';
import { useLogViewerStore } from '@/stores/logViewerStore';
import styles from './index.module.less';
import { classNames } from '@/utils';
import { useChatStore } from '@/stores/chatStore';
import SelectTagRender from './selectTagRender';

/**
 * RuleForm Component - Unified form component for creating and editing smart automation rules
 * 规则表单组件 - 用于创建和编辑智能自动化规则的统一表单组件
 *
 * @param {Object} props - Component props
 * @param {string} [props.mode='create'] - Form mode: 'create' | 'edit' | 'queryEdit' | 'readonly'
 * @param {Object} [props.initialRule] - Initial rule data (for edit/queryEdit/readonly modes)
 * @param {Function} props.onSubmit - Submit callback function
 * @param {boolean} [props.loading=false] - Loading state for submit button
 * @param {Function} [props.onCancel] - Cancel callback function
 * @param {Array} [props.cameraOptions=[]] - Available camera options
 * @param {Array} [props.actionOptions=[]] - Available action options
 * @param {boolean} [props.enableCameraRefresh=false] - Whether to enable camera refresh
 * @param {Function} [props.onRefreshCameras] - Camera refresh callback function
 * @param {boolean} [props.enableActionRefresh=false] - Whether to enable action refresh
 * @param {Function} [props.onRefreshActions] - Action refresh callback function
 * @param {boolean} [props.cameraLoading=false] - Camera loading state
 * @param {boolean} [props.actionLoading=false] - Action loading state
 * @returns {JSX.Element} Rule form component
 */
const RuleForm = ({
  mode = 'create',
  initialRule = null,
  onSubmit,
  loading = false,
  onCancel,
  cameraOptions = [],
  actionOptions = [],
  enableCameraRefresh = false,
  onRefreshCameras,
  enableActionRefresh = false,
  onRefreshActions,
  cameraLoading = false,
  actionLoading = false,
}) => {
  const { t } = useTranslation();
  const [form] = Form.useForm();
  const { openModal } = useLogViewerStore();
  const { availableMcpServices } = useChatStore();

  const formData = useRuleFormData(initialRule);
  const { aiGeneratedActions, setAiGeneratedActions } = useLogViewerStore();

  const {
    groupedOptions,
    selectedActionKeys: initialSelectedKeys,
    selectedActionObjects,
  } = useRuleFormActions(actionOptions, formData?.automation_actions || []);

  const [selectedActions, setSelectedActions] = useState([]);
  const [sendNotification, setSendNotification] = useState(false);
  const [notificationText, setNotificationText] = useState('');
  const [useTemplateNotification, setUseTemplateNotification] = useState(false);

  const [checkedMcpServices, setCheckedMcpServices] = useState([]);
  const [aiRecommendExecuteType, setAiRecommendExecuteType] = useState('dynamic');
  const [aiRecommendActionDescriptions, setAiRecommendActionDescriptions] = useState([]);
  const [aiRecommendActions, setAiRecommendActions] = useState([]);
  const [actionDescriptionError, setActionDescriptionError] = useState(false);

  const [advancedOptionsVisible, setAdvancedOptionsVisible] = useState(false);
  const [triggerPeriod, setTriggerPeriod] = useState('all_day');
  const [triggerIntervalHours, setTriggerIntervalHours] = useState(0);
  const [triggerIntervalMinutes, setTriggerIntervalMinutes] = useState(0);
  const [triggerIntervalSeconds, setTriggerIntervalSeconds] = useState(2);

  useEffect(() => {
    if (mode === 'readonly') {
      return;
    }
    setAiRecommendActions(aiGeneratedActions);
    setAiRecommendExecuteType(aiGeneratedActions.length > 0 ? 'static' : 'dynamic');
  }, [aiGeneratedActions]);

  useEffect(() => {
    if (mode === 'create') {
      setAiGeneratedActions([]);
    }
    if (mode !== 'create' && formData) {
      form.setFieldsValue({
        name: formData.name,
        condition: formData.condition,
        cameras: formData.cameras?.map(camera =>
          typeof camera === 'object' ? camera.did : camera
        ) || [],
      });

      if (initialSelectedKeys && initialSelectedKeys.length > 0) {
        setSelectedActions(initialSelectedKeys);
      } else {
        const actionKeys = selectedActionObjects.map(action => {
          const serverName = action.mcp_server_name || 'unknown';
          return `${serverName}#${action.introduction}`;
        });
        setSelectedActions(actionKeys);
      }

      if (formData.notify?.content) {
        setSendNotification(true);
        setNotificationText(formData.notify.content);
        setUseTemplateNotification(formData.notify.use_template || false);
      }

      setCheckedMcpServices(formData.mcp_list?.map(mcp => `${mcp?.server_name}#${mcp?.client_id}`) || []);
      setAiRecommendExecuteType(formData.ai_recommend_execute_type || 'static');
      setAiRecommendActionDescriptions(formData.ai_recommend_action_descriptions || []);
      setAiRecommendActions(formData.ai_recommend_actions || []);
      if(formData?.ai_recommend_actions?.length === 0) {
        setAiGeneratedActions([]);
      }
      if (formData.filter) {
        const filterData = formDataUtils.toFormFormat(formData.filter);
        setTriggerPeriod(filterData.triggerPeriod || 'all_day');
        setTriggerIntervalHours(filterData.triggerIntervalHours || 0);
        setTriggerIntervalMinutes(filterData.triggerIntervalMinutes || 0);
        setTriggerIntervalSeconds(filterData.triggerIntervalSeconds || 2);
        // setAdvancedOptionsVisible(true);
      }
    }
  }, [mode, formData, form, initialSelectedKeys, selectedActionObjects]);



  const isReadonly = mode === 'readonly';

  const getBtnText = () => {
    if (mode === 'create' || mode === 'queryEdit') {
      return t('smartCenter.saveRule');
    }
    if (mode === 'edit') {
      return t('smartCenter.updateRule');
    }
    return '';
  };

  const refreshMiotInfo = async (refreshFun) => {
    if (!refreshFun) { return; }

    try {
      const res = await refreshFun();
      const { code: refreshCode, message: refreshMessage } = res || {};
      if (refreshCode !== 0) {
        message.error(refreshMessage);
      }
    } catch (error) {
      console.error('Refresh error:', error);
      message.error(t('smartCenter.refreshFailed'));
    }
  };

  const renderDropdownWithRefresh = (loading, text, refreshFun) => {
    if (!refreshFun) {
      return undefined;
    }

    return (menu) => (
      <>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            padding: '8px 12px',
            cursor: loading ? 'not-allowed' : 'pointer',
            color: loading ? '#999' : '#1677ff',
            fontWeight: 500,
            borderBottom: '1px solid #f0f0f0',
            userSelect: 'none',
            transition: 'background 0.2s',
          }}
          onClick={loading ? undefined : () => refreshMiotInfo(refreshFun)}
          onMouseDown={e => e.preventDefault()}
        >
          {loading ? <Spin size="small" style={{ marginRight: 8 }} /> : <ReloadOutlined style={{ marginRight: 8 }} />}
          {text}
        </div>
        {menu}
      </>
    );
  };


  const getBackendData = async (type = 'submit') => {
    const values = await form.validateFields();

    const hasActions = selectedActions.length > 0;
    const hasNotification = sendNotification && notificationText.trim();

    if (type === 'submit') {
      const hasAiRecommendActions = aiRecommendExecuteType === 'dynamic'
        ? aiRecommendActionDescriptions.length > 0
        : aiRecommendActions.length > 0;

      if (!hasAiRecommendActions && !hasActions && !hasNotification) {
        message.error(t('common.pleaseSelectAction'));
        return false;
      }
    }

    const allSelectedActionKeys = selectedActions;
    const automation_actions = allSelectedActionKeys
      .map(key => {
        return actionOptions.find(action => {
          const serverName = action.mcp_server_name || 'unknown';
          return `${serverName}#${action.introduction}` === key;
        });
      })
      .filter(Boolean)
      .map(action => ({
        mcp_client_id: action.mcp_client_id,
        mcp_tool_name: action.mcp_tool_name,
        mcp_tool_input: action.mcp_tool_input,
        mcp_server_name: action.mcp_server_name,
        mcp_tool_description: action.mcp_tool_description,
        introduction: action.introduction || '',
      }));

    const cameras = values.cameras.map(did => {
      const camera = cameraOptions.find(c => c.did === did);
      return camera || did;
    });
    const formData = {
      name: values.name,
      cameras,
      condition: values.condition,
      automation_actions,
      ai_recommend_execute_type: aiRecommendExecuteType,
      ai_recommend_action_descriptions: aiRecommendActionDescriptions,
      ai_recommend_actions: aiRecommendActions || [],
      notify: hasNotification ? {
        id: initialRule?.execute_info?.notify?.id || null,
        content: notificationText.trim(),
        use_template: useTemplateNotification,
      } : null,
      filter: {
        triggerPeriod,
        triggerIntervalHours,
        triggerIntervalMinutes,
        triggerIntervalSeconds,
      },
      mcp_list: checkedMcpServices.map(service => availableMcpServices.find(mcp => `${mcp?.server_name}#${mcp?.client_id}` === service)).filter(Boolean),
      enabled: initialRule?.enabled !== undefined ? initialRule.enabled : true,
    };

    const backendData = convertFormDataToBackend(formData);
    if (mode === 'edit' || mode === 'queryEdit') {
      backendData.id = initialRule?.id;
    }

    return backendData;
  };

  const handleSubmit = async (type = 'submit') => {
    const backendData = await getBackendData(type);
    if (!backendData) {
      return;
    }
    if (type === 'submit') {
      if (aiRecommendActionDescriptions.length !== 0 && aiRecommendActions.length === 0) {
        setActionDescriptionError(true);
        message.error(t('smartCenter.pleaseEnterActionDescription'));
        return;
      }
      setActionDescriptionError(false);
      await onSubmit(backendData);
    }
    if (type === 'cancel') {
      await onCancel(backendData);
    }
  };

  const handleFormValuesChange = (changedValues, allValues) => {
    if ('cameras' in changedValues) {
      setAiRecommendActions([]);
      setAiRecommendExecuteType('dynamic');
    }
  };

  const isSubmitDisabled = isReadonly || loading;
  return (
    <Form form={form} layout="vertical" onValuesChange={handleFormValuesChange}>
      <Form.Item
        className={styles.customFormLabel}
        label={t('smartCenter.serviceName')}
        name="name"
        rules={[{ required: true, message: t('smartCenter.pleaseEnterRuleName') }]}
      >
        <Input
          placeholder={t('smartCenter.exampleService')}
          disabled={isSubmitDisabled}
        />
      </Form.Item>

      <Form.Item
        className={styles.customFormLabel}
        label={t('smartCenter.selectCameras')}
        name="cameras"
        rules={[{ required: true, message: t('smartCenter.pleaseSelectCameras') }]}
      >
        <Select
          mode="multiple"
          allowClear
          placeholder={t('smartCenter.pleaseSelectCameras')}
          disabled={isSubmitDisabled}
          options={cameraOptions?.map?.(item => ({
            label: `${item.name}(${item.room_name || ''})`,
            value: item.did
          }))}
          className={styles.select}
          dropdownRender={enableCameraRefresh && onRefreshCameras
            ? renderDropdownWithRefresh(cameraLoading, t('smartCenter.refreshCameras'), onRefreshCameras)
            : undefined
          }
        />
      </Form.Item>

      <Form.Item
        className={styles.customFormLabel}
        label={
          <span>
            {t('smartCenter.triggerCondition')}
            <Tooltip
              placement="right"
              title={
                <div>
                  <div>{t('smartCenter.triggerConditionTip1')}</div>
                  <div style={{ marginTop: 8 }}>{t('smartCenter.triggerConditionTip2')}</div>
                  <div style={{ marginTop: 4 }}>• {t('smartCenter.triggerConditionExample1')}</div>
                  <div>• {t('smartCenter.triggerConditionExample2')}</div>
                  <div>• {t('smartCenter.triggerConditionExample3')}</div>
                  <div>• {t('smartCenter.triggerConditionExample4')}</div>
                  <div>• ...</div>
                  <div style={{ marginTop: 8 }}>{t('smartCenter.triggerConditionTip3')}</div>
                </div>
              }>
              <QuestionCircleOutlined style={{ marginLeft: 4, color: '#999' }} />
            </Tooltip>
          </span>}
        name="condition"
        rules={[{ required: true, message: t('smartCenter.pleaseEnterTriggerCondition') }]}
      >
        <Input
          placeholder={t('smartCenter.exampleMove')}
          disabled={isReadonly || loading}
        />
      </Form.Item>

      <Form.Item
        label={t('smartCenter.executionAction')}
        required
        className={styles.customFormLabel}
        validateStatus={(() => {
          const hasActions = selectedActions.length > 0;
          const hasNotification = sendNotification && notificationText.trim();

          if (!hasActions && !hasNotification &&
            (selectedActions.length > 0 || (sendNotification && notificationText.trim()) ||
              form.getFieldError('name')?.length > 0 || form.getFieldError('condition')?.length > 0)) {
            return 'error';
          }
          return '';
        })()}
      >
        <div className={styles.actionGroup}>
          {mode !== 'readonly' && (
            <div className={styles.actionItem}>
              <div className={styles.actionLabel}>MCP</div>
              <Select
                disabled={isSubmitDisabled}
                mode="multiple"
                placeholder={t('smartCenter.pleaseSelectMcp')}
                value={checkedMcpServices}
                options={availableMcpServices.map(service => ({
                  label: `${service?.server_name}#${service?.client_id}`,
                  value: `${service?.server_name}#${service?.client_id}`,
                }))}
                onChange={(values) => {
                  setCheckedMcpServices(values)
                  setAiRecommendActions([]);
                  setAiRecommendExecuteType('dynamic');
                }}
              />
            </div>
          )}
          <div className={styles.actionItem}>
            <div className={styles.actionLabel}>
              <span>
                {t('smartCenter.deviceControl')}
              </span>
            </div>
            <div className={classNames(styles.actionControl)}>
              <Select
                mode="tags"
                placeholder={t('smartCenter.pleaseSelectDevice')}
                disabled={isSubmitDisabled}
                value={aiRecommendActionDescriptions}
                status={actionDescriptionError ? 'error' : ''}
                onChange={(values) => {
                  setAiRecommendActionDescriptions(values);
                  setAiRecommendActions([]);
                  setAiRecommendExecuteType('dynamic');
                  setActionDescriptionError(false);
                }}
              />
              <Button
                type='primary'
                danger
                disabled={isSubmitDisabled}
                className={styles.actionControlButton}
                onClick={() => {
                  setActionDescriptionError(false);
                  const cameras = form.getFieldValue('cameras')
                  if (checkedMcpServices.length === 0) {
                    message.error(t('smartCenter.pleaseSelectMcp'));
                    return;
                  }
                  const mcp_list = availableMcpServices.filter(mcp => checkedMcpServices.includes(`${mcp.server_name}#${mcp.client_id}`));
                  const mcp_list_ids = mcp_list.map(mcp => mcp.client_id);
                  if (aiRecommendActionDescriptions.length > 0) {
                    openModal(aiRecommendActionDescriptions, cameras, mcp_list_ids);
                  } else {
                    message.error(t('smartCenter.pleaseEnterActionDescription'));
                  }
                }}
              >
                {t('smartCenter.generateStaticActionList')}
              </Button>
            </div>
            <div className={classNames(styles.actionControl, styles.actionControl2)}>
              <Select
                mode="tags"
                tagRender={(props) => <SelectTagRender aiRecommendActions={aiRecommendActions} {...props} />}
                placeholder={t('smartCenter.pleaseSelectAiRecommendedAction')}
                disabled={true}
                value={aiRecommendActions.map(action => action.introduction)}
                status={actionDescriptionError ? 'error' : ''}
                suffixIcon={null}
              />
            </div>
            <div className={classNames(styles.actionControl, styles.actionControl2)}>
              <span>
                {t('smartCenter.whetherToCache')}
                <Tooltip
                  placement="right"
                  title={
                    <div>
                      <div>{t('smartCenter.deviceControlTip1')}</div>
                      <div style={{ marginTop: 8 }}>{t('smartCenter.deviceControlTip2')}</div>
                    </div>
                  }>
                  <InfoCircleOutlined style={{ marginLeft: 4, color: '#999' }} />
                </Tooltip>
              </span>
              <Switch
                checked={aiRecommendExecuteType === 'static'}
                onChange={(checked) => {
                  setAiRecommendExecuteType(checked ? 'static' : 'dynamic');
                }}
                disabled={isSubmitDisabled || aiRecommendActions.length === 0}
              />
            </div>
          </div>
          <div className={styles.actionItem}>
            <div className={styles.actionLabel}>{t('smartCenter.automationScene')}</div>
            <Select
              mode="multiple"
              allowClear
              placeholder={t('smartCenter.automationSceneDescription')}
              disabled={isSubmitDisabled}
              options={groupedOptions}
              value={selectedActions}
              onChange={(values) => {
                setSelectedActions(values);
              }}
              className={styles.select}
              dropdownRender={enableActionRefresh && onRefreshActions
                ? renderDropdownWithRefresh(actionLoading, t('smartCenter.refreshActions'), onRefreshActions)
                : undefined
              }
            />
          </div>


          <div className={styles.actionItem}>
            <div className={styles.actionLabel}>
              <Checkbox
                checked={sendNotification}
                onChange={(e) => setSendNotification(e.target.checked)}
                disabled={isSubmitDisabled}
              >
                {t('smartCenter.sendMiHomeNotification')}
              </Checkbox>
            </div>
            {sendNotification && (
              <>
                <div className={styles.notificationOptions}>
                  <span className={styles.templateLabel}>
                    {t('smartCenter.useTemplateNotification')}
                    <Tooltip
                      placement="right"
                      title={t('smartCenter.useTemplateNotificationTip')}
                    >
                      <QuestionCircleOutlined style={{ marginLeft: 4, color: '#999' }} />
                    </Tooltip>
                  </span>
                  <Switch
                    checked={useTemplateNotification}
                    onChange={(checked) => setUseTemplateNotification(checked)}
                    disabled={isSubmitDisabled}
                    size="small"
                  />
                </div>
                {useTemplateNotification && (
                  <div className={styles.templateHint}>
                    <span className={styles.templateVariables}>{t('smartCenter.templateVariables')}: </span>
                    <code>{'{camera_name}'}</code>, <code>{'{camera_location}'}</code>, <code>{'{condition}'}</code>, <code>{'{trigger_time}'}</code>, <code>{'{rule_name}'}</code>
                  </div>
                )}
                <Input.TextArea
                  placeholder={useTemplateNotification 
                    ? t('smartCenter.templateExample')
                    : t('smartCenter.pleaseEnterNotification')}
                  value={notificationText}
                  onChange={(e) => setNotificationText(e.target.value)}
                  disabled={isSubmitDisabled}
                  rows={3}
                />
              </>
            )}
          </div>
        </div>
      </Form.Item>

      <div className={styles.advancedOptionsSection}>
        <div
          className={styles.advancedOptionsHeader}
          onClick={() => !isReadonly && setAdvancedOptionsVisible(!advancedOptionsVisible)}
        >
          <span className={styles.advancedOptionsTitle}>{t('smartCenter.moreAdvancedOptions')}</span>
          {advancedOptionsVisible ? <UpOutlined style={{ color: 'var(--text-color-5)' }} /> : <DownOutlined style={{ color: 'var(--text-color-5)' }} />}
        </div>

        {advancedOptionsVisible && (
          <div className={styles.advancedOptionsContent}>
            <div className={styles.advancedOptionItem}>
              <div className={styles.advancedOptionLabel}>{t('smartCenter.triggerPeriod')}:</div>
              <Select
                placeholder={t('smartCenter.nonRequired')}
                value={triggerPeriod}
                onChange={setTriggerPeriod}
                options={TRIGGER_PERIOD_OPTIONS}
                className={styles.advancedSelect}
                allowClear
                disabled={isSubmitDisabled}
              />
            </div>

            <div className={styles.advancedOptionItem}>
              <div className={styles.advancedOptionLabel}>{t('smartCenter.triggerInterval')}:</div>
              <div className={styles.triggerIntervalDescription}>
                {t('smartCenter.triggerIntervalDescription')}
              </div>
              <TimeSelector
                hours={triggerIntervalHours}
                minutes={triggerIntervalMinutes}
                seconds={triggerIntervalSeconds}
                onHoursChange={setTriggerIntervalHours}
                onMinutesChange={setTriggerIntervalMinutes}
                onSecondsChange={setTriggerIntervalSeconds}
                hoursOptions={TRIGGER_INTERVAL_OPTIONS.hours}
                minutesOptions={TRIGGER_INTERVAL_OPTIONS.minutes}
                secondsOptions={TRIGGER_INTERVAL_OPTIONS.seconds}
                className={styles.triggerIntervalSelector}
                disabled={isSubmitDisabled}
              />
            </div>
          </div>
        )}
      </div>

      {!isReadonly && (
        <div className={styles.saveBtnWrap}>
          {(mode === 'edit' || mode === 'queryEdit') && onCancel && (
            <Button onClick={() => handleSubmit('cancel')} disabled={isSubmitDisabled}>{t('common.cancel')}</Button>
          )}
          <Tooltip
            title={t('smartCenter.pleaseEnterActionDescription')}
            placement="top"
          >
            <Button
              type='primary'
              disabled={isSubmitDisabled}
              className={mode === 'create' ? styles.saveBtn : ''}
              block={mode === 'create'}
              onClick={() => handleSubmit('submit')}
              loading={loading}
            >
              {getBtnText()}
            </Button>

          </Tooltip>

        </div>
      )}
    </Form>
  );
};

export default RuleForm;
