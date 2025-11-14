/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import React, { useCallback, useState } from 'react';
import { Tag, Button, message } from 'antd';
import ReactMarkdown from 'react-markdown';
import { useTranslation } from 'react-i18next';
import { executeSceneActions } from '@/api';
import { formatDataToMarkdown } from '@/utils/util';

const AiGeneratedActionsMessage = ({ data }) => {
  const { t } = useTranslation();
  const [loadingStates, setLoadingStates] = useState(false);

  const { actions = []} = data
  const markdownContent = formatDataToMarkdown(data);


  const handleExecuteActions = useCallback(async (items) => {
    setLoadingStates(true)
    try {
      const res = await executeSceneActions(items);
      const data = res?.data || [];
      const resultTest = data.map((item, index) => {
        return !item ? `${items[index]?.introduction} ` : '';
      }).filter(item => item !== '');
      console.log('resultTest', resultTest);

      if (res?.code === 0 && resultTest.length === 0) {
        message.success(res?.message);
      } else {
        message.error(`${t('smartCenter.executionFailed')} ${resultTest.join(', ')}`);
      }
    } catch (error) {
      console.error('handleExecuteAction error', error);
      message.error(t('executionManage.executeActionFailed'));
    } finally {
      setLoadingStates(false);
    }
  }, []);


  return (
    <div>
      <Tag color="blue">{t('smartCenter.executionResult')}</Tag>
      <div
        style={{
          backgroundColor: 'var(--bg-color-card)',
          borderRadius: '6px',
          padding: '12px 16px',
          marginTop: '8px',
          marginBottom: '12px',
        }}
      >
        <ReactMarkdown
          components={{
            p: ({ children }) => <p style={{ margin: '0' }}>{children}</p>,
            code: ({ children, className }) => {
              const isInline = !className;
              return (
                <code
                  style={{
                    backgroundColor: isInline ? 'rgba(175, 184, 193, 0.2)' : 'transparent',
                    padding: isInline ? '2px 6px' : '0',
                    borderRadius: '3px',
                    fontSize: '13px',
                    fontFamily: 'Monaco, Menlo, "Ubuntu Mono", Consolas, monospace',
                  }}
                  className={className}
                >
                  {children}
                </code>
              );
            },
            pre: ({ children }) => (
              <pre
                style={{
                  margin: '0',
                  padding: '0',
                  backgroundColor: 'transparent',
                  overflow: 'auto',
                }}
              >
                {children}
              </pre>
            ),
          }}
        >
          {markdownContent}
        </ReactMarkdown>
      </div>
    </div>
  );
};

export default AiGeneratedActionsMessage;
