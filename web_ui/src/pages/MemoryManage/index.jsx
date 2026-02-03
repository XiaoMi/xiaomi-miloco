/**
 * Copyright (C) 2025 Xiaomi Corporation
 * This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
 */

import React, { useState, useEffect, useCallback } from 'react';
import { 
  Card, 
  List, 
  Button, 
  Input, 
  Modal, 
  Form, 
  Select, 
  Tag, 
  message, 
  Popconfirm, 
  Empty,
  Spin,
  Space,
  Tooltip,
  Statistic,
  Row,
  Col
} from 'antd';
import { 
  PlusOutlined, 
  DeleteOutlined, 
  EditOutlined, 
  SearchOutlined,
  ReloadOutlined,
  BulbOutlined,
  HeartOutlined,
  ClockCircleOutlined,
  HomeOutlined,
  TeamOutlined,
  SettingOutlined,
  TagOutlined
} from '@ant-design/icons';
import { useTranslation } from 'react-i18next';
import { 
  getMemoryList, 
  addMemory, 
  updateMemory, 
  deleteMemory, 
  searchMemory,
  handleMemoryCommand,
  getMemoryStats,
  getMemoryTypes 
} from '@/api';
import styles from './index.module.less';

const { TextArea } = Input;

// 记忆类型图标映射
const typeIconMap = {
  preference: <HeartOutlined />,
  fact: <BulbOutlined />,
  habit: <ClockCircleOutlined />,
  device_setting: <SettingOutlined />,
  schedule: <ClockCircleOutlined />,
  relationship: <TeamOutlined />,
  custom: <TagOutlined />,
};

// 记忆类型颜色映射
const typeColorMap = {
  preference: 'magenta',
  fact: 'blue',
  habit: 'green',
  device_setting: 'orange',
  schedule: 'purple',
  relationship: 'cyan',
  custom: 'default',
};

