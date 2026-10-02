"""
AstrBot 函数工具：把 dna 数据层包装成模型可直接调用的工具。

工具面刻意保持「可发现 → 可检索 → 可读详情 → 可看原文」的层次，和 DNA Builder
资料检索 Agent 的工具设计一致：列表类工具只回摘要 + key，详情用另一个工具按 key 取，
剧情则单独给一组跨表工具。工具描述里写清了「下一步该用哪个工具」，因为模型的调用
顺序基本是被描述约束出来的。

依赖 AstrBot 的 FunctionTool 契约（>= 4.5.7 推荐写法）：
`async def call(self, context, **kwargs)`；旧的 `run(event, **kwargs)` 由框架兼容层兜底。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

try:  # AstrBot 运行时
    from astrbot.core.agent.tool import FunctionTool, ToolExecResult
except Exception:  # pragma: no cover - 离线自测环境没有 astrbot
    FunctionTool = object  # type: ignore[assignment,misc]
    ToolExecResult = Any  # type: ignore[misc]

from . import render, story
from .client import ALLOWED_FILTER_OPS, DnaError, clip

logger = logging.getLogger("astrbot_plugin_dna_builder")

RELATED_STORY_DATASETS = {"char", "npc"}
"""这些数据集只装数值与设定，人物的档案 / 语音 / 剧情在别的表里，返回时提醒一句。"""

RELATED_STORY_HINT = (
    "\n\n相关：这个人物还有角色档案、语音与剧情文本，用 dna_search_story"
    "（scope 可选 profile 档案 / voice 语音 / summary 剧情概要 / dialog 对话原文）查，"
    "不要只看 char 表就下结论。"
)


def _text(client: Any, content: str) -> str:
    """按配置的字符上限收敛工具返回文本（client 可为数据包或在线查询后端）。"""
    return clip(content, client.max_chars)


def _error(exc: Exception) -> str:
    """把异常转成模型能读懂、并且知道怎么改的提示。"""
    return f"查询失败：{exc}"


def _as_int(value: Any, default: int, low: int, high: int) -> int:
    """把模型给的数字参数夹到合法区间（模型经常给字符串或越界值）。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default

    return max(low, min(number, high))


def _as_str_list(value: Any) -> list[str]:
    """把字段投影参数规整成字符串列表。"""
    if not isinstance(value, (list, tuple)):
        return []

    return [str(v).strip() for v in value if str(v).strip()]


def _as_filters(value: Any) -> list[dict]:
    """
    规整过滤条件：只保留字段名合法的条目，算子非法时退回 EQ。

    @param value: 模型给的 filters 参数
    @return: 可直接交给 GraphQL 的 where 数组
    """
    if not isinstance(value, (list, tuple)):
        return []

    filters: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue

        field = str(item.get("field") or "").strip()
        if not field:
            continue

        op = str(item.get("op") or "EQ").strip().upper()
        if op not in ALLOWED_FILTER_OPS:
            op = "EQ"

        entry: dict[str, Any] = {"field": field, "op": op}
        if op != "EXISTS":
            entry["value"] = item.get("value")

        filters.append(entry)

    return filters


@dataclass
class DnaListModulesTool(FunctionTool):
    """列出资料库的模块与数据集。"""

    name: str = "dna_list_data_modules"
    description: str = (
        "列出《二重螺旋》资料库的模块与数据集（模块 id、中文名、数据集 id、记录数）。"
        "不确定某个数据属于哪个模块、或需要准确的数据集名时先调用它；"
        'keyword 可按模块 id / 中文名过滤，例如 keyword="角色"。'
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "可选，按模块 id 或中文名过滤，例如 角色 / 武器 / mod",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多列出多少个模块，默认 40，最大 100",
                },
            },
        }
    )
    client: Any = None
    """注入的数据源（DnaGateway / DnaClient / DnaPack，由插件在构造后赋值）。"""

    async def call(self, context: Any = None, **kwargs: Any) -> ToolExecResult:
        try:
            modules = await self.client.modules()
            datasets = await self.client.datasets()
        except DnaError as exc:
            return _error(exc)

        return _text(
            self.client,
            render.render_modules(
                modules,
                datasets,
                keyword=str(kwargs.get("keyword") or ""),
                limit=_as_int(kwargs.get("limit"), 40, 1, 100),
            ),
        )


