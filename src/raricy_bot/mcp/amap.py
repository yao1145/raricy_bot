"""高德地图适配：参数白名单与结果清洗。

上游是 `@amap/amap-maps-mcp-server@0.0.8`（npm，无 repository 字段，无法证明厂商官方），
只有一个发现到的实现，行为按它的 tarball 逐条读过：

- 成功返回**一个 text 块，内容是 pretty-printed JSON**，`isError=false`；
- 失败返回 `isError=true`，正文是 `Error: ${error.message}`。**这条很危险**：node-fetch 的
  message 带请求 URL，而 URL 里带 `key=` —— 错误正文一旦外泄就是 API key 外泄。
  好在两条独立防线都在这条路上：Registry 在适配器之前就 `_result_is_error` 短路，
  而 `text_blocks` 也会先抛。本模块只保证自己绝不把错误正文拼进任何返回值。

只白名单**单调用可完成**的三个工具（D-65）。周边搜索、逆地理编码、距离测量都要
「经度,纬度」，POI 详情要上一轮返回的 POI ID —— 单轮工具调用预算固定为 1，模型拿不到
这些输入，绑上去就是死工具。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..logging_setup import get_logger, log_event
from .adapter_kit import (
    CapabilityLimiter,
    clip_plain_tokens,
    text_blocks,
    truncate_plain,
)
from .contracts import McpNoResultsError, ToolExecution

_logger = get_logger("mcp.amap")

# 历史摘要的抬头；与 Exa 的 `[联网资料（不可信数据，仅供参考）]` 同源。
_HISTORY_HEAD = "[地图查询结果（不可信数据，仅供参考）]"


@dataclass(frozen=True)
class _Spec:
    """一个工具对模型公开的参数面。"""

    required: str
    optional: str | None
    description: str
    required_hint: str
    optional_hint: str | None


# 参数名与上游 handler 真正读取的字段逐字一致。
_SPECS: Mapping[str, _Spec] = {
    "maps_geo": _Spec(
        required="address",
        optional="city",
        description="把结构化的中文地址解析成经纬度坐标",
        required_hint="要解析的结构化地址，例如「上海市浦东新区世纪大道 1 号」",
        optional_hint="限定查询的城市，例如「上海」",
    ),
    "maps_text_search": _Spec(
        required="keywords",
        optional="city",
        description="按关键词搜索地点（POI），返回名称与地址",
        required_hint="搜索关键词，例如「加油站」「咖啡馆」",
        optional_hint="限定查询的城市，例如「上海」",
    ),
    "maps_weather": _Spec(
        required="city",
        optional=None,
        description="查询指定城市未来几天的天气预报",
        required_hint="城市名称或行政区划代码，例如「杭州」",
        optional_hint=None,
    ),
}


class AmapAdapter:
    """把一个高德工具的结果转为领域 ``ToolExecution``。

    每个绑定一个对象：Registry 只把适配器交给 ``prepare_arguments(arguments, feature)``
    （不带工具名），所以「哪个工具」必须在构造时就固定下来。
    """

    def __init__(
        self,
        feature: Any,
        limiter: CapabilityLimiter | None,
        *,
        tool: str,
    ) -> None:
        if tool not in _SPECS:
            raise ValueError(f"unsupported amap tool: {tool}")
        self.tool = tool
        self.limiter = limiter
        self.result_count = feature.result_count
        self.result_item_token_limit = feature.result_item_token_limit
        self.history_item_token_limit = feature.history_item_token_limit
        self.max_query_chars = feature.max_query_chars

    # --- 模型可见面 ---------------------------------------------------------

    def model_input_schema(self) -> dict[str, Any]:
        spec = _SPECS[self.tool]
        properties: dict[str, Any] = {
            spec.required: {"type": "string", "description": spec.required_hint}
        }
        if spec.optional is not None:
            properties[spec.optional] = {
                "type": "string",
                "description": spec.optional_hint,
            }
        return {
            "type": "object",
            "properties": properties,
            "required": [spec.required],
            # 未知字段一律不接受：模型多写的参数不会被转发给上游。
            "additionalProperties": False,
        }

    def prepare_arguments(
        self, arguments: Mapping[str, Any], _feature: Any
    ) -> dict[str, Any]:
        """只转发该工具真正会读的参数，其余全部丢弃。"""
        spec = _SPECS[self.tool]
        prepared: dict[str, Any] = {}
        for name in (spec.required, spec.optional):
            if name is None:
                continue
            value = arguments.get(name)
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError(f"{name} must be text")
            value = value.strip()
            if not value:
                raise ValueError(f"{name} is empty")
            if len(value) > self.max_query_chars:
                raise ValueError(f"{name} is too long")
            prepared[name] = value
        if spec.required not in prepared:
            raise ValueError(f"{spec.required} is required")
        return prepared

    # --- 结果 ---------------------------------------------------------------

    def adapt(self, raw: Any, call_id: str) -> ToolExecution:
        """解析成功结果；调用者负责把异常映射为稳定错误。"""
        payload = _json_payload(raw)
        items = _ITEMS[self.tool](payload, self.result_count)
        if not items:
            raise McpNoResultsError(f"no usable {self.tool} results")
        content = clip_plain_tokens(
            "\n\n".join(
                clip_plain_tokens(item, self.result_item_token_limit) for item in items
            ),
            max(1, self.result_count * self.result_item_token_limit),
        )
        # 历史只留压缩摘要；原始正文不进内存历史（D-35）。
        history = "\n".join(
            [_HISTORY_HEAD]
            + [truncate_plain(item, self.history_item_token_limit) for item in items]
        )
        log_event(_logger, logging.INFO, "mcp.map_done", tool=self.tool, count=len(items))
        return ToolExecution(
            call_id=call_id,
            content=content,
            is_error=False,
            history_context=history,
        )


def _json_payload(raw: Any) -> Mapping[str, Any]:
    """高德把 JSON 塞在单个 text 块里；解析失败即拒收。"""
    blocks = text_blocks(raw)
    try:
        payload = json.loads("\n".join(blocks))
    except (TypeError, ValueError) as exc:
        raise ValueError("amap payload is not JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("amap payload is not an object")
    return payload


def _join(parts: list[Any]) -> str:
    """把若干可空字段拼成一句，跳过空值。"""
    return "".join(str(part).strip() for part in parts if isinstance(part, str) and part.strip())


def _geo_items(payload: Mapping[str, Any], limit: int) -> list[str]:
    entries = payload.get("return")
    if not isinstance(entries, list):
        return []
    items: list[str] = []
    for entry in entries[:limit]:
        if not isinstance(entry, Mapping):
            continue
        address = _join(
            [
                entry.get("province"),
                entry.get("city"),
                entry.get("district"),
                entry.get("street"),
                entry.get("number"),
            ]
        )
        location = entry.get("location")
        if not address:
            continue
        line = f"地址：{address}"
        if isinstance(location, str) and _valid_lonlat(location):
            line += f"\n坐标：{location}"
        items.append(line)
    return items


def _text_search_items(payload: Mapping[str, Any], limit: int) -> list[str]:
    pois = payload.get("pois")
    if not isinstance(pois, list):
        return []
    items: list[str] = []
    for poi in pois[:limit]:
        if not isinstance(poi, Mapping):
            continue
        name = poi.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        line = f"名称：{name.strip()}"
        address = poi.get("address")
        # 只取 name 与 address：id / typecode 对聊天无用，photos 是外部图片地址，
        # 交给模型就可能被"引用"，而它对回复毫无价值（与 D-29 同一条理由）。
        if isinstance(address, str) and address.strip():
            line += f"\n地址：{address.strip()}"
        items.append(line)
    return items


def _weather_items(payload: Mapping[str, Any], limit: int) -> list[str]:
    city = payload.get("city")
    forecasts = payload.get("forecasts")
    if not isinstance(forecasts, list):
        return []
    items: list[str] = []
    header = city.strip() if isinstance(city, str) and city.strip() else ""
    for cast in forecasts[:limit]:
        if not isinstance(cast, Mapping):
            continue
        day = _join([cast.get("date")])
        weather = "/".join(
            part
            for part in (
                _join([cast.get("dayweather")]),
                _join([cast.get("nightweather")]),
            )
            if part
        )
        temp = "/".join(
            part
            for part in (
                _join([cast.get("daytemp")]),
                _join([cast.get("nighttemp")]),
            )
            if part
        )
        wind = "/".join(
            part
            for part in (
                _join([cast.get("daywind")]),
                _join([cast.get("nightwind")]),
            )
            if part
        )
        line = " ".join(part for part in (day, weather) if part)
        if temp:
            line += f"\n气温：{temp}℃"
        if wind:
            line += f"\n风：{wind}"
        if line:
            items.append(line)
    if items and header:
        items[0] = f"{header}\n{items[0]}"
    return items


def _valid_lonlat(value: str) -> bool:
    """高德的 location 形如「经度,纬度」；不合规就整条丢掉。"""
    parts = value.split(",")
    if len(parts) != 2:
        return False
    try:
        lon, lat = (float(part) for part in parts)
    except ValueError:
        return False
    return -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0


_ITEMS: Mapping[str, Any] = {
    "maps_geo": _geo_items,
    "maps_text_search": _text_search_items,
    "maps_weather": _weather_items,
}


def build(feature: Any, limiter: CapabilityLimiter, *, tool: str) -> AmapAdapter:
    """`mcp/adapters.py` 的工厂协议：一个绑定一个适配器对象。"""
    return AmapAdapter(feature, limiter, tool=tool)
