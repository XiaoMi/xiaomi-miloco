# -*- coding: utf-8 -*-
# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.
"""
MIoT SPEC.
"""
import asyncio
from enum import Enum, auto
import json
from pathlib import Path
import platform
import time
from typing import Any, Dict, List, Optional
import logging
from pydantic import BaseModel, ConfigDict, Field

# pylint: disable=relative-beyond-top-level
from .const import SYSTEM_LANGUAGE_DEFAULT, SPEC_STD_LIB_EFFECTIVE_TIME
from .common import http_get_json_async, load_yaml_file
from .storage import MIoTStorage
from .error import MIoTSpecError

_LOGGER = logging.getLogger(__name__)


class MIoTSpecTypeLevel(int, Enum):
    """MIoT-Spec-V2 type level."""
    UNKNOWN = 0
    OPTIONAL = auto()
    REQUIRED = auto()
    CUSTOM = auto()


class MIoTSpecValueRange(BaseModel):
    """MIoT SPEC value range class."""
    min_: float = Field(alias="min", serialization_alias="min", description="Property value min")
    max_: float = Field(alias="max", serialization_alias="max", description="Property value max")
    step: float = Field(description="Property value step")

    model_config = ConfigDict(populate_by_name=True)

    def __str__(self) -> str:
        return f"[{self.min_}, {self.max_}, {self.step}]"


class MIoTSpecValueListItem(BaseModel):
    """MIoT SPEC value list item class."""
    # NOTICE: bool type without name
    name: str = Field(description="Property value name")
    # Value
    value: Any = Field(description="Property value")
    # Descriptions after multilingual conversion.
    description: str = Field(description="Property value description")

    model_config = ConfigDict(populate_by_name=True)

    def __str__(self) -> str:
        return f"{self.name}: {self.value} - {self.description}"


class MIoTSpecStdLib(BaseModel):
    """MIoT-Spec-V2 standard library."""
    devices: Dict[str, Dict[str, str]] = Field(description="Device list")
    services: Dict[str, Dict[str, str]] = Field(description="Service list")
    properties: Dict[str, Dict[str, str]] = Field(description="Property list")
    events: Dict[str, Dict[str, str]] = Field(description="Event list")
    actions: Dict[str, Dict[str, str]] = Field(description="Action list")
    values: Dict[str, Dict[str, str]] = Field(description="Value list")


