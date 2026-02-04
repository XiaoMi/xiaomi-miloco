/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import React, { useState, useEffect, useMemo } from 'react';
import { Button, message } from 'antd';
import { useTranslation } from 'react-i18next';
import RuleForm from '@/components/RuleForm';
import { useGlobalSocket } from '@/hooks/useGlobalSocket';
import { useChatStore } from '@/stores/chatStore';
import { MESSAGE_CONFIRMATION_NAME, MESSAGE_NAMESPACE, MESSAGE_TYPES } from '@/constants/messageTypes';
import { convertFormDataToBackend } from '@/hooks/useRuleFormData';

/**
 * RuleConfirmMessage Component - Rule confirm message component
 * 规则确认消息组件
 *
 * @param {Object} data - The data to display
 * @param {string} mode - The mode of the component
 * @returns {JSX.Element} Rule confirm message component
 */
const RuleConfirmMessage = React.memo(({ data, mode = 'queryEdit' }) => {
  const { t } = useTranslation();
  const socketActions = useGlobalSocket();
  const {
    sessionId,
    currentRequestId,
    availableMcpServices,
    fetchMcpServices,
  } = useChatStore();
  const [loading, setLoading] = useState(false);

  const {
    rule,
    confirmed,
    userSelections,
    camera_options = [],
    action_options = []
  } = data;

  // 加载 MCP 服务列表
  useEffect(() => {
    if (mode !== 'readonly' && (!availableMcpServices || availableMcpServices.length === 0)) {
      fetchMcpServices();
    }
  }, [mode, availableMcpServices, fetchMcpServices]);

  const actualMode = useMemo(() => {
    if (mode === 'readonly' || confirmed !== undefined) {
      return 'readonly';
    }
    return mode;
  }, [mode, confirmed]);


  // Send rule confirm message to backend
  const sendRuleConfirmMessage = async (confirmed, ruleData) => {
    if (!socketActions || !socketActions.sendMessageDirect) {
      console.error(t('instant.chat.socketUnavailable'));
      throw new Error(t('instant.chat.socketUnavailable'));
    }

    try {
      const confirmMessage = {
        header: {
          type: MESSAGE_TYPES.EVENT,
          namespace: MESSAGE_NAMESPACE.CONFIRMATION,
          name: MESSAGE_CONFIRMATION_NAME.SAVE_RULE_CONFIRM_RESULT,
          timestamp: Math.floor(Date.now() / 1000),
          request_id: currentRequestId,
          session_id: sessionId
        },
        payload: JSON.stringify({
          confirmed,
          rule: ruleData
        })
      };

      console.log('send rule confirm message to backend:', confirmMessage);
      const sentRequestId = socketActions.sendMessageDirect(confirmMessage);
      return sentRequestId;
    } catch (error) {
      console.error('send rule confirm message failed:', error);
      throw error;
    }
  };

  const handleConfirm = async (formData) => {
    try {
      setLoading(true);
      const backendData = formData;

      if (rule?.id) {
        backendData.id = rule.id;
      }
      await sendRuleConfirmMessage(true, backendData);
      message.success(t('instant.chat.ruleSaved'));

      // Save to message history
      if (socketActions && socketActions.saveMessageToHistory) {
        const confirmResultMessage = {
          header: {
            type: MESSAGE_TYPES.EVENT,
            namespace: MESSAGE_NAMESPACE.CONFIRMATION,
            name: MESSAGE_CONFIRMATION_NAME.SAVE_RULE_CONFIRM_RESULT,
            timestamp: Math.floor(Date.now() / 1000),
            request_id: currentRequestId,
            session_id: sessionId
          },
          payload: JSON.stringify({
            confirmed: true,
            rule: backendData,
            userSelections: formData
          })
        };

        socketActions.saveMessageToHistory(confirmResultMessage);
      }
    } catch (error) {
      console.error('confirm rule failed:', error);
      message.error(t('instant.chat.saveFail'));
    } finally {
      setLoading(false);
    }
  };

  const handleCancel = async (formData) => {
    try {
      setLoading(true);

      await sendRuleConfirmMessage(false, formData);

      if (socketActions && socketActions.saveMessageToHistory) {
        const cancelResultMessage = {
          header: {
            type: MESSAGE_TYPES.EVENT,
            namespace: MESSAGE_NAMESPACE.CONFIRMATION,
            name: MESSAGE_CONFIRMATION_NAME.SAVE_RULE_CONFIRM_RESULT,
            timestamp: Math.floor(Date.now() / 1000),
            request_id: currentRequestId,
            session_id: sessionId
          },
          payload: JSON.stringify({
            confirmed: false,
            rule: formData,
            userSelections: null
          })
        };

        socketActions.saveMessageToHistory(cancelResultMessage);
      }
    } catch (error) {
      console.error('cancel rule failed:', error);
      message.error(t('instant.chat.cancelFail'));
    } finally {
      setLoading(false);
    }
  };

  const getTitle = () => {
    if (actualMode === 'readonly' && confirmed) {
      return t('smartCenter.ruleOperated');
    }
    if (actualMode === 'readonly' && confirmed === false) {
      return t('smartCenter.ruleSaveCanceled');
    }
    return t('smartCenter.aiGeneratedRule');
  };

  return (
    <div style={{
      border: actualMode === 'readonly' && confirmed ? '1px solid #b7eb8f' :
        actualMode === 'readonly' && confirmed === false ? '1px solid #ffccc7' :
          '1px solid var(--border-color)',
      borderRadius: '6px',
      padding: '16px',
      backgroundColor: actualMode === 'readonly' && confirmed ? '#f6ffed' :
        actualMode === 'readonly' && confirmed === false ? '#fff2f0' :
          'var(--bg-color-card)',
      width: '520px',
      boxSizing: 'content-box',
    }}>
      <div style={{
        fontSize: '14px',
        fontWeight: 500,
        color: 'var(--text-color-85)',
        marginBottom: '16px',
        display: 'flex',
        alignItems: 'center',
        gap: '8px'
      }}>
        {getTitle()}
      </div>

      <div style={{
        border: '1px solid var(--border-color)',
        borderRadius: '6px',
        padding: '16px',
        backgroundColor: 'var(--bg-color)'
      }}>
        {rule && (
          <RuleForm
            mode={actualMode}
            initialRule={rule}
            onSubmit={handleConfirm}
            onCancel={actualMode !== 'readonly' ? handleCancel : undefined}
            loading={loading}
            cameraOptions={camera_options}
            actionOptions={action_options}
            enableCameraRefresh={false}
            enableActionRefresh={false}
          />
        )}
      </div>
    </div>
  );
}, (prevProps, nextProps) => {
  const prevRule = prevProps.data?.rule;
  const nextRule = nextProps.data?.rule;
  const isSameData = (
    prevProps.data?.confirmed === nextProps.data?.confirmed &&
    prevProps.mode === nextProps.mode &&
    JSON.stringify(prevRule) === JSON.stringify(nextRule) &&
    JSON.stringify(prevProps.data?.camera_options) === JSON.stringify(nextProps.data?.camera_options) &&
    JSON.stringify(prevProps.data?.action_options) === JSON.stringify(nextProps.data?.action_options)
  );

  return isSameData;
});

export default RuleConfirmMessage;
