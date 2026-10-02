"""
查询结果渲染：把 GraphQL 返回的结构压缩成「模型读了就能继续下一步」的紧凑文本。

设计取向参考 DNA Builder 的资料检索 Agent：

1. 列表类结果只给摘要 + key，并显式提示下一步用什么工具取详情；
2. 所有长文本、长数组都做递归收缩，避免一次工具调用把上下文撑爆；
3. 输出是纯文本（不是给程序解析的 JSON），允许带中文字段说明。
"""

from __future__ import annotations

import json
from typing import Any


def shrink(
    value: Any,
    str_limit: int = 300,
    list_limit: int = 8,
    depth: int = 5,
    _level: int = 0,
) -> Any:
    """
    递归收缩数据结构：长字符串截断、长数组只留前若干项（附剩余数量说明）。

    @param value: 原始数据
    @param str_limit: 单个字符串保留的最大字符数
    @param list_limit: 数组最多保留的项数
    @param depth: 最大下钻层数，超过后只保留类型提示
    @return: 收缩后的数据（仍是合法 JSON 结构）
    """
    if isinstance(value, str):
        if str_limit > 0 and len(value) > str_limit:
            return f"{value[:str_limit]}…（略 {len(value) - str_limit} 字）"

        return value

    if _level >= depth:
        if isinstance(value, (dict, list)):
            return f"…（{type(value).__name__} 已折叠）"

        return value

    if isinstance(value, dict):
        return {
            k: shrink(v, str_limit, list_limit, depth, _level + 1)
            for k, v in value.items()
        }

    if isinstance(value, list):
        items = [
            shrink(v, str_limit, list_limit, depth, _level + 1)
            for v in value[:list_limit]
        ]
        if len(value) > list_limit:
            items.append(f"…（另有 {len(value) - list_limit} 项）")

        return items

    return value


def to_json(
    value: Any, str_limit: int = 300, list_limit: int = 8, depth: int = 5
) -> str:
    """收缩后序列化成紧凑 JSON 文本。"""
    return json.dumps(
        shrink(value, str_limit, list_limit, depth),
        ensure_ascii=False,
        separators=(",", ":"),
    )


IDENTITY_FIELDS = ("id", "key", "名称", "name")
"""无论怎么裁剪都要保留的身份字段。"""


def compact_record(
    data: Any,
    *,
    item_chars: int = 800,
    str_limit: int = 240,
    list_limit: int = 6,
) -> tuple[Any, list[str]]:
    """
    把一条记录压到指定体积：先递归收缩，再按字段体积从大到小整块丢弃。

    搜索结果只需要「认得出是哪一条」；技能树 / 突破 / 升级这类大字段会把上下文占满
    （实测角色记录 4.7KB，大半是技能与材料），完整内容留给 dna_get_entry。

    @param data: 记录主体
    @param item_chars: 单条记录的目标字符上限
    @param str_limit: 字符串字段保留长度
    @param list_limit: 数组字段保留项数
    @return: (裁剪后的记录, 被整块省略的字段名列表)
    """
    if not isinstance(data, dict):
        return shrink(data, str_limit, list_limit), []

    kept = {key: shrink(value, str_limit, list_limit) for key, value in data.items()}
    sizes = {
        key: len(json.dumps(value, ensure_ascii=False)) for key, value in kept.items()
    }
    total = sum(sizes.values())
    omitted: list[str] = []

    for key in sorted(sizes, key=lambda name: -sizes[name]):
        if total <= item_chars:
            break
        if key in IDENTITY_FIELDS:
            continue

        omitted.append(key)
        total -= sizes[key]
        del kept[key]

    return kept, omitted


def render_modules(
    modules: list[dict], datasets: list[dict], keyword: str = "", limit: int = 40
) -> str:
    """
    渲染模块总览：模块 id、中文名、基准数据集与记录数。

    @param modules: gameDataModules 结果
    @param datasets: gameDataSets() 全量结果（含语言变体）
    @param keyword: 可选关键词，按模块 id / 中文名过滤
    @param limit: 最多列出多少个模块
    @return: 供模型阅读的文本
    """
    # 每个模块挑一个基准数据集（优先 id == baseId，其次 locale 为 zh）
    base: dict[str, dict] = {}
    for s in datasets:
        module_id = s.get("baseId") or s.get("module") or s.get("id")
        current = base.get(module_id)
        better = current is None or (
            s.get("id") == s.get("baseId")
            and current.get("id") != current.get("baseId")
        )
        if better:
            base[module_id] = s

    needle = (keyword or "").strip().lower()
    rows: list[str] = []
    for m in modules:
        module_id = m.get("id") or ""
        if not module_id or "." in module_id:  # 语言变体不单独列
            continue

        label = m.get("label") or ""
        if needle and needle not in module_id.lower() and needle not in label.lower():
            continue

        dataset = base.get(module_id) or {}
        count = dataset.get("count")
        count_text = f"{count} 条" if isinstance(count, int) else "未知条数"
        rows.append(
            f"- {module_id}（{label}）：数据集 {dataset.get('id') or module_id}，{count_text}"
        )

        if len(rows) >= limit:
            break

    if not rows:
        return f"没有匹配「{keyword}」的模块。"

    total = len([m for m in modules if m.get("id") and "." not in m["id"]])
    head = (
        f"共 {total} 个模块"
        + (f"（按关键词「{keyword}」筛选）" if needle else "")
        + f"，列出 {len(rows)} 个："
    )

    return "\n".join(
        [
            head,
            *rows,
            "",
            "用法：dna_search_data(dataset=数据集或模块名, query=关键词) 检索；"
            "查剧情/语音/档案改用 dna_search_story；按 key 取完整字段用 dna_get_entry。",
        ]
    )


