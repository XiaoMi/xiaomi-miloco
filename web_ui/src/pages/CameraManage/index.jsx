/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import React from 'react';
import { useTranslation } from 'react-i18next';
import { Header, PageContent } from '@/components';
import EmptyRule from '@/assets/images/empty-rule.png';
import { CameraList, CameraForm } from './components';
import { useCameras, useCameraForm } from './hooks';
import styles from './index.module.less';

/**
 * CameraManage Page - RTSP摄像头管理页面
 * 用于管理手动添加的RTSP摄像头
 *
 * @returns {JSX.Element} 摄像头管理页面组件
 */
const CameraManage = () => {
  const { t } = useTranslation();

  const {
    cameras,
    loading,
    handleSwitch,
    handleDelete,
    createCamera,
    updateCamera,
    refreshStatus,
  } = useCameras();

  const {
    modalOpen,
    editId,
    form,
    submitLoading,
    openAddForm,
    openEditForm,
    closeForm,
    setLoading,
  } = useCameraForm();

  // 处理表单提交
  const handleFormSubmit = async () => {
    try {
      setLoading(true);
      const values = await form.validateFields();

      if (editId) {
        const result = await updateCamera(editId, values);
        if (result.success) {
          closeForm();
        }
      } else {
        const result = await createCamera(values);
        if (result.success) {
          closeForm();
        }
      }
    } catch (error) {
      console.error('保存摄像头失败:', error);
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      <PageContent
        Header={(
          <Header
            title={t('cameraManage.title')}
            buttonText={t('cameraManage.addCamera')}
            buttonHandleCallback={openAddForm}
          />
        )}
        loading={loading}
        showEmptyContent={!loading && cameras.length === 0}
        emptyContentProps={{
          description: t('cameraManage.noCameras'),
          imageStyle: { width: 72, height: 72 },
          image: EmptyRule,
        }}
      >
        <CameraList
          cameras={cameras}
          onSwitch={handleSwitch}
          onEdit={openEditForm}
          onDelete={handleDelete}
          onRefreshStatus={refreshStatus}
        />
      </PageContent>

      <CameraForm
        modalOpen={modalOpen}
        editId={editId}
        form={form}
        submitLoading={submitLoading}
        onCancel={closeForm}
        onOk={handleFormSubmit}
      />
    </>
  );
};

export default CameraManage;
