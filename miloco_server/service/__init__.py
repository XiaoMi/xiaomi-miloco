# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""
service package
"""
from cachetools import TTLCache
from miloco_server.service.trigger_rule_dynamic_executor import TriggerRuleDynamicExecutor


trigger_rule_dynamic_executor_cache: TTLCache[str, TriggerRuleDynamicExecutor] = TTLCache(maxsize=100, ttl=600)