class _MIoTSpecStdLibClass:
    """MIoT-Spec-V2 standard library class."""
    # pylint: disable=inconsistent-quotes
    _DOMAIN: str = "miot_specs"
    _NAME: str = "spec_std_lib"
    _main_loop: asyncio.AbstractEventLoop
    _storage: MIoTStorage
    _lang: str
    _devices: Dict[str, Dict[str, str]]
    _services: Dict[str, Dict[str, str]]
    _properties: Dict[str, Dict[str, str]]
    _events: Dict[str, Dict[str, str]]
    _actions: Dict[str, Dict[str, str]]
    _values: Dict[str, Dict[str, str]]

    def __init__(
        self,
        storage: MIoTStorage,
        lang: Optional[str] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        self._main_loop = loop or asyncio.get_running_loop()
        self._storage = storage
        self._lang = lang or SYSTEM_LANGUAGE_DEFAULT
        self._devices = {}
        self._services = {}
        self._properties = {}
        self._events = {}
        self._actions = {}
        self._values = {}

        self._spec_std_lib = None

    async def init_async(self) -> None:
        """Init."""
        std_lib_cache = await self._storage.load_async(domain=self._DOMAIN, name=self._NAME, type_=dict)
        if (
            isinstance(std_lib_cache, Dict)
            and "data" in std_lib_cache
            and "ts" in std_lib_cache
            and isinstance(std_lib_cache["ts"], int)
            and int(time.time()) - std_lib_cache["ts"] < SPEC_STD_LIB_EFFECTIVE_TIME
        ):
            # Use the cache if the update time is less than 14 day
            _LOGGER.debug("use local spec std cache, ts->%s", std_lib_cache["ts"])
            self.__load(std_lib_cache["data"])
            return
        # Update spec std lib
        if not await self.refresh_async():
            if isinstance(std_lib_cache, Dict) and "data" in std_lib_cache:
                self.__load(std_lib_cache["data"])
                _LOGGER.info("get spec std lib failed, use local cache")
            else:
                _LOGGER.error("load spec std lib failed")

    async def deinit_async(self) -> None:
        """Deinit."""

    def __load(self, std_lib: Dict[str, Dict[str, Dict[str, str]]]) -> None:
        if (
            not isinstance(std_lib, Dict)
            or "devices" not in std_lib
            or "services" not in std_lib
            or "properties" not in std_lib
            or "events" not in std_lib
            or "actions" not in std_lib
            or "values" not in std_lib
        ):
            return
        self._devices = std_lib["devices"]
        self._services = std_lib["services"]
        self._properties = std_lib["properties"]
        self._events = std_lib["events"]
        self._actions = std_lib["actions"]
        self._values = std_lib["values"]

    def __dump(self) -> Dict[str, Dict[str, Dict[str, str]]]:
        # pylint: disable=unused-private-member
        return {
            "devices": self._devices,
            "services": self._services,
            "properties": self._properties,
            "events": self._events,
            "actions": self._actions,
            "values": self._values
        }

    def device_translate(self, key: str) -> Optional[str]:
        """device translate."""
        if not self._devices or key not in self._devices:
            return None
        if self._lang not in self._devices[key]:
            return self._devices[key].get(SYSTEM_LANGUAGE_DEFAULT, None)
        return self._devices[key][self._lang]

    def service_translate(self, key: str) -> Optional[str]:
        """service translate."""
        if not self._services or key not in self._services:
            return None
        if self._lang not in self._services[key]:
            return self._services[key].get(SYSTEM_LANGUAGE_DEFAULT, None)
        return self._services[key][self._lang]

    def property_translate(self, key: str) -> Optional[str]:
        """property translate."""
        if not self._properties or key not in self._properties:
            return None
        if self._lang not in self._properties[key]:
            return self._properties[key].get(SYSTEM_LANGUAGE_DEFAULT, None)
        return self._properties[key][self._lang]

    def event_translate(self, key: str) -> Optional[str]:
        """event translate."""
        if not self._events or key not in self._events:
            return None
        if self._lang not in self._events[key]:
            return self._events[key].get(SYSTEM_LANGUAGE_DEFAULT, None)
        return self._events[key][self._lang]

    def action_translate(self, key: str) -> Optional[str]:
        """action translate."""
        if not self._actions or key not in self._actions:
            return None
        if self._lang not in self._actions[key]:
            return self._actions[key].get(SYSTEM_LANGUAGE_DEFAULT, None)
        return self._actions[key][self._lang]

    def value_translate(self, key: str) -> Optional[str]:
        """value translate."""
        if not self._values or key not in self._values:
            return None
        if self._lang not in self._values[key]:
            return self._values[key].get(SYSTEM_LANGUAGE_DEFAULT, None)
        return self._values[key][self._lang]

    async def refresh_async(self) -> bool:
        """refresh spec std lib."""
        std_lib_new = await self.__request_from_cloud_async()
        if std_lib_new:
            self.__load(std_lib_new)
            if not await self._storage.save_async(
                domain=self._DOMAIN, name=self._NAME,
                data={
                    "data": std_lib_new,
                    "ts": int(time.time())
                }
            ):
                _LOGGER.error("save spec std lib failed")
            return True
        return False

    async def __request_from_cloud_async(self) -> Optional[Dict]:
        std_libs: Optional[Dict] = None
        for index in range(3):
            try:
                tasks: List = []
                # Get std lib
                for name in ["device", "service", "property", "event", "action"]:
                    tasks.append(self.__get_template_list(name=name))
                tasks.append(self.__get_property_value())
                # Async request
                results = await asyncio.gather(*tasks)
                if None in results:
                    raise MIoTSpecError("init failed, None in result")
                std_libs = {
                    "devices": results[0],
                    "services": results[1],
                    "properties": results[2],
                    "events": results[3],
                    "actions": results[4],
                    "values": results[5],
                }
                # Get external std lib, Power by LM
                tasks.clear()
                for name in ["device", "service", "property", "event", "action", "property_value"]:
                    tasks.append(
                        http_get_json_async(
                            url=f"https://cdn.cnbj1.fds.api.mi-img.com/res-conf/xiaomi-home/std_ex_{name}.json",
                            loop=self._main_loop
                        )
                    )
                results = await asyncio.gather(*tasks)
                if results[0]:
                    for key, value in results[0].items():
                        if key in std_libs["devices"]:
                            std_libs["devices"][key].update(value)
                        else:
                            std_libs["devices"][key] = value
                else:
                    _LOGGER.error("get external std lib failed, devices")
                if results[1]:
                    for key, value in results[1].items():
                        if key in std_libs["services"]:
                            std_libs["services"][key].update(value)
                        else:
                            std_libs["services"][key] = value
                else:
                    _LOGGER.error("get external std lib failed, services")
                if results[2]:
                    for key, value in results[2].items():
                        if key in std_libs["properties"]:
                            std_libs["properties"][key].update(value)
                        else:
                            std_libs["properties"][key] = value
                else:
                    _LOGGER.error("get external std lib failed, properties")
                if results[3]:
                    for key, value in results[3].items():
                        if key in std_libs["events"]:
                            std_libs["events"][key].update(value)
                        else:
                            std_libs["events"][key] = value
                else:
                    _LOGGER.error("get external std lib failed, events")
                if results[4]:
                    for key, value in results[4].items():
                        if key in std_libs["actions"]:
                            std_libs["actions"][key].update(value)
                        else:
                            std_libs["actions"][key] = value
                else:
                    _LOGGER.error("get external std lib failed, actions")
                if results[5]:
                    for key, value in results[5].items():
                        if key in std_libs["values"]:
                            std_libs["values"][key].update(value)
                        else:
                            std_libs["values"][key] = value
                else:
                    _LOGGER.error(
                        "get external std lib failed, values")
                return std_libs
            except Exception as err:  # pylint: disable=broad-exception-caught
                _LOGGER.error(
                    "update spec std lib error, retry, %d, %s", index, err)
        return None

    async def __get_property_value(self) -> Dict:
        reply = await http_get_json_async(
            url="https://miot-spec.org/miot-spec-v2/normalization/list/property_value", loop=self._main_loop)
        if reply is None or "result" not in reply:
            raise MIoTSpecError("get property value failed")
        result = {}
        for item in reply["result"]:
            if (
                not isinstance(item, Dict)
                or "normalization" not in item
                or "description" not in item
                or "proName" not in item
                or "urn" not in item
            ):
                continue
            result[f"{item['urn']}|{item['proName']}|{item['normalization']}"] = {
                "zh-Hans": item["description"],
                "en": item["normalization"]
            }
        return result

    async def __get_template_list(self, name: str) -> Dict:
        reply = await http_get_json_async(
            url="https://miot-spec.org/miot-spec-v2/template/list/" + name,
            loop=self._main_loop)
        if reply is None or "result" not in reply:
            raise MIoTSpecError(f"get service failed, {name}")
        result: Dict = {}
        for item in reply["result"]:
            if not isinstance(item, Dict) or "type" not in item or "description" not in item:
                continue
            if "zh_cn" in item["description"]:
                item["description"]["zh-Hans"] = item["description"].pop("zh_cn")
            if "zh_hk" in item["description"]:
                item["description"]["zh-Hant"] = item["description"].pop("zh_hk")
                item["description"].pop("zh_tw", None)
            elif "zh_tw" in item["description"]:
                item["description"]["zh-Hant"] = item["description"].pop("zh_tw")
            result[item["type"]] = item["description"]
        return result


class _MIoTSpecBase(BaseModel):
    """MIoT SPEC base class."""
    iid: int = Field(description="MIoT SPEC Instance ID")
    name: str = Field(description="MIoT SPEC name")
    type_: str = Field(description="MIoT SPEC urn", alias="type", serialization_alias="type")
    description: str = Field(description="MIoT SPEC description")
    description_trans: str = Field(description="MIoT SPEC description translate")

    type_level: MIoTSpecTypeLevel = Field(default=MIoTSpecTypeLevel.UNKNOWN, description="MIoT SPEC type level")
    proprietary: bool = Field(default=False, description="MIoT SPEC proprietary")
    need_filter: bool = Field(default=False, description="MIoT SPEC need filter")

    model_config = ConfigDict(populate_by_name=True)


class MIoTSpecProperty(_MIoTSpecBase):
    """MIoT SPEC property class."""
    # Spec v2: "bool","uint8","uint16","uint32","uint64","int8","int16","int32","int64","float","string"
    # Spec v3: "uint8","int8","uint16","int16","uint32","int32","uint64","int64","float","string","bool",
    #   "iids","array","struct"
    format: str = Field(description="Property format")
    access: List[str] = Field(description="Property access")
    unit: Optional[str] = Field(description="MIoT SPEC unit", default=None)
    value_range: Optional[MIoTSpecValueRange] = Field(
        alias="value-range", serialization_alias="value-range", default=None, description="Property value range")
    value_list: Optional[List[MIoTSpecValueListItem]] = Field(
        alias="value-list", serialization_alias="value-list", default=None, description="Property value list")

    @property
    def readable(self):
        """Is readable."""
        return "read" in self.access

    @property
    def writable(self):
        """Is writable."""
        return "write" in self.access

    @property
    def notify(self):
        """Is notify."""
        return "notify" in self.access


class MIoTSpecEvent(_MIoTSpecBase):
    """MIoT SPEC event class."""
    arguments: List[MIoTSpecProperty] = Field(description="Event arguments", default=[])
    # service: "MIoTSpecService"


class MIoTSpecAction(_MIoTSpecBase):
    """MIoT SPEC action class."""
    in_: List[MIoTSpecProperty] = Field(alias="in", serialization_alias="in", default=[], description="Action input")
    out: List[MIoTSpecProperty] = Field(default=[], description="Action output")
    # service: "MIoTSpecService"


class MIoTSpecService(_MIoTSpecBase):
    """MIoT SPEC service class."""
    properties: List[MIoTSpecProperty] = Field(default_factory=list, description="Service properties")
    events: List[MIoTSpecEvent] = Field(default_factory=list, description="Service events")
    actions: List[MIoTSpecAction] = Field(default_factory=list, description="Service actions")


class MIoTSpecDevice(BaseModel):
    """MIoT SPEC device class."""
    urn: str = Field(description="MIoT SPEC urn")
    name: str = Field(description="MIoT SPEC name")
    # urn_name: str
    description: str = Field(description="MIoTSpecDevice description")
    description_trans: str = Field(description="MIoTSpecDevice description translate")
    services: List[MIoTSpecService] = Field(default=[], description="Device services")

    model_config = ConfigDict(populate_by_name=True)


class MIoTSpecDeviceLite(BaseModel):
    """MIoT device spec lite."""
    iid: str = Field(description="SPEC Instance ID")
    description: str = Field(description="SPEC name")
    # int
    format: str = Field(description="SPEC format")
    writeable: bool = Field(description="Writeable")
    readable: bool = Field(description="Readable")
    unit: Optional[str] = Field(default=None, description="SPEC unit")
    value_range: Optional[MIoTSpecValueRange] = Field(default=None, description="SPEC value range")
    value_list: Optional[List[MIoTSpecValueListItem]] = Field(default=None, description="SPEC value list")


class _MIoTSpecMultiLang:
    """MIoT SPEC multi lang class."""
    _DOMAIN: str = "miot_specs_multi_lang"
    _storage: MIoTStorage
    _lang: str
    _main_loop: asyncio.AbstractEventLoop

    _custom_cache: Dict[str, Dict]
    _current_data: Optional[Dict[str, str]]

    def __init__(
        self,
        storage: MIoTStorage,
        lang: Optional[str] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        self._storage = storage
        self._lang = lang or SYSTEM_LANGUAGE_DEFAULT
        self._main_loop = loop or asyncio.get_running_loop()

        self._custom_cache = {}
        self._current_data = None

    async def set_spec_async(self, urn: str) -> None:
        """Set spec."""
        if urn in self._custom_cache:
            self._current_data = self._custom_cache[urn]
            return

        trans_cache: Dict[str, str] = {}
        trans_cloud: Dict = {}
        trans_local: Dict = {}
        # Get multi lang from cloud
        try:
            trans_cloud = await self.__get_multi_lang_async(urn)
            if self._lang == "zh-Hans":
                # Simplified Chinese
                trans_cache = trans_cloud.get("zh_cn", {})
            elif self._lang == "zh-Hant":
                # Traditional Chinese, zh_hk or zh_tw
                trans_cache = trans_cloud.get("zh_hk", {})
                if not trans_cache:
                    trans_cache = trans_cloud.get("zh_tw", {})
            else:
                trans_cache = trans_cloud.get(self._lang, {})
        except Exception as err:  # pylint: disable=broad-except
            trans_cloud = {}
            _LOGGER.info("get multi lang from cloud failed, %s, %s", urn, err)
        # Get multi lang from local
        try:
            trans_local = await self._storage.load_async(
                domain=self._DOMAIN, name=urn, type_=Dict)  # type: ignore
            if (
                isinstance(trans_local, Dict)
                and self._lang in trans_local
            ):
                trans_cache.update(trans_local[self._lang])
        except Exception as err:  # pylint: disable=broad-except
            trans_local = {}
            _LOGGER.info("get multi lang from local failed, %s, %s", urn, err)
        # Default language
        if not trans_cache:
            if trans_cloud and SYSTEM_LANGUAGE_DEFAULT in trans_cloud:
                trans_cache = trans_cloud[SYSTEM_LANGUAGE_DEFAULT]
            if trans_local and SYSTEM_LANGUAGE_DEFAULT in trans_local:
                trans_cache.update(
                    trans_local[SYSTEM_LANGUAGE_DEFAULT])
        trans_data: Dict[str, str] = {}
        for tag, value in trans_cache.items():
            if value is None or value.strip() == "":
                continue
            # The Dict key is like:
            # "service:002:property:001:valuelist:000" or
            # "service:002:property:001" or "service:002"
            strs: List = tag.split(":")
            strs_len = len(strs)
            if strs_len == 2:
                trans_data[f"s:{int(strs[1])}"] = value
            elif strs_len == 4:
                type_ = "p" if strs[2] == "property" else (
                    "a" if strs[2] == "action" else "e")
                trans_data[
                    f"{type_}:{int(strs[1])}:{int(strs[3])}"
                ] = value
            elif strs_len == 6:
                trans_data[
                    f"v:{int(strs[1])}:{int(strs[3])}:{int(strs[5])}"
                ] = value

        self._custom_cache[urn] = trans_data
        self._current_data = trans_data

    def translate(self, key: str) -> Optional[str]:
        """Translate."""
        if not self._current_data:
            return None
        return self._current_data.get(key, None)

    async def __get_multi_lang_async(self, urn: str) -> Dict:
        res_trans = await http_get_json_async(
            url="https://miot-spec.org/instance/v2/multiLanguage",
            params={"urn": urn},
            loop=self._main_loop)
        if (
            not isinstance(res_trans, Dict)
            or "data" not in res_trans
            or not isinstance(res_trans["data"], Dict)
        ):
            raise MIoTSpecError("invalid translation data")
        return res_trans["data"]


class _SpecBoolTranslation:
    """
    Boolean value translation.
    """
    _BOOL_TRANS_FILE = "specs/bool_trans.yaml"
    _main_loop: asyncio.AbstractEventLoop
    _lang: str
    _data: Optional[Dict[str, List]]
    _data_default: Optional[List[Dict]]

    def __init__(
        self, lang: str, loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        self._main_loop = loop or asyncio.get_event_loop()
        self._lang = lang
        self._data = None
        self._data_default = None

    async def init_async(self) -> None:
        """Init."""
        if isinstance(self._data, Dict):
            return
        data = None
        self._data = {}
        try:
            data = await self._main_loop.run_in_executor(
                None,
                load_yaml_file,
                str(Path(__file__).parent / self._BOOL_TRANS_FILE))
        except Exception as err:  # pylint: disable=broad-exception-caught
            _LOGGER.error("bool trans, load file error, %s", err)
            return
        # Check if the file is a valid file
        if (
            not isinstance(data, Dict)
            or "data" not in data
            or not isinstance(data["data"], Dict)
            or "translate" not in data
            or not isinstance(data["translate"], Dict)
        ):
            _LOGGER.error("bool trans, valid file")
            return

        if "default" in data["translate"]:
            data_default = (
                data["translate"]["default"].get(self._lang, None)
                or data["translate"]["default"].get(
                    SYSTEM_LANGUAGE_DEFAULT, None))
            if data_default:
                self._data_default = [
                    {"value": True, "description": data_default["true"]},
                    {"value": False, "description": data_default["false"]}
                ]

        for urn, key in data["data"].items():
            if key not in data["translate"]:
                _LOGGER.error("bool trans, unknown key, %s, %s", urn, key)
                continue
            trans_data = (
                data["translate"][key].get(self._lang, None)
                or data["translate"][key].get(
                    SYSTEM_LANGUAGE_DEFAULT, None))
            if trans_data:
                self._data[urn] = [
                    {"value": True, "description": trans_data["true"]},
                    {"value": False, "description": trans_data["false"]}
                ]

    async def deinit_async(self) -> None:
        """Deinit."""
        self._data = None
        self._data_default = None

    async def translate_async(self, urn: str) -> Optional[List[Dict]]:
        """
        MUST call init_async() before calling this method.
        [
            {"value": True, "description": "True"},
            {"value": False, "description": "False"}
        ]
        """
        if not self._data or urn not in self._data:
            return self._data_default
        return self._data[urn]


class _SpecFilter:
    """
    MIoT-Spec-V2 filter for entity conversion.
    """
    _SPEC_FILTER_FILE = "specs/spec_filter.yaml"
    _main_loop: asyncio.AbstractEventLoop
    _data: Optional[Dict[str, Dict[str, set]]]
    _cache: Optional[Dict]

    def __init__(self, loop: Optional[asyncio.AbstractEventLoop]) -> None:
        self._main_loop = loop or asyncio.get_event_loop()
        self._data = None
        self._cache = None

    async def init_async(self) -> None:
        """Init."""
        if isinstance(self._data, Dict):
            return
        filter_data = None
        self._data = {}
        try:
            filter_data = await self._main_loop.run_in_executor(
                None,
                load_yaml_file,
                str(Path(__file__).parent / self._SPEC_FILTER_FILE))
        except Exception as err:  # pylint: disable=broad-exception-caught
            _LOGGER.error("spec filter, load file error, %s", err)
            return
        if not isinstance(filter_data, Dict):
            _LOGGER.error("spec filter, invalid spec filter content")
            return
        for values in list(filter_data.values()):
            if not isinstance(values, Dict):
                _LOGGER.error("spec filter, invalid spec filter data")
                return
            for value in values.values():
                if not isinstance(value, List):
                    _LOGGER.error("spec filter, invalid spec filter rules")
                    return

        self._data = filter_data

    async def deinit_async(self) -> None:
        """Deinit."""
        self._cache = None
        self._data = None

    async def set_spec_spec(self, urn_key: str) -> None:
        """MUST call init_async() first."""
        if not self._data:
            return
        self._cache = self._data.get(urn_key, None)

    def filter_service(self, siid: int) -> bool:
        """Filter service by siid.
        MUST call init_async() and set_spec_spec() first."""
        if (
            self._cache
            and "services" in self._cache
            and (
                str(siid) in self._cache["services"]
                or "*" in self._cache["services"])
        ):
            return True

        return False

    def filter_property(self, siid: int, piid: int) -> bool:
        """Filter property by piid.
        MUST call init_async() and set_spec_spec() first."""
        if (
            self._cache
            and "properties" in self._cache
            and (
                f"{siid}.{piid}" in self._cache["properties"]
                or f"{siid}.*" in self._cache["properties"])
        ):
            return True
        return False

    def filter_event(self, siid: int, eiid: int) -> bool:
        """Filter event by eiid.
        MUST call init_async() and set_spec_spec() first."""
        if (
            self._cache
            and "events" in self._cache
            and (
                f"{siid}.{eiid}" in self._cache["events"]
                or f"{siid}.*" in self._cache["events"]
            )
        ):
            return True
        return False

    def filter_action(self, siid: int, aiid: int) -> bool:
        """"Filter action by aiid.
        MUST call init_async() and set_spec_spec() first."""
        if (
            self._cache
            and "actions" in self._cache
            and (
                f"{siid}.{aiid}" in self._cache["actions"]
                or f"{siid}.*" in self._cache["actions"])
        ):
            return True
        return False


class _SpecModify:
    """MIoT-Spec-V2 modify for entity conversion."""
    _SPEC_MODIFY_FILE = "specs/spec_modify.yaml"
    _main_loop: asyncio.AbstractEventLoop
    _data: Optional[Dict]
    _selected: Optional[Dict]

    def __init__(
        self, loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        self._main_loop = loop or asyncio.get_running_loop()
        self._data = None

    async def init_async(self) -> None:
        """Init."""
        if isinstance(self._data, Dict):
            return
        modify_data = None
        self._data = {}
        self._selected = None
        try:
            modify_data = await self._main_loop.run_in_executor(
                None,
                load_yaml_file,
                str(Path(__file__).parent / self._SPEC_MODIFY_FILE))
        except Exception as err:  # pylint: disable=broad-exception-caught
            _LOGGER.error("spec modify, load file error, %s", err)
            return
        if not isinstance(modify_data, Dict):
            _LOGGER.error("spec modify, invalid spec modify content")
            return
        for key, value in modify_data.items():
            if not isinstance(key, str) or not isinstance(value, (Dict, str)):
                _LOGGER.error("spec modify, invalid spec modify data")
                return

        self._data = modify_data

    async def deinit_async(self) -> None:
        """Deinit."""
        self._data = None
        self._selected = None

    async def set_spec_async(self, urn: str) -> None:
        if not self._data:
            return
        self._selected = self._data.get(urn, None)
        if isinstance(self._selected, str):
            return await self.set_spec_async(urn=self._selected)

    def get_prop_unit(self, siid: int, piid: int) -> Optional[str]:
        return self.__get_prop_item(siid=siid, piid=piid, key="unit")

    def get_prop_expr(self, siid: int, piid: int) -> Optional[str]:
        return self.__get_prop_item(siid=siid, piid=piid, key="expr")

    def get_prop_icon(self, siid: int, piid: int) -> Optional[str]:
        return self.__get_prop_item(siid=siid, piid=piid, key="icon")

    def get_prop_access(self, siid: int, piid: int) -> Optional[List]:
        access = self.__get_prop_item(siid=siid, piid=piid, key="access")
        if not isinstance(access, List):
            return None
        return access

    def __get_prop_item(self, siid: int, piid: int, key: str) -> Optional[str]:
        if not self._selected:
            return None
        prop = self._selected.get(f"prop.{siid}.{piid}", None)
        if not prop:
            return None
        return prop.get(key, None)


class MIoTSpecServiceType(BaseModel):
    """MIoT-Spec-V2 service type."""
    description: Dict[str, str] = Field(default_factory=dict, description="Description")
    required_properties: List[str] = Field(
        default_factory=list, alias="required-properties", description="Required properties")
    optional_properties: List[str] = Field(
        default_factory=list, alias="optional-properties", description="Optional properties")
    required_actions: List[str] = Field(
        default_factory=list, alias="required-actions", description="Required actions")
    optional_actions: List[str] = Field(
        default_factory=list, alias="optional-actions", description="Optional actions")
    required_events: List[str] = Field(
        default_factory=list, alias="required-events", description="Required events")
    optional_events: List[str] = Field(
        default_factory=list, alias="optional-events", description="Optional events")

    model_config = ConfigDict(populate_by_name=True)


class MIoTSpecDeviceType(BaseModel):
    """MIoT-Spec-V2 device type."""
    description: Dict[str, str] = Field(description="Description")
    required_services: List[str] = Field(
        default_factory=list, alias="required-services", description="Required services")
    optional_services: List[str] = Field(
        default_factory=list, alias="optional-services", description="Optional services")

    model_config = ConfigDict(populate_by_name=True)


class MIoTSpecType(BaseModel):
    """MIoT-Spec-V2 type."""
    ts: int = Field(default=0, description="Timestamp")
    devices: Dict[str, MIoTSpecDeviceType] = Field(description="Devices types")
    services: Dict[str, MIoTSpecServiceType] = Field(description="Services types")


class MIoTSpecTypeClass:
    """MIoT-Spec-V2 types."""
    _DOMAIN = "miot_specs"
    _NAME = "spec_types"
    _main_loop: asyncio.AbstractEventLoop
    _storage: MIoTStorage

    _data: MIoTSpecType

    def __init__(self, storage: MIoTStorage, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        self._main_loop = loop or asyncio.get_running_loop()
        self._storage = storage

    @property
    def data(self) -> MIoTSpecType:
        """Get spec types."""
        return self._data

    async def init_async(self) -> None:
        """Init."""
        cache = await self._storage.load_async(domain=self._DOMAIN, name=self._NAME, type_=dict)
        if isinstance(cache, Dict) and int(time.time()) - cache.get("ts", 0) < SPEC_STD_LIB_EFFECTIVE_TIME:
            self._data = MIoTSpecType.model_validate(obj=cache)
            _LOGGER.info("load spec types from cache, %s", self._data.ts)
            return

        if not await self.refresh_async():
            if isinstance(cache, Dict) and "devices" in cache and "services" in cache:
                self._data = MIoTSpecType.model_validate(obj=cache)
                _LOGGER.info("load spec types from cache failed, use cache, %s", self._data.ts)
            else:
                _LOGGER.error("load spec types failed")

    async def deinit_async(self) -> None:
        """Deinit."""

    def get_service_type(self, device_name: str, service_name: str) -> MIoTSpecTypeLevel:
        """Get device type."""
        if device_name not in self._data.devices:
            return MIoTSpecTypeLevel.UNKNOWN
        if service_name in self._data.devices[device_name].required_services:
            return MIoTSpecTypeLevel.REQUIRED
        if service_name in self._data.devices[device_name].optional_services:
            return MIoTSpecTypeLevel.OPTIONAL
        return MIoTSpecTypeLevel.UNKNOWN

    def get_property_type(self, service_name: str, property_name: str) -> MIoTSpecTypeLevel:
        """Get property type."""
        if service_name not in self._data.services:
            return MIoTSpecTypeLevel.UNKNOWN
        if property_name in self._data.services[service_name].required_properties:
            return MIoTSpecTypeLevel.REQUIRED
        if property_name in self._data.services[service_name].optional_properties:
            return MIoTSpecTypeLevel.OPTIONAL
        return MIoTSpecTypeLevel.UNKNOWN

    def get_action_type(self, service_name: str, action_name: str) -> MIoTSpecTypeLevel:
        """Get action type."""
        if service_name not in self._data.services:
            return MIoTSpecTypeLevel.UNKNOWN
        if action_name in self._data.services[service_name].required_actions:
            return MIoTSpecTypeLevel.REQUIRED
        if action_name in self._data.services[service_name].optional_actions:
            return MIoTSpecTypeLevel.OPTIONAL
        return MIoTSpecTypeLevel.UNKNOWN

    def get_event_type(self, service_name: str, event_name: str) -> MIoTSpecTypeLevel:
        """Get event type."""
        if service_name not in self._data.services:
            return MIoTSpecTypeLevel.UNKNOWN
        if event_name in self._data.services[service_name].required_events:
            return MIoTSpecTypeLevel.REQUIRED
        if event_name in self._data.services[service_name].optional_events:
            return MIoTSpecTypeLevel.OPTIONAL
        return MIoTSpecTypeLevel.UNKNOWN

    async def refresh_async(self) -> bool:
        """refresh spec types."""
        device_types = await self.__get_device_types()
        if not device_types:
            _LOGGER.error("get device types failed")
            return False
        service_types = await self.__get_service_types()
        if not service_types:
            _LOGGER.error("get service types failed")
            return False

        self._data = MIoTSpecType.model_validate(obj={
            "ts": int(time.time()),
            "devices": device_types,
            "services": service_types
        })
        if not await self._storage.save_async(
                domain=self._DOMAIN, name=self._NAME, data=self._data.model_dump(by_alias=True)):
            _LOGGER.error("save spec types failed")
        return True

    async def __get_device_types(self) -> Optional[Dict[str, MIoTSpecDeviceType]]:
        """Get device types."""
        type_list: Dict[str, List[str]] = await http_get_json_async(
            url="http://miot-spec.org/miot-spec-v2/spec/devices", loop=self._main_loop)
        if "types" not in type_list or not isinstance(type_list["types"], List):
            _LOGGER.error("get device types failed, invalid types")
            return None
        task_list = []
        for type_item in type_list["types"]:
            task_list.append(http_get_json_async(
                url="https://miot-spec.org/miot-spec-v2/spec/device?type="+type_item,
                loop=self._main_loop))
        task_result = await asyncio.gather(*task_list, return_exceptions=True)
        result: Dict[str, MIoTSpecDeviceType] = {}
        for type_device, type_info in zip(type_list["types"], task_result):
            if not isinstance(type_info, Dict):
                _LOGGER.error("get device types failed, invalid type info, %s, %s", type_device, type_info)
                continue
            result[type_device.split(":")[3]] = MIoTSpecDeviceType.model_validate(obj={
                "description": {"en": type_info.get("description", "")},
                "required-services": [
                    type_service.split(":")[3] for type_service in type_info.get("required-services", [])
                    if type_service.split(":")[3] != "device-information"],
                "optional-services": [
                    type_service.split(":")[3] for type_service in type_info.get("optional-services", [])]
            })
        return result

    async def __get_service_types(self) -> Optional[Dict[str, MIoTSpecServiceType]]:
        """Get service types."""
        type_list: Dict[str, List[str]] = await http_get_json_async(
            url="http://miot-spec.org/miot-spec-v2/spec/services", loop=self._main_loop)
        if "types" not in type_list or not isinstance(type_list["types"], List):
            _LOGGER.error("get service types failed, invalid types")
            return None
        task_list = []
        for type_item in type_list["types"]:
            task_list.append(http_get_json_async(
                url="https://miot-spec.org/miot-spec-v2/spec/service?type="+type_item,
                loop=self._main_loop))
        task_result = await asyncio.gather(*task_list, return_exceptions=True)
        result: Dict[str, MIoTSpecServiceType] = {}
        for type_service, type_info in zip(type_list["types"], task_result):
            if not isinstance(type_info, Dict):
                _LOGGER.error("get service types failed, invalid type info, %s, %s", type_service, type_info)
                continue
            result[type_service.split(":")[3]] = MIoTSpecServiceType.model_validate(obj={
                "description": {"en": type_info.get("description", "")},
                "required-properties": [
                    type_prop.split(":")[3] for type_prop in type_info.get("required-properties", [])],
                "optional-properties": [
                    type_prop.split(":")[3] for type_prop in type_info.get("optional-properties", [])],
                "required-actions": [
                    type_action.split(":")[3] for type_action in type_info.get("required-actions", [])],
                "optional-actions": [
                    type_action.split(":")[3] for type_action in type_info.get("optional-actions", [])],
                "required-events": [
                    type_event.split(":")[3] for type_event in type_info.get("required-events", [])],
                "optional-events": [
                    type_event.split(":")[3] for type_event in type_info.get("optional-events", [])],
            })
        return result


class MIoTSpecParser:
    """MIoT SPEC parser."""
    # pylint: disable=inconsistent-quotes
    VERSION: int = 1
    _DOMAIN: str = "miot_specs"
    _lang: str
    _storage: MIoTStorage
    _main_loop: asyncio.AbstractEventLoop

    _std_lib: _MIoTSpecStdLibClass
    _spec_types: MIoTSpecTypeClass
    _multi_lang: _MIoTSpecMultiLang
    _bool_trans: _SpecBoolTranslation
    _spec_filter: _SpecFilter
    # _spec_modify: _SpecModify

    _init_done: bool

    def __init__(
        self,
        storage: MIoTStorage,
        lang: Optional[str],
        loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        self._lang = lang or SYSTEM_LANGUAGE_DEFAULT
        self._storage = storage
        self._main_loop = loop or asyncio.get_running_loop()
        self._std_lib = _MIoTSpecStdLibClass(storage=self._storage, lang=self._lang, loop=self._main_loop)
        self._spec_types = MIoTSpecTypeClass(storage=self._storage, loop=self._main_loop)
        self._multi_lang = _MIoTSpecMultiLang(storage=self._storage, lang=self._lang, loop=self._main_loop)
        self._bool_trans = _SpecBoolTranslation(lang=self._lang, loop=self._main_loop)
        self._spec_filter = _SpecFilter(loop=self._main_loop)
        # self._spec_modify = _SpecModify(loop=self._main_loop)

        self._init_done = False

    async def init_async(self) -> None:
        """Init."""
        if self._init_done is True:
            return
        await self._std_lib.init_async()
        await self._spec_types.init_async()
        await self._bool_trans.init_async()
        await self._spec_filter.init_async()
        # await self._spec_modify.init_async()

        self._init_done = True

    async def deinit_async(self) -> None:
        """Deinit."""
        self._init_done = False
        # await self._std_lib.deinit()
        await self._std_lib.deinit_async()
        await self._bool_trans.deinit_async()
        await self._spec_filter.deinit_async()
        # await self._spec_modify.deinit_async()

    async def parse_async(
        self, urn: str, skip_cache: bool = False,
    ) -> Optional[MIoTSpecDevice]:
        """MUST await init first !!!"""
        if not skip_cache:
            cache_result = await self.__cache_get(urn=urn)
            if isinstance(cache_result, Dict):
                _LOGGER.debug("get from cache, %s", urn)
                return MIoTSpecDevice(**cache_result)
        # Retry three times
        for index in range(3):
            try:
                return await self.__parse(urn=urn)
            except Exception as err:  # pylint: disable=broad-exception-caught
                _LOGGER.error("parse error, retry, %d, %s, %s", index, urn, err)
        return None

    async def parse_lite_async(
        self,
        urn: str,
        skip_cache: bool = False,
        spec_service_level: MIoTSpecTypeLevel = MIoTSpecTypeLevel.OPTIONAL,
        spec_property_level: MIoTSpecTypeLevel = MIoTSpecTypeLevel.OPTIONAL,
        spec_action_level: MIoTSpecTypeLevel = MIoTSpecTypeLevel.OPTIONAL,
        skip_proprietary: bool = False,
    ) -> Optional[Dict[str, MIoTSpecDeviceLite]]:
        """Get lite spec for LLM."""
        spec_device = await self.parse_async(urn=urn, skip_cache=skip_cache)
        if not spec_device:
            return None
        result: Dict[str, MIoTSpecDeviceLite] = {}
        for spec_service in spec_device.services:
            if not skip_proprietary and spec_service.proprietary:
                continue
            if spec_service.type_level < spec_service_level:
                continue
            for spec_property in spec_service.properties:
                if not skip_proprietary and spec_property.proprietary:
                    continue
                if spec_property.type_level < spec_property_level:
                    continue
                iid = f"prop.0.{spec_service.iid}.{spec_property.iid}"
                name = spec_service.description_trans
                if spec_service.description_trans != spec_property.description_trans:
                    name += f" {spec_property.description_trans}"
                result[iid] = MIoTSpecDeviceLite(
                    iid=iid,
                    description=name,
                    format=spec_property.format,
                    writeable=spec_property.writable,
                    readable=spec_property.readable,
                    value_range=spec_property.value_range,
                    value_list=spec_property.value_list,
                )
            for spec_action in spec_service.actions:
                if not skip_proprietary and spec_action.proprietary:
                    continue
                if spec_action.type_level < spec_action_level:
                    continue
                iid = f"action.0.{spec_service.iid}.{spec_action.iid}"
                name = spec_service.description_trans
                if spec_service.description_trans != spec_action.description_trans:
                    name += f" {spec_action.description_trans}"
                in_list = []
                for spec_property in spec_action.in_:
                    in_list.append(f"{spec_property.description_trans}: {spec_property.format}")
                    # for spec_property in spec_service.properties:
                    #     if spec_property.iid == prop_iid:
                    #         in_list.append(f"{spec_property.description_trans}: {spec_property.format}")
                    #         break
                result[iid] = MIoTSpecDeviceLite(
                    iid=iid,
                    description=name,
                    format=json.dumps(in_list, ensure_ascii=False),
                    writeable=True,
                    readable=False
                )
        return result

    async def refresh_async(self, urn_list: List[str]) -> int:
        """MUST await init first !!!"""
        if not urn_list:
            return False
        if not await self._std_lib.refresh_async():
            raise MIoTSpecError("get spec std lib failed")
        success_count = 0
        for index in range(0, len(urn_list), 5):
            batch = urn_list[index:index+5]
            task_list = [self._main_loop.create_task(self.parse_async(urn=urn, skip_cache=True)) for urn in batch]
            results = await asyncio.gather(*task_list)
            success_count += sum(1 for result in results if result is not None)
        return success_count

    async def __cache_get(self, urn: str) -> Optional[Dict]:
        if platform.system() == "Windows":
            urn = urn.replace(":", "_")
        return await self._storage.load_async(
            domain=self._DOMAIN,
            name=f"{urn}_{self._lang}",
            type_=dict)  # type: ignore

    async def __cache_set(self, urn: str, data: Dict) -> bool:
        if platform.system() == "Windows":
            urn = urn.replace(":", "_")
        return await self._storage.save_async(domain=self._DOMAIN, name=f"{urn}_{self._lang}", data=data)

    async def __get_instance(self, urn: str) -> Optional[Dict]:
        return await http_get_json_async(
            url="https://miot-spec.org/miot-spec-v2/instance",
            params={"type": urn},
            loop=self._main_loop)

    async def __parse(self, urn: str) -> MIoTSpecDevice:
        _LOGGER.debug("parse urn, %s", urn)
        # Load spec instance
        instance = await self.__get_instance(urn=urn)
        if (
            not isinstance(instance, Dict)
            or "type" not in instance
            or "description" not in instance
            or "services" not in instance
        ):
            raise MIoTSpecError(f"invalid urn instance, {urn}")
        urn_strs: List[str] = urn.split(":")
        urn_key: str = ":".join(urn_strs[:6])
        # Set translation cache
        await self._multi_lang.set_spec_async(urn=urn)
        # Set spec filter
        await self._spec_filter.set_spec_spec(urn_key=urn_key)
        # Set spec modify
        # await self._spec_modify.set_spec_async(urn=urn)
        # Parse device type
        spec_device: MIoTSpecDevice = MIoTSpecDevice(
            urn=urn,
            name=urn_strs[3],
            description=instance["description"],
            description_trans=(
                self._std_lib.device_translate(key=":".join(urn_strs[:5]))
                or instance["description"]
                or urn_strs[3]))
        # Parse services
        spec_device.services = []
        for service in instance.get("services", []):
            if "iid" not in service or "type" not in service or "description" not in service:
                _LOGGER.error("invalid service, %s, %s", urn, service)
                continue
            type_strs: List[str] = service["type"].split(":")
            if type_strs[3] == "device-information":
                # Ignore device-information service
                continue
            spec_service: MIoTSpecService = MIoTSpecService(
                iid=service["iid"],
                name=type_strs[3],
                type=service["type"],
                description=service["description"],
                description_trans=(
                    self._multi_lang.translate(key=f"s:{service['iid']}")
                    or self._std_lib.service_translate(key=":".join(type_strs[:5]))
                    or service["description"]
                    or type_strs[3])
            )
            # Get spec service type level
            spec_service.type_level = self._spec_types.get_service_type(
                device_name=urn_strs[3], service_name=type_strs[3])
            # Filter spec service
            spec_service.need_filter = self._spec_filter.filter_service(siid=service["iid"])
            if type_strs[1] != "miot-spec-v2":
                spec_service.proprietary = True
            # Parse service property
            spec_service.properties = []
            for property_ in service.get("properties", []):
                if (
                    "iid" not in property_
                    or "type" not in property_
                    or "description" not in property_
                    or "format" not in property_
                    or "access" not in property_
                ):
                    continue
                p_type_strs: List[str] = property_["type"].split(":")
                # Handle special property.unit
                unit = property_.get("unit", None)
                spec_prop: MIoTSpecProperty = MIoTSpecProperty(
                    iid=property_["iid"],
                    name=p_type_strs[3],
                    type=property_["type"],
                    description=property_["description"],
                    description_trans=(
                        self._multi_lang.translate(key=f"p:{service['iid']}:{property_['iid']}")
                        or self._std_lib.property_translate(key=":".join(p_type_strs[:5]))
                        or property_["description"]
                        or p_type_strs[3]),
                    format=property_["format"],
                    access=property_["access"],
                    unit=unit if unit != "none" else None)
                # Get spec property type level
                spec_prop.type_level = self._spec_types.get_property_type(
                    service_name=type_strs[3], property_name=p_type_strs[3])
                # Filter spec property
                spec_prop.need_filter = (
                    spec_service.need_filter or self._spec_filter.filter_property(
                        siid=service["iid"], piid=property_["iid"]))
                if p_type_strs[1] != "miot-spec-v2":
                    spec_prop.proprietary = spec_service.proprietary or True
                if "value-range" in property_:
                    spec_prop.value_range = MIoTSpecValueRange(
                        min=property_["value-range"][0],
                        max=property_["value-range"][1],
                        step=property_["value-range"][2])
                elif "value-list" in property_:
                    v_list: List[Dict] = property_["value-list"]
                    spec_prop.value_list = []
                    for index, v in enumerate(v_list):
                        if v["description"].strip() == "":
                            v["description"] = f"v_{v['value']}"
                        v["name"] = v["description"]
                        v["description"] = (
                            self._multi_lang.translate(key=f"v:{service['iid']}:{property_['iid']}:{index}")
                            or self._std_lib.value_translate(key=f"{type_strs[:5]}|{p_type_strs[3]}|{v['description']}")
                            or v["name"])
                        spec_prop.value_list.append(MIoTSpecValueListItem.model_validate(obj=v))
                # elif property_["format"] == "bool":
                #     v_tag = ":".join(p_type_strs[:5])
                #     v_descriptions = await self._bool_trans.translate_async(urn=v_tag)
                #     if v_descriptions:
                #         # bool without value-list.name
                #         spec_prop.value_list = [MIoTSpecValueListItem.model_validate(
                #             obj=v_item) for v_item in v_descriptions]
                # Prop modify
                # spec_prop.unit = self._spec_modify.get_prop_unit(
                #     siid=service["iid"], piid=property_["iid"]) or spec_prop.unit
                spec_service.properties.append(spec_prop)
                # custom_access = self._spec_modify.get_prop_access(siid=service["iid"], piid=property_["iid"])
                # if custom_access:
                #     spec_prop.access = custom_access
            # Parse service event
            spec_service.events = []
            for event in service.get("events", []):
                if (
                    "iid" not in event
                    or "type" not in event
                    or "description" not in event
                    or "arguments" not in event
                ):
                    continue
                e_type_strs: List[str] = event["type"].split(":")
                spec_event: MIoTSpecEvent = MIoTSpecEvent(
                    iid=event["iid"],
                    name=e_type_strs[3],
                    type=event["type"],
                    description=event["description"],
                    description_trans=(
                        self._multi_lang.translate(key=f"e:{service['iid']}:{event['iid']}")
                        or self._std_lib.event_translate(key=":".join(e_type_strs[:5]))
                        or event["description"]
                        or e_type_strs[3]
                    )
                )
                # Get spec event type level
                spec_event.type_level = self._spec_types.get_event_type(
                    service_name=type_strs[3], event_name=e_type_strs[3])
                # Filter spec event
                spec_event.need_filter = (
                    spec_service.need_filter
                    or self._spec_filter.filter_event(siid=service["iid"], eiid=event["iid"]))
                if e_type_strs[1] != "miot-spec-v2":
                    spec_event.proprietary = spec_service.proprietary or True
                arg_list: List[MIoTSpecProperty] = []
                for piid in event["arguments"]:
                    for prop in spec_service.properties:
                        if prop.iid == piid:
                            arg_list.append(prop)
                            break
                spec_event.arguments = arg_list
                spec_service.events.append(spec_event)
            # Parse service action
            spec_service.actions = []
            for action in service.get("actions", []):
                if (
                    "iid" not in action
                    or "type" not in action
                    or "description" not in action
                    or "in" not in action
                ):
                    continue
                a_type_strs: List[str] = action["type"].split(":")
                spec_action: MIoTSpecAction = MIoTSpecAction(
                    iid=action["iid"],
                    name=a_type_strs[3],
                    type=action["type"],
                    description=action["description"],
                    description_trans=(
                        self._multi_lang.translate(key=f"a:{service['iid']}:{action['iid']}")
                        or self._std_lib.action_translate(key=":".join(a_type_strs[:5]))
                        or action["description"]
                        or a_type_strs[3]
                    )
                )
                # Get spec action type level
                spec_action.type_level = self._spec_types.get_action_type(
                    service_name=type_strs[3], action_name=a_type_strs[3])
                # Filter spec action
                spec_action.need_filter = (
                    spec_service.need_filter
                    or self._spec_filter.filter_action(siid=service["iid"], aiid=action["iid"]))
                if a_type_strs[1] != "miot-spec-v2":
                    spec_action.proprietary = spec_service.proprietary or True
                in_list: List[MIoTSpecProperty] = []
                for piid in action["in"]:
                    for prop in spec_service.properties:
                        if prop.iid == piid:
                            in_list.append(prop)
                            break
                spec_action.in_ = in_list
                out_list: List[MIoTSpecProperty] = []
                for piid in action["out"]:
                    for prop in spec_service.properties:
                        if prop.iid == piid:
                            out_list.append(prop)
                            break
                spec_action.out = out_list
                spec_service.actions.append(spec_action)
            spec_device.services.append(spec_service)

        await self.__cache_set(urn=urn, data=spec_device.model_dump(by_alias=True, exclude_none=True))
        return spec_device