def render_page(
    page: dict,
    str_limit: int = 240,
    list_limit: int = 6,
    item_chars: int = 800,
) -> str:
    """
    渲染 gameData 分页结果（摘要视图）。

    每条记录都会被压到 `item_chars` 以内：过大的字段整块省略并列出字段名，
    提示模型按需用 dna_get_entry 取完整内容。这样一次检索不会因为几条大记录就吃掉整个上下文。

    @param page: gameData 返回的 GameDataPage
    @param str_limit: 单字段字符串上限
    @param list_limit: 数组字段保留项数上限
    @param item_chars: 单条记录目标字符上限
    @return: 供模型阅读的文本
    """
    dataset = page.get("dataset") or ""
    total = page.get("total")
    items = page.get("items") or []

    if not items:
        return f"数据集 {dataset} 没有命中记录（total={total}）。可以换关键词，或先用 dna_list_data_modules 确认数据集。"

    lines = [
        f"数据集 {dataset}：共 {total} 条命中，本页 {len(items)} 条（offset={page.get('offset', 0)}）"
    ]
    for index, item in enumerate(items, start=1):
        lines.append(f"{index}) key={item.get('key')}")
        compact, omitted = compact_record(
            item.get("data"),
            item_chars=item_chars,
            str_limit=str_limit,
            list_limit=list_limit,
        )
        lines.append(
            "   " + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        )
        if omitted:
            lines.append(f"   （省略字段：{'、'.join(omitted)}）")

    lines.append(
        f'提示：以上是摘要。需要完整字段（含被省略的）用 dna_get_entry(dataset="{dataset}", key=<上面的 key>)。'
    )

    return "\n".join(lines)


def render_record(
    record: dict | None,
    dataset: str,
    key: str,
    str_limit: int = 1200,
    list_limit: int = 20,
) -> str:
    """
    渲染单条记录的完整字段。

    @param record: gameDataRecord 结果
    @param dataset: 数据集 id
    @param key: 记录键
    @param str_limit: 单字段字符串上限（详情页给得比列表宽）
    @param list_limit: 数组字段保留项数上限
    @return: 供模型阅读的文本
    """
    if not record:
        return f"数据集 {dataset} 里没有 key={key} 的记录。可先用 dna_search_data 搜索确认 key。"

    return f"数据集 {dataset} 记录 {record.get('key') or key}：\n" + to_json(
        record.get("data"), str_limit, list_limit, depth=8
    )


def render_field_values(
    dataset: str, field: str, values: list[dict], limit: int = 40
) -> str:
    """
    渲染某字段的去重取值（用于做筛选、枚举）。

    @param dataset: 数据集 id
    @param field: 字段名
    @param values: gameDataFieldValues 结果
    @param limit: 最多列出多少项
    @return: 供模型阅读的文本
    """
    if not values:
        return f"数据集 {dataset} 的字段 {field} 没有取值统计（字段名可能不对，可用 dna_list_data_modules 或 gameDataFields 核对）。"

    lines = [
        f"数据集 {dataset} 字段 {field} 的取值（按出现次数降序，列出前 {min(len(values), limit)} 项）："
    ]
    for item in values[:limit]:
        lines.append(
            f"- {to_json(item.get('value'), str_limit=120)} × {item.get('count')}"
        )

    lines.append(
        f'用法：把取值放进 dna_search_data 的 filters，例如 filters=[{{"field": "{field}", "op": "EQ", "value": <取值>}}]。'
    )

    return "\n".join(lines)


def snippet(text: str, needle: str, width: int = 70) -> str:
    """
    从文本里截出命中关键词前后的一段上下文（用于剧情检索的「证据片段」）。

    @param text: 原始文本
    @param needle: 关键词
    @param width: 命中处前后各保留的字符数
    @return: 片段；未命中时返回文本开头
    """
    flat = " ".join(str(text).split())
    if not flat:
        return ""

    position = flat.find(needle)
    if position < 0:
        position = flat.lower().find(needle.lower())
    if position < 0:
        return flat[: width * 2] + ("…" if len(flat) > width * 2 else "")

    start = max(0, position - width)
    end = min(len(flat), position + len(needle) + width)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(flat) else ""

    return f"{prefix}{flat[start:end]}{suffix}"


def flatten_text(value: Any, limit: int = 8000) -> str:
    """
    把任意 JSON 结构里的字符串按顺序拼成一段纯文本（用于生成干净的证据片段）。

    直接对 JSON 文本截片段会带上 `{"value":"...` 这种噪声，模型读起来更费力。

    @param value: 任意 JSON 结构
    @param limit: 拼接后的字符上限
    @return: 拼接文本
    """
    parts: list[str] = []
    total = 0

    def walk(node: Any) -> None:
        nonlocal total
        if total >= limit:
            return

        if isinstance(node, str):
            parts.append(node)
            total += len(node)
        elif isinstance(node, dict):
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)

    return " ".join(parts)[:limit]


PLACEHOLDERS = (
    ("{nickname}", "你"),
    ("{性别：她|他}", "她/他"),
    ("{性别：他|她}", "他/她"),
)


def clean_game_text(text: str) -> str:
    """
    收敛资料库里的占位符，让台词读起来像人话。

    @param text: 原始文本
    @return: 替换占位符后的文本
    """
    result = str(text)
    for token, replacement in PLACEHOLDERS:
        result = result.replace(token, replacement)

    return result
