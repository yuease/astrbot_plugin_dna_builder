"""
本地数据集：把数据包里的各种导出形态归一成记录表，并复刻接口的检索语义。

官方数据包的模块导出有三种形态，接口把它们统一包装成 `{key, data}` 记录：

- 数组（char / mod / weapon / questchain ...）→ 每条记录一个元素，key 取 id / 名称 / name；
- 映射（storysummary 的 key→文本、questchain 的 key→版本）→ 每对键值一条记录，标量包成 `{"value": ...}`；
- 其它标量 → 单条记录。

筛选、全文匹配、排序、投影的语义都对齐 `server/src/db/mod/gameData.ts` 里那套定义，
这样同一句查询在本地数据包和接口上会得到一致的结果（tests/selftest.py 里有对比用例）。
"""

from __future__ import annotations

import json
from typing import Any


def json_text(value: Any) -> str:
    """对象 / 数组按紧凑 JSON 文本参与比较与检索。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def record_key(item: Any, index: int) -> str:
    """
    取记录键，规则与接口一致：id → 名称 → name → 序号。

    @param item: 记录主体
    @param index: 记录序号（兜底）
    @return: 字符串键
    """
    if isinstance(item, dict):
        for field in ("id", "名称", "name", "key"):
            if field in item and item[field] is not None:
                return str(item[field])

    return str(index)


def normalize(value: Any) -> list[dict]:
    """
    把模块导出值归一成 `[{key, data}]` 记录表。

    @param value: 模块导出的原始值
    @return: 记录表
    """
    if isinstance(value, list):
        return [
            {"key": record_key(item, index), "data": item}
            for index, item in enumerate(value)
        ]

    if isinstance(value, dict):
        records: list[dict] = []
        for key, item in value.items():
            data = item if isinstance(item, dict) else {"value": item}
            records.append({"key": str(key), "data": data})

        return records

    return [{"key": "0", "data": {"value": value}}]


def project(data: Any, fields: list[str] | None) -> Any:
    """顶层字段投影；不传或不是对象时原样返回。"""
    if not fields or not isinstance(data, dict):
        return data

    return {field: data[field] for field in fields if field in data}


def values_at(data: Any, path: str) -> list[Any]:
    """
    按 `a.b` 路径取值，遇到数组自动下钻（对齐接口的字段路径语义）。

    @param data: 记录主体
    @param path: 字段路径
    @return: 命中的原始值列表
    """
    current: list[Any] = [data]
    for part in str(path).split("."):
        found: list[Any] = []
        for node in current:
            if isinstance(node, dict):
                if part in node:
                    found.append(node[part])
            elif isinstance(node, list):
                for item in node:
                    if isinstance(item, dict) and part in item:
                        found.append(item[part])
        current = found
        if not current:
            return []

    return current


def flatten_scalars(values: list[Any]) -> list[Any]:
    """把嵌套数组摊平；对象保留原样（比较时按 JSON 文本处理）。"""
    flat: list[Any] = []
    for value in values:
        if isinstance(value, list):
            flat.extend(flatten_scalars(value))
        else:
            flat.append(value)

    return flat


def loose_equal(left: Any, right: Any) -> bool:
    """宽松相等：数字与数字字符串互认，其余按字符串比较。"""
    if left is right:
        return True

    if isinstance(left, bool) or isinstance(right, bool):
        return left == right

    try:
        if isinstance(left, (int, float)) or isinstance(right, (int, float)):
            return float(left) == float(right)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        pass

    if isinstance(left, (dict, list)) or isinstance(right, (dict, list)):
        return json_text(left) == json_text(right)

    return str(left) == str(right)


def match_filter(data: Any, condition: dict) -> bool:
    """
    单条过滤条件求值（EQ / NE / CONTAINS / IN / EXISTS）。

    @param data: 记录主体
    @param condition: {"field": ..., "op": ..., "value": ...}
    @return: 是否命中
    """
    field = str(condition.get("field") or "")
    op = str(condition.get("op") or "EQ").upper()
    target = condition.get("value")
    scalars = flatten_scalars(values_at(data, field))

    if op == "EXISTS":
        return bool(scalars)

    if op == "NE":
        return not any(loose_equal(item, target) for item in scalars)

    if op == "IN":
        wanted = target if isinstance(target, list) else [target]

        return any(any(loose_equal(item, want) for want in wanted) for item in scalars)

    if op == "CONTAINS":
        for item in scalars:
            if isinstance(item, str) and isinstance(target, str) and target in item:
                return True
            if (
                isinstance(item, (dict, list))
                and isinstance(target, str)
                and target in json_text(item)
            ):
                return True
            if loose_equal(item, target):
                return True

        return False

    return any(loose_equal(item, target) for item in scalars)


def text_contains(node: Any, needle: str) -> bool:
    """
    跨字段全文匹配：键名与标量值都参与，大小写不敏感（对齐接口语义）。

    @param node: 任意 JSON 节点
    @param needle: 已转小写的关键词
    @return: 是否命中
    """
    if isinstance(node, str):
        return needle in node.lower()

    if isinstance(node, bool):
        return needle in ("true" if node else "false")

    if isinstance(node, (int, float)):
        return needle in str(node)

    if isinstance(node, dict):
        for key, value in node.items():
            if needle in str(key).lower():
                return True
            if text_contains(value, needle):
                return True

        return False

    if isinstance(node, list):
        return any(text_contains(item, needle) for item in node)

    return False


def _sort_key(item: dict, field: str) -> tuple:
    """排序键：数值优先，缺失值恒排最后。"""
    scalars = flatten_scalars(values_at(item.get("data"), field))
    if not scalars:
        return (2, 0.0, "")

    value = scalars[0]
    if isinstance(value, bool):
        return (0, float(value), "")

    if isinstance(value, (int, float)):
        return (0, float(value), "")

    if isinstance(value, str):
        try:
            return (0, float(value), "")
        except ValueError:
            return (1, 0.0, value)

    return (1, 0.0, json_text(value))


class LocalDataset:
    """一份本地记录表，提供与接口一致的检索 / 取值 / 投影能力。"""

    def __init__(
        self,
        dataset_id: str,
        module_id: str,
        records: list[dict],
        export_name: str = "",
        kind: str = "array",
    ) -> None:
        """
        @param dataset_id: 数据集 id（对外暴露的名字）
        @param module_id: 所属模块 id
        @param records: 归一化后的记录表
        @param export_name: 模块导出名
        @param kind: 导出形态（array / object / map）
        """
        self.id = dataset_id
        self.module = module_id
        self.export_name = export_name
        self.kind = kind
        self.records = records
        self._by_key = {record["key"]: record for record in records}

    @property
    def count(self) -> int:
        """记录数。"""
        return len(self.records)

    def search(
        self,
        query: str | None = None,
        filters: list[dict] | None = None,
        fields: list[str] | None = None,
        limit: int = 5,
        offset: int = 0,
        sort: list[dict] | None = None,
    ) -> dict:
        """
        本地检索，返回结构与接口的 GameDataPage 对齐。

        @param query: 跨字段全文关键词
        @param filters: 过滤条件列表
        @param fields: 顶层字段投影
        @param limit: 单页条数
        @param offset: 分页偏移
        @param sort: 排序规则 [{"field": ..., "order": "ASC"|"DESC"}]
        @return: {"dataset", "total", "offset", "limit", "count", "items"}
        """
        matched = self.records

        if filters:
            matched = [
                record
                for record in matched
                if all(match_filter(record.get("data"), cond) for cond in filters)
            ]

        needle = (query or "").strip().lower()
        if needle:
            matched = [
                record
                for record in matched
                if text_contains(record.get("data"), needle)
            ]

        for rule in reversed(sort or []):
            field = str(rule.get("field") or "")
            if field:
                matched = sorted(
                    matched,
                    key=lambda record: _sort_key(record, field),
                    reverse=str(rule.get("order", "ASC")).upper() == "DESC",
                )

        total = len(matched)
        page = matched[offset : offset + limit]

        return {
            "dataset": self.id,
            "total": total,
            "offset": offset,
            "limit": limit,
            "count": len(page),
            "items": [
                {"key": record["key"], "data": project(record.get("data"), fields)}
                for record in page
            ],
        }

    def record(self, key: str, fields: list[str] | None = None) -> dict | None:
        """按 key 取单条记录（可选字段投影）。"""
        found = self._by_key.get(str(key))
        if not found:
            return None

        return {"key": found["key"], "data": project(found.get("data"), fields)}

    def search_by_id(
        self, key: str, fields: list[str] | None = None, limit: int = 1
    ) -> dict:
        """按 id 字段做一次检索（对应接口里带 fields 的 gameData 查询）。"""
        return self.search(
            filters=[{"field": "id", "op": "EQ", "value": key}],
            fields=fields,
            limit=limit,
        )

    def field_names(self, limit: int = 60) -> list[str]:
        """按首次出现顺序取顶层字段名。"""
        names: list[str] = []
        for record in self.records:
            data = record.get("data")
            if isinstance(data, dict):
                for key in data:
                    if key not in names:
                        names.append(key)
                        if len(names) >= limit:
                            return names

        return names

    def field_values(self, field: str, limit: int = 50) -> list[dict]:
        """统计某字段的去重取值与出现次数（按次数降序）。"""
        counter: dict[str, int] = {}
        display: dict[str, Any] = {}

        for record in self.records:
            for value in flatten_scalars(values_at(record.get("data"), field)):
                text = (
                    json_text(value) if isinstance(value, (dict, list)) else str(value)
                )
                counter[text] = counter.get(text, 0) + 1
                display.setdefault(text, value)

        ordered = sorted(counter.items(), key=lambda pair: (-pair[1], pair[0]))

        return [
            {"value": display[text], "count": count} for text, count in ordered[:limit]
        ]