@dataclass
class DnaSearchDataTool(FunctionTool):
    """在指定数据集里检索条目。"""

    name: str = "dna_search_data"
    description: str = (
        "在《二重螺旋》资料库的某个数据集里检索条目，返回命中的摘要条目（key + 字段）。"
        "dataset 可以是数据集 id（char / weapon / mod / monster / achievement / questchain ...）"
        "或模块中文名（角色 / 武器 / 魔之楔 / 怪物）。query 是跨字段全文匹配关键词（支持别名，如“蝴蝶”能搜到赛琪）；"
        "需要精确筛选时用 filters（取值先用 dna_list_field_values 查）；只返回关心的字段用 fields 降噪。"
        "拿到 key 后用 dna_get_entry 读完整字段。"
        "注意：剧情 / 语音 / 角色档案这类文本内容用 dna_search_story 更合适（scope 可选 profile / voice / dialog）。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "dataset": {
                    "type": "string",
                    "description": "数据集 id 或模块名，例如 char / 角色 / weapon / mod / questchain",
                },
                "query": {
                    "type": "string",
                    "description": "可选，全文匹配关键词（名字、别名、词条等）",
                },
                "filters": {
                    "type": "array",
                    "description": "可选，精确过滤条件，多个条件之间是「与」",
                    "items": {
                        "type": "object",
                        "properties": {
                            "field": {
                                "type": "string",
                                "description": "字段名，支持 a.b 路径，例如 charId",
                            },
                            "op": {
                                "type": "string",
                                "enum": list(ALLOWED_FILTER_OPS),
                                "description": "算子，默认 EQ",
                            },
                            "value": {
                                "description": "比较值：EQ/NE/CONTAINS 传单值，IN 传数组，EXISTS 不需要"
                            },
                        },
                        "required": ["field"],
                    },
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": '可选，只返回这些顶层字段，例如 ["id","名称","属性"]',
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条数，默认 5，最大 20",
                },
                "offset": {"type": "integer", "description": "分页偏移，默认 0"},
            },
            "required": ["dataset"],
        }
    )
    client: Any = None

    async def call(self, context: Any = None, **kwargs: Any) -> ToolExecResult:
        try:
            dataset, _ = await self.client.resolve_dataset(
                str(kwargs.get("dataset") or "")
            )
            page = await self.client.search(
                dataset,
                query=str(kwargs.get("query") or ""),
                filters=_as_filters(kwargs.get("filters")),
                fields=_as_str_list(kwargs.get("fields")),
                limit=_as_int(kwargs.get("limit"), 5, 1, 20),
                offset=_as_int(kwargs.get("offset"), 0, 0, 100000),
            )
        except DnaError as exc:
            return _error(exc)

        content = render.render_page(page, str_limit=240, list_limit=6)
        if dataset in RELATED_STORY_DATASETS:
            content += RELATED_STORY_HINT

        return _text(self.client, content)


@dataclass
class DnaGetEntryTool(FunctionTool):
    """按 key 读取一条完整记录。"""

    name: str = "dna_get_entry"
    description: str = (
        "按 key 读取《二重螺旋》资料库里一条记录的完整字段：角色属性 / 技能 / 溯源、武器面板、"
        "魔之楔词条、怪物属性、成就条件等。key 先用 dna_search_data 检索拿到。"
        "返回内容较长时会被截断，可用 fields 只取需要的顶层字段。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "dataset": {
                    "type": "string",
                    "description": "数据集 id 或模块名，例如 char / weapon / mod",
                },
                "key": {
                    "type": "string",
                    "description": "记录 key（dna_search_data 返回里的 key）",
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选，只返回这些顶层字段",
                },
            },
            "required": ["dataset", "key"],
        }
    )
    client: Any = None

    async def call(self, context: Any = None, **kwargs: Any) -> ToolExecResult:
        key = str(kwargs.get("key") or "").strip()
        if not key:
            return "请提供 key（可先用 dna_search_data 搜索）。"

        try:
            dataset, _ = await self.client.resolve_dataset(
                str(kwargs.get("dataset") or "")
            )
            record = await self.client.record(
                dataset, key, fields=_as_str_list(kwargs.get("fields")) or None
            )
        except DnaError as exc:
            return _error(exc)

        return _text(
            self.client,
            render.render_record(record, dataset, key, str_limit=1200, list_limit=20),
        )