const MemoryManage = () => {
  const { t } = useTranslation();
  const [memories, setMemories] = useState([]);
  const [memoryTypes, setMemoryTypes] = useState([]);
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(false);
  const [searchLoading, setSearchLoading] = useState(false);
  
  const [isModalVisible, setIsModalVisible] = useState(false);
  const [editingMemory, setEditingMemory] = useState(null);
  const [form] = Form.useForm();
  
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState(null);
  
  const [naturalCommand, setNaturalCommand] = useState('');
  const [commandLoading, setCommandLoading] = useState(false);

  // 加载记忆列表
  const loadMemories = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getMemoryList({ pageSize: 100 });
      if (res?.code === 0) {
        setMemories(res.data?.memories || []);
      } else {
        message.error(res?.message || t('memory.loadFailed'));
      }
    } catch (error) {
      console.error('Load memories error:', error);
      message.error(t('memory.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  // 加载统计信息
  const loadStats = useCallback(async () => {
    try {
      const res = await getMemoryStats();
      if (res?.code === 0) {
        setStats(res.data);
      }
    } catch (error) {
      console.error('Load stats error:', error);
    }
  }, []);

  // 加载记忆类型
  const loadMemoryTypes = useCallback(async () => {
    try {
      const res = await getMemoryTypes();
      if (res?.code === 0) {
        setMemoryTypes(res.data || []);
      }
    } catch (error) {
      console.error('Load memory types error:', error);
    }
  }, []);

  useEffect(() => {
    loadMemories();
    loadStats();
    loadMemoryTypes();
  }, [loadMemories, loadStats, loadMemoryTypes]);

  // 打开添加/编辑模态框
  const openModal = (memory = null) => {
    setEditingMemory(memory);
    if (memory) {
      form.setFieldsValue({
        content: memory.content,
        memory_type: memory.memory_type,
      });
    } else {
      form.resetFields();
    }
    setIsModalVisible(true);
  };

  // 保存记忆
  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      
      if (editingMemory) {
        // 更新
        const res = await updateMemory(editingMemory.id, values);
        if (res?.code === 0) {
          message.success(t('memory.updateSuccess'));
          setIsModalVisible(false);
          loadMemories();
          loadStats();
        } else {
          message.error(res?.message || t('memory.updateFailed'));
        }
      } else {
        // 添加
        const res = await addMemory(values);
        if (res?.code === 0) {
          message.success(t('memory.addSuccess'));
          setIsModalVisible(false);
          loadMemories();
          loadStats();
        } else {
          message.error(res?.message || t('memory.addFailed'));
        }
      }
    } catch (error) {
      console.error('Save memory error:', error);
    }
  };

  // 删除记忆
  const handleDelete = async (memoryId) => {
    try {
      const res = await deleteMemory(memoryId);
      if (res?.code === 0) {
        message.success(t('memory.deleteSuccess'));
        loadMemories();
        loadStats();
      } else {
        message.error(res?.message || t('memory.deleteFailed'));
      }
    } catch (error) {
      console.error('Delete memory error:', error);
      message.error(t('memory.deleteFailed'));
    }
  };

  // 搜索记忆
  const handleSearch = async () => {
    if (!searchQuery.trim()) {
      setSearchResults(null);
      return;
    }
    
    setSearchLoading(true);
    try {
      const res = await searchMemory({ query: searchQuery, limit: 10 });
      if (res?.code === 0) {
        setSearchResults(res.data || []);
      } else {
        message.error(res?.message || t('memory.searchFailed'));
      }
    } catch (error) {
      console.error('Search memory error:', error);
      message.error(t('memory.searchFailed'));
    } finally {
      setSearchLoading(false);
    }
  };

  // 处理自然语言指令
  const handleNaturalCommand = async () => {
    if (!naturalCommand.trim()) {
      return;
    }
    
    setCommandLoading(true);
    try {
      const res = await handleMemoryCommand({ command: naturalCommand });
      if (res?.code === 0) {
        message.success(res.data?.message || t('memory.commandSuccess'));
        setNaturalCommand('');
        loadMemories();
        loadStats();
      } else {
        message.warning(res?.message || t('memory.commandFailed'));
      }
    } catch (error) {
      console.error('Handle command error:', error);
      message.error(t('memory.commandFailed'));
    } finally {
      setCommandLoading(false);
    }
  };

  // 渲染记忆卡片
  const renderMemoryItem = (memory) => {
    const typeLabel = memoryTypes.find(t => t.value === memory.memory_type)?.label || memory.memory_type;
    
    return (
      <List.Item
        key={memory.id}
        className={styles.memoryItem}
        actions={[
          <Tooltip title={t('common.edit')} key="edit">
            <Button 
              type="text" 
              icon={<EditOutlined />} 
              onClick={() => openModal(memory)}
            />
          </Tooltip>,
          <Popconfirm
            key="delete"
            title={t('memory.confirmDelete')}
            onConfirm={() => handleDelete(memory.id)}
            okText={t('common.confirm')}
            cancelText={t('common.cancel')}
          >
            <Tooltip title={t('common.delete')}>
              <Button type="text" danger icon={<DeleteOutlined />} />
            </Tooltip>
          </Popconfirm>
        ]}
      >
        <List.Item.Meta
          avatar={
            <span className={styles.typeIcon}>
              {typeIconMap[memory.memory_type] || <TagOutlined />}
            </span>
          }
          title={
            <div className={styles.memoryTitle}>
              <span className={styles.memoryContent}>{memory.content}</span>
              <Tag color={typeColorMap[memory.memory_type] || 'default'}>
                {typeLabel}
              </Tag>
            </div>
          }
          description={
            <div className={styles.memoryMeta}>
              <span>{t('memory.source')}: {memory.source === 'auto' ? t('memory.autoExtract') : t('memory.manualAdd')}</span>
              {memory.created_at && (
                <span className={styles.time}>
                  {new Date(memory.created_at).toLocaleString()}
                </span>
              )}
            </div>
          }
        />
      </List.Item>
    );
  };

  // 渲染搜索结果
  const renderSearchResult = (result) => {
    const memory = result.memory;
    const score = (result.score * 100).toFixed(1);
    
    return (
      <List.Item key={memory.id} className={styles.searchResultItem}>
        <List.Item.Meta
          title={
            <div className={styles.searchResultTitle}>
              <span>{memory.content}</span>
              <Tag color="blue">{t('memory.relevance')}: {score}%</Tag>
            </div>
          }
          description={
            <Tag color={typeColorMap[memory.memory_type] || 'default'}>
              {memoryTypes.find(t => t.value === memory.memory_type)?.label || memory.memory_type}
            </Tag>
          }
        />
      </List.Item>
    );
  };

  return (
    <div className={styles.memoryManage}>
      {/* 统计信息 */}
      {stats && (
        <Card className={styles.statsCard}>
          <Row gutter={16}>
            <Col span={6}>
              <Statistic 
                title={t('memory.totalMemories')} 
                value={stats.total_count} 
                prefix={<BulbOutlined />}
              />
            </Col>
            <Col span={6}>
              <Statistic 
                title={t('memory.activeMemories')} 
                value={stats.active_count}
                prefix={<HeartOutlined />}
              />
            </Col>
            <Col span={6}>
              <Statistic 
                title={t('memory.autoExtracted')} 
                value={stats.by_source?.auto || 0}
                prefix={<SettingOutlined />}
              />
            </Col>
            <Col span={6}>
              <Statistic 
                title={t('memory.manualAdded')} 
                value={stats.by_source?.manual || 0}
                prefix={<EditOutlined />}
              />
            </Col>
          </Row>
        </Card>
      )}

      {/* 自然语言指令 */}
      <Card 
        title={t('memory.naturalLanguageControl')} 
        className={styles.commandCard}
        extra={
          <Tooltip title={t('memory.commandTip')}>
            <BulbOutlined />
          </Tooltip>
        }
      >
        <div className={styles.commandInput}>
          <Input
            placeholder={t('memory.commandPlaceholder')}
            value={naturalCommand}
            onChange={(e) => setNaturalCommand(e.target.value)}
            onPressEnter={handleNaturalCommand}
            style={{ flex: 1 }}
          />
          <Button 
            type="primary" 
            onClick={handleNaturalCommand}
            loading={commandLoading}
          >
            {t('memory.execute')}
          </Button>
        </div>
        <div className={styles.commandExamples}>
          <span>{t('memory.examples')}: </span>
          <Tag onClick={() => setNaturalCommand('记住，我的猫叫咪咪')}>记住，我的猫叫咪咪</Tag>
          <Tag onClick={() => setNaturalCommand('我喜欢空调开26度')}>我喜欢空调开26度</Tag>
          <Tag onClick={() => setNaturalCommand('忘记我的温度偏好')}>忘记我的温度偏好</Tag>
        </div>
      </Card>

      {/* 搜索区域 */}
      <Card title={t('memory.search')} className={styles.searchCard}>
        <div className={styles.searchInput}>
          <Input.Search
            placeholder={t('memory.searchPlaceholder')}
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onSearch={handleSearch}
            loading={searchLoading}
            enterButton={<SearchOutlined />}
            style={{ flex: 1 }}
          />
          {searchResults && (
            <Button onClick={() => { setSearchResults(null); setSearchQuery(''); }}>
              {t('memory.clearSearch')}
            </Button>
          )}
        </div>
        
        {searchResults && (
          <List
            className={styles.searchResults}
            dataSource={searchResults}
            renderItem={renderSearchResult}
            locale={{ emptyText: t('memory.noSearchResults') }}
          />
        )}
      </Card>

      {/* 记忆列表 */}
      <Card 
        title={t('memory.memoryList')} 
        className={styles.listCard}
        extra={
          <Space>
            <Button 
              icon={<ReloadOutlined />} 
              onClick={() => { loadMemories(); loadStats(); }}
            >
              {t('common.refresh')}
            </Button>
            <Button 
              type="primary" 
              icon={<PlusOutlined />} 
              onClick={() => openModal()}
            >
              {t('memory.addMemory')}
            </Button>
          </Space>
        }
      >
        <Spin spinning={loading}>
          <List
            dataSource={memories}
            renderItem={renderMemoryItem}
            locale={{ emptyText: <Empty description={t('memory.noMemories')} /> }}
          />
        </Spin>
      </Card>

      {/* 添加/编辑模态框 */}
      <Modal
        title={editingMemory ? t('memory.editMemory') : t('memory.addMemory')}
        open={isModalVisible}
        onOk={handleSave}
        onCancel={() => setIsModalVisible(false)}
        okText={t('common.save')}
        cancelText={t('common.cancel')}
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="content"
            label={t('memory.content')}
            rules={[{ required: true, message: t('memory.contentRequired') }]}
          >
            <TextArea 
              rows={3} 
              placeholder={t('memory.contentPlaceholder')}
            />
          </Form.Item>
          <Form.Item
            name="memory_type"
            label={t('memory.type')}
            initialValue="custom"
          >
            <Select
              options={memoryTypes}
              placeholder={t('memory.selectType')}
            />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
};

export default MemoryManage;
