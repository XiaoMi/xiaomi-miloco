# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
RTSP摄像头数据访问对象
处理RTSP摄像头的CRUD操作
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from miloco_server.utils.database import get_db_connector

logger = logging.getLogger(__name__)


class RTSPCameraDAO:
    """RTSP摄像头数据访问对象"""

    def __init__(self):
        self.db_connector = get_db_connector()
        logger.info("RTSPCameraDAO 初始化完成")

    def create(self, camera_data: Dict[str, Any]) -> Optional[str]:
        """
        创建RTSP摄像头记录

        Args:
            camera_data: 摄像头数据字典，包含name, location, rtsp_url_main, rtsp_url_sub等字段

        Returns:
            Optional[str]: 成功返回摄像头ID，失败返回None
        """
        try:
            camera_id = camera_data.get("id") or str(uuid.uuid4())
            current_time = datetime.now().isoformat()
            
            sql = """
                INSERT INTO rtsp_camera (
                    id, name, location, rtsp_url_main, rtsp_url_sub, 
                    enabled, online_main, online_sub, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            params = (
                camera_id,
                camera_data.get("name", ""),
                camera_data.get("location", ""),
                camera_data.get("rtsp_url_main", ""),
                camera_data.get("rtsp_url_sub", ""),
                camera_data.get("enabled", True),
                camera_data.get("online_main", False),
                camera_data.get("online_sub", False),
                current_time,
                current_time
            )
            
            affected_rows = self.db_connector.execute_update(sql, params)
            if affected_rows > 0:
                logger.info("RTSP摄像头创建成功: id=%s, name=%s", camera_id, camera_data.get("name"))
                return camera_id
            else:
                logger.warning("RTSP摄像头创建失败: name=%s", camera_data.get("name"))
                return None
                
        except Exception as e:
            logger.error("创建RTSP摄像头时出错: %s", e)
            return None

    def get_by_id(self, camera_id: str) -> Optional[Dict[str, Any]]:
        """
        根据ID获取摄像头信息

        Args:
            camera_id: 摄像头ID

        Returns:
            Optional[Dict[str, Any]]: 摄像头信息字典，不存在返回None
        """
        try:
            sql = "SELECT * FROM rtsp_camera WHERE id = ?"
            params = (camera_id,)
            results = self.db_connector.execute_query(sql, params)
            
            if results:
                camera = dict(results[0])
                # 转换布尔值
                camera["enabled"] = bool(camera.get("enabled", False))
                camera["online_main"] = bool(camera.get("online_main", False))
                camera["online_sub"] = bool(camera.get("online_sub", False))
                return camera
            return None
            
        except Exception as e:
            logger.error("获取RTSP摄像头时出错: id=%s, error=%s", camera_id, e)
            return None

    def get_all(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        """
        获取所有RTSP摄像头

        Args:
            enabled_only: 是否只返回启用的摄像头

        Returns:
            List[Dict[str, Any]]: 摄像头列表
        """
        try:
            if enabled_only:
                sql = "SELECT * FROM rtsp_camera WHERE enabled = 1 ORDER BY created_at DESC"
            else:
                sql = "SELECT * FROM rtsp_camera ORDER BY created_at DESC"
            
            results = self.db_connector.execute_query(sql)
            cameras = []
            
            for row in results:
                camera = dict(row)
                # 转换布尔值
                camera["enabled"] = bool(camera.get("enabled", False))
                camera["online_main"] = bool(camera.get("online_main", False))
                camera["online_sub"] = bool(camera.get("online_sub", False))
                cameras.append(camera)
            
            logger.debug("获取到 %d 个RTSP摄像头", len(cameras))
            return cameras
            
        except Exception as e:
            logger.error("获取所有RTSP摄像头时出错: %s", e)
            return []

    def update(self, camera_id: str, camera_data: Dict[str, Any]) -> bool:
        """
        更新RTSP摄像头信息

        Args:
            camera_id: 摄像头ID
            camera_data: 要更新的摄像头数据

        Returns:
            bool: 更新是否成功
        """
        try:
            current_time = datetime.now().isoformat()
            
            # 构建动态更新语句
            update_fields = []
            params = []
            
            field_mapping = {
                "name": "name",
                "location": "location",
                "rtsp_url_main": "rtsp_url_main",
                "rtsp_url_sub": "rtsp_url_sub",
                "enabled": "enabled",
                "online_main": "online_main",
                "online_sub": "online_sub"
            }
            
            for key, db_field in field_mapping.items():
                if key in camera_data:
                    update_fields.append(f"{db_field} = ?")
                    params.append(camera_data[key])
            
            if not update_fields:
                logger.warning("没有要更新的字段")
                return False
            
            update_fields.append("updated_at = ?")
            params.append(current_time)
            params.append(camera_id)
            
            sql = f"UPDATE rtsp_camera SET {', '.join(update_fields)} WHERE id = ?"
            affected_rows = self.db_connector.execute_update(sql, tuple(params))
            
            if affected_rows > 0:
                logger.info("RTSP摄像头更新成功: id=%s", camera_id)
                return True
            else:
                logger.warning("RTSP摄像头更新失败: id=%s", camera_id)
                return False
                
        except Exception as e:
            logger.error("更新RTSP摄像头时出错: id=%s, error=%s", camera_id, e)
            return False

    def update_online_status(self, camera_id: str, channel: int, online: bool) -> bool:
        """
        更新摄像头在线状态

        Args:
            camera_id: 摄像头ID
            channel: 通道号 (0=主码流, 1=子码流)
            online: 是否在线

        Returns:
            bool: 更新是否成功
        """
        try:
            field = "online_main" if channel == 0 else "online_sub"
            current_time = datetime.now().isoformat()
            
            sql = f"UPDATE rtsp_camera SET {field} = ?, updated_at = ? WHERE id = ?"
            params = (online, current_time, camera_id)
            
            affected_rows = self.db_connector.execute_update(sql, params)
            return affected_rows > 0
            
        except Exception as e:
            logger.error("更新RTSP摄像头在线状态时出错: id=%s, channel=%d, error=%s", camera_id, channel, e)
            return False

    def delete(self, camera_id: str) -> bool:
        """
        删除RTSP摄像头

        Args:
            camera_id: 摄像头ID

        Returns:
            bool: 删除是否成功
        """
        try:
            sql = "DELETE FROM rtsp_camera WHERE id = ?"
            params = (camera_id,)
            
            affected_rows = self.db_connector.execute_update(sql, params)
            
            if affected_rows > 0:
                logger.info("RTSP摄像头删除成功: id=%s", camera_id)
                return True
            else:
                logger.warning("RTSP摄像头删除失败，可能不存在: id=%s", camera_id)
                return False
                
        except Exception as e:
            logger.error("删除RTSP摄像头时出错: id=%s, error=%s", camera_id, e)
            return False

    def exists(self, camera_id: str) -> bool:
        """
        检查摄像头是否存在

        Args:
            camera_id: 摄像头ID

        Returns:
            bool: 是否存在
        """
        try:
            sql = "SELECT COUNT(*) as count FROM rtsp_camera WHERE id = ?"
            params = (camera_id,)
            results = self.db_connector.execute_query(sql, params)
            
            if results and results[0]["count"] > 0:
                return True
            return False
            
        except Exception as e:
            logger.error("检查RTSP摄像头是否存在时出错: id=%s, error=%s", camera_id, e)
            return False
