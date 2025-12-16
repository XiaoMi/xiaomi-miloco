/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import React from 'react';
import { Table, Empty, Button } from 'antd';
import { useTranslation } from 'react-i18next';
import { Card, Icon, LogViewerModal } from '@/components';
import { useLogViewerStore } from '@/stores/logViewerStore';
import EmptyHistory from '@/assets/images/empty-history.png';
import styles from '../index.module.less';
import ImageRecordModal from './ImageRecordModal';
import useLog from '../hooks/useLog';

/**
 * RuleRecord Component - Rule record component
 * 规则记录组件
 *
 * @returns {JSX.Element} RuleRecord component
 */
const RuleRecord = () => {
  const { t } = useTranslation();

  const {
    recordData,
    totalItems,
    rulesLength,
    loading,
    imageModalVisible,
    currentImageData,
    initData,
    handleImageClick,
    handleCloseImageModal
  } = useLog();

  const handleViewDynamicLog = (item) => {
    const { rawData } = item;
    const logId = rawData?.id;

    if (!logId) {
      console.error('handleViewDynamicLog: cannot get log_id');
      return;
    }

    useLogViewerStore.getState().openModalWithLogId(logId);
  };

  const renderExecutionResult = (item) => {
    const {
      executionResults,
      notifyResult,
      triggerCondition,
      isDynamic
    } = item;

    const allResults = [...executionResults];
    if (notifyResult) {
      allResults.push(notifyResult);
    }

    let executionContent;

    if (isDynamic) {
      const actionElements = allResults.length > 0 ? (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          {allResults.map((result, index) => {
            const statusIcon = result.success ? (
              <Icon name="toolCallSuccess" size={14} style={{ marginRight: 4 }} />
            ) : (
              <Icon name="toolCallFail" size={14} style={{ marginRight: 4 }} />
            );

            const displayText = result.isNotification
              ? `${t('logManage.sendNotification')}: ${result.text}`
              : result.text;

            return (
              <span key={index} style={{ display: 'flex', alignItems: 'center' }}>
                {statusIcon}
                {displayText}
                <span>; </span>
              </span>
            );
          })}
        </div>
      ) : null;

      const { aiRecommendActionDescriptions = [] } = item;

      const buttonText = aiRecommendActionDescriptions.length > 0
        ? `${t('logManage.viewDynamicExecutionLogButtonText')}`
        : '';

      executionContent = (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          {actionElements}
          {buttonText && (
            <Button
              type="link"
              size="small"
              onClick={() => handleViewDynamicLog(item)}
              style={{ padding: 0, height: 'auto', color: 'blue' }}
            >
              {buttonText}
            </Button>
          )}
        </div>
      );
    } else {
      if (allResults.length > 0) {
        executionContent = (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            {allResults.map((result, index) => {
              const statusIcon = result.success ? (
                <Icon name="toolCallSuccess" size={14} style={{ marginRight: 4 }} />
              ) : (
                <Icon name="toolCallFail" size={14} style={{ marginRight: 4 }} />
              );

              const displayText = result.isNotification
                ? `${t('logManage.sendNotification')}: ${result.text}`
                : result.text;

              return (
                <span key={index} style={{ display: 'flex', alignItems: 'center' }}>
                  {statusIcon}
                  {displayText}
                  <span>; </span>
                </span>
              );
            })}
          </div>
        );
      } else {
        executionContent = t('logManage.noExecutionAction');
      }
    }

    return (
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ fontWeight: 500 }}>{triggerCondition}</span>
        <span>→</span>
        {executionContent}
      </div>
    );
  };

  const recordColumns = [
    {
      title: t('logManage.triggerTime'),
      dataIndex: 'time',
      key: 'time',
      width: 100,
      render: (text) => (
        <div style={{ whiteSpace: 'pre-line' }}>{text}</div>
      )
    },
    { title: t('logManage.ruleName'), dataIndex: 'rule', key: 'rule', width: 120 },
    {
      title: t('logManage.executionEffect'),
      dataIndex: 'result',
      key: 'result',
      render: (_, record) => renderExecutionResult(record)
    },
    {
      title: t('common.images'),
      dataIndex: 'images',
      key: 'images',
      width: 70,
      render: (_, record) => (
        <Icon
          onClick={() => handleImageClick(record.cameraResults)}
          style={{ cursor: 'pointer' }}
          name="ruleRecordImages"
          size={18}
        />
      )
    },
  ];


  return (
    <div className={styles.record}>
      <div className={styles.statsRow}>
        <Card className={styles.statCard} contentClassName={styles.statCardContent} >
          <div className={styles.statCardTitle}>
            <div className={styles.title}>{t('logManage.triggerRules')}</div>
            <div className={styles.subtitle}>{t('logManage.totalTriggeredRules')}</div>
          </div>
          <div className={styles.statCardValue}>{totalItems || 0}</div>
        </Card>
        <Card className={styles.statCard} contentClassName={styles.statCardContent}>
          <div className={styles.statCardTitle}>
            <div className={styles.title}>{t('logManage.activeRules')}</div>
            <div className={styles.subtitle}>{t('logManage.activeRulesCount')}:</div>
          </div>

          <div className={styles.statCardValue}>{rulesLength}</div>
        </Card>
      </div>
      <Card className={styles.recordCard}>
        <div className={styles.statList}>
          <span className={styles.statListTitle}>{t('logManage.recentTriggeredRules')}:</span>
          <div className={styles.update}>
            <Icon name="refresh" size={16} onClick={initData} />
          </div>
        </div>

        <Table
          columns={recordColumns}
          dataSource={recordData}
          pagination={false}
          loading={loading}
          bordered={false}
          scroll={{ y: 600 }}
          size="small"
          className={styles.tableFont}
          locale={{
            emptyText: (
              <div
                style={{
                  height: '600px',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center'
                }}
              >
                <Empty
                  description={t('logManage.noRuleRecord')}
                  imageStyle={{ width: 75, height: 75 }}
                  image={EmptyHistory}
                />
              </div>
            ),
          }}
        />
      </Card>
      <ImageRecordModal
        visible={imageModalVisible}
        onCancel={handleCloseImageModal}
        imageData={currentImageData}
      />
      <LogViewerModal />
    </div>
  );
};

export default RuleRecord;