@dataclass
class DnaListFieldValuesTool(FunctionTool):
    """查字段的可选值。"""

    name: str = "dna_list_field_values"
    description: str = (
        "查《二重螺旋》资料库里某个字段的可选值（去重 + 出现次数），用来构造 filters 或做枚举。"
        "例如 questchain 的 chapterName（章节）、char 的 势力 / 属性 / 标签。字段名不确定时先调用，"
        "本工具在字段不存在时会返回该数据集的可用字段列表。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "dataset": {"type": "string", "description": "数据集 id 或模块名"},
                "field": {
                    "type": "string",
                    "description": "字段名，例如 chapterName / 势力 / 属性",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回多少个取值，默认 40，最大 100",
                },
            },
            "required": ["dataset", "field"],
        }
    )
    client: Any = None

    async def call(self, context: Any = None, **kwargs: Any) -> ToolExecResult:
        field = str(kwargs.get("field") or "").strip()
        if not field:
            return "请提供 field（字段名）。"

        try:
            dataset, _ = await self.client.resolve_dataset(
                str(kwargs.get("dataset") or "")
            )
            values = await self.client.field_values(
                dataset, field, limit=_as_int(kwargs.get("limit"), 40, 1, 100)
            )
            if not values:
                names = await self.client.field_names(dataset)

                return f"数据集 {dataset} 的字段 {field} 没有取值统计。该数据集可用字段：{'、'.join(names)}"
        except DnaError as exc:
            return _error(exc)

        return _text(
            self.client,
            render.render_field_values(
                dataset, field, values, limit=_as_int(kwargs.get("limit"), 40, 1, 100)
            ),
        )


@dataclass
class DnaSearchStoryTool(FunctionTool):
    """跨剧情语料检索。"""

    name: str = "dna_search_story"
    description: str = (
        "跨剧情语料检索《二重螺旋》的故事内容，返回带出处的命中片段：剧情概要、任务链、角色语音、"
        "角色档案、书籍、光阴集；scope 含 dialog 时还会搜任务对话原文（体积大，按需用）。"
        "适合「谁说过什么」「哪段剧情提到 X」「某角色的故事」，也可用来找任务链 id。"
        "返回的是证据片段，请据此直接回答用户的问题、不要只复述命中了几条；"
        "需要某条剧情的完整概要或台词，再用 dna_read_story(chain_id=...)。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "关键词：角色名、别名、台词片段、章节名等",
                },
                "scope": {
                    "type": "string",
                    "description": "检索范围：all（默认）或逗号分隔的 summary/chain/voice/profile/book/topic/dialog，也支持中文如「剧情,语音」",
                },
                "limit": {
                    "type": "integer",
                    "description": "每类最多返回多少条，默认 4，最大 10",
                },
            },
            "required": ["query"],
        }
    )
    client: Any = None

    async def call(self, context: Any = None, **kwargs: Any) -> ToolExecResult:
        try:
            content = await story.search_story(
                self.client,
                str(kwargs.get("query") or ""),
                scope=str(kwargs.get("scope") or "all"),
                limit=_as_int(kwargs.get("limit"), 4, 1, 10),
            )
        except DnaError as exc:
            return _error(exc)

        return _text(self.client, content)


@dataclass
class DnaReadStoryTool(FunctionTool):
    """按任务链 id 读剧情详情。"""

    name: str = "dna_read_story"
    description: str = (
        "按任务链 id 读取《二重螺旋》剧情详情：任务链章节信息 + 中文剧情概要，可选附带对话原文节选（含说话人）。"
        "任务链 id 可用 dna_search_story 查到（例如 100101）。用户问「这段剧情讲了什么」时优先用它。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "chain_id": {"type": "string", "description": "任务链 id，例如 100101"},
                "include_dialogue": {
                    "type": "boolean",
                    "description": "是否附带对话原文节选，默认 false",
                },
                "max_dialogues": {
                    "type": "integer",
                    "description": "对话节选条数上限，默认 12，最大 40",
                },
            },
            "required": ["chain_id"],
        }
    )
    client: Any = None

    async def call(self, context: Any = None, **kwargs: Any) -> ToolExecResult:
        try:
            content = await story.read_story(
                self.client,
                str(kwargs.get("chain_id") or ""),
                include_dialogue=bool(kwargs.get("include_dialogue")),
                max_dialogues=_as_int(kwargs.get("max_dialogues"), 12, 1, 40),
            )
        except DnaError as exc:
            return _error(exc)

        return _text(self.client, content)


def build_tools(client: Any) -> list[Any]:
    """
    构造全部工具实例并注入客户端。

    @param client: 资料库数据源（网关或任一后端）
    @return: 工具实例列表（可直接交给 context.add_llm_tools）
    """
    tools = [
        DnaListModulesTool(),
        DnaSearchDataTool(),
        DnaGetEntryTool(),
        DnaListFieldValuesTool(),
        DnaSearchStoryTool(),
        DnaReadStoryTool(),
    ]
    for tool in tools:
        tool.client = client
        logger.debug("注册 DNA Builder 工具：%s", tool.name)

    return tools
