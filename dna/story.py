"""
剧情检索：把散落在多个数据集里的「故事文本」串成一条可用的检索路径。

《二重螺旋》的剧情在本资料库里被拆成好几张表，单表检索都会漏：

- storysummary：每条任务链一段中文梗概（回答「这段剧情讲了什么」最省事）；
- questchain：任务链的章节 / 名称 / 版本（用于把 key 还原成人能看懂的位置）；
- quest：任务与对话节点原文（台词在这里，体积最大）；
- charvoice：角色语音台词；charext：角色档案；book：书籍；partytopic：光阴集。

因此这里提供两个工具语义：

- search_story：跨表召回，返回「出处 + 命中片段」，解决「哪里提到过 X」；
- read_story：按任务链 key 组装「梗概 + 章节信息 +（可选）对话节选」，解决「这条剧情讲了什么」。
"""

from __future__ import annotations

from typing import Any

from . import render
from .client import DnaClient, DnaError

STORY_SCOPES: dict[str, dict[str, Any]] = {
    "summary": {
        "title": "剧情概要",
        "dataset": "storysummary",
        "fields": ["value"],
    },
    "chain": {
        "title": "任务链",
        "dataset": "questchain",
        "fields": ["id", "name", "chapterName", "chapterNumber", "episode", "版本"],
    },
    "voice": {
        "title": "角色语音",
        "dataset": "charvoice",
        "fields": ["id", "charId", "name", "text"],
    },
    "profile": {
        "title": "角色档案",
        "dataset": "charext",
        "fields": ["id", "charId", "name", "unlock", "text"],
    },
    "book": {
        "title": "书籍",
        "dataset": "book",
        "fields": ["id", "name", "desc", "res"],
    },
    "topic": {
        "title": "光阴集",
        "dataset": "partytopic",
        "fields": ["id", "charId", "name", "desc", "memoryName", "memoryDesc"],
    },
    "dialog": {
        "title": "任务对话",
        "dataset": "quest",
        "fields": ["id", "quests"],
    },
}

ALL_SCOPES = ("summary", "chain", "voice", "profile", "book", "topic")
"""默认检索范围：不含 dialog（单条记录 5~8KB，明显偏大，需要时才显式指定）。"""

SCOPE_ALIASES: dict[str, str] = {
    "剧情": "summary",
    "梗概": "summary",
    "概要": "summary",
    "任务链": "chain",
    "章节": "chain",
    "语音": "voice",
    "台词": "voice",
    "档案": "profile",
    "角色档案": "profile",
    "书": "book",
    "书籍": "book",
    "光阴集": "topic",
    "对话": "dialog",
    "原文": "dialog",
}


def normalize_scope(scope: str) -> list[str]:
    """
    把调用方给的 scope 解析成数据集键列表。

    @param scope: all / 逗号分隔的键（可混用中文别名）
    @return: 命中的 scope 键列表
    """
    raw = (scope or "all").strip()
    if not raw or raw.lower() in ("all", "*", "全部"):
        return list(ALL_SCOPES)

    keys: list[str] = []
    for piece in raw.replace("，", ",").split(","):
        name = piece.strip()
        if not name:
            continue

        key = name if name in STORY_SCOPES else SCOPE_ALIASES.get(name)
        if key and key not in keys:
            keys.append(key)

    return keys or list(ALL_SCOPES)


async def _chain_meta(client: DnaClient, keys: list[str]) -> dict[str, dict]:
    """
    批量取任务链元信息（用于给剧情概要补上「这是哪一章」）。

    @param client: 资料库客户端
    @param keys: 任务链 key 列表
    @return: {key: 任务链记录}
    """
    result: dict[str, dict] = {}
    for key in keys[:5]:
        try:
            record = await client.record(
                "questchain",
                key,
                fields=["id", "name", "chapterName", "chapterNumber", "episode"],
            )
        except DnaError:
            continue

        if record:
            result[str(key)] = record.get("data") or record

    return result


async def _role_names(client: DnaClient, ids: list[int]) -> dict[int, str]:
    """批量把角色 id 解析成名字（语音 / 档案里只有 charId）。"""
    unique = [int(i) for i in dict.fromkeys(ids) if isinstance(i, int)]
    if not unique:
        return {}

    try:
        page = await client.search(
            "char",
            filters=[{"field": "id", "op": "IN", "value": unique[:100]}],
            fields=["id", "名称"],
            limit=min(len(unique), 50),
        )
    except DnaError:
        return {}

    names: dict[int, str] = {}
    for item in page.get("items") or []:
        data = item.get("data") or {}
        if isinstance(data.get("id"), int) and data.get("名称"):
            names[data["id"]] = str(data["名称"])

    return names


def _describe(
    scope_key: str, item: dict, chain_meta: dict[str, dict], role_names: dict[int, str]
) -> str:
    """
    生成一条命中的「出处」描述。

    @param scope_key: 数据集键
    @param item: 命中记录
    @param chain_meta: 任务链元信息表
    @param role_names: 角色名表
    @return: 一行出处文本
    """
    data = item.get("data") or {}
    key = str(item.get("key"))

    if scope_key == "summary":
        meta = chain_meta.get(key)
        if meta:
            where = " · ".join(
                str(meta.get(f) or "") for f in ("chapterName", "chapterNumber", "name")
            ).strip(" ·")
            return f"{key} {where}" if where else key

        return f"{key}（剧情概要）"

    if scope_key == "chain":
        parts = [
            str(data.get(f) or "")
            for f in ("name", "chapterName", "chapterNumber", "episode")
        ]
        where = " · ".join(p for p in parts if p)

        return f"{key} {where}".strip()

    if scope_key in ("voice", "profile", "topic"):
        role = role_names.get(data.get("charId"))
        role_text = (
            f"{role}"
            if role
            else (f"角色{data.get('charId')}" if data.get("charId") else "")
        )
        name = str(data.get("name") or data.get("memoryName") or "")

        return " · ".join(p for p in (f"{key}", role_text, name) if p)

    if scope_key == "book":
        return f"{key} {data.get('name') or ''}".strip()

    return f"{key}（{STORY_SCOPES[scope_key]['title']}）"


async def search_story(
    client: DnaClient,
    query: str,
    scope: str = "all",
    limit: int = 4,
    width: int = 70,
) -> str:
    """
    跨剧情语料检索，返回带出处的命中片段。

    @param client: 资料库客户端
    @param query: 关键词（角色名、台词片段、章节名等）
    @param scope: all 或逗号分隔的范围（summary/chain/voice/profile/book/topic/dialog，支持中文别名）
    @param limit: 每类最多返回多少条
    @param width: 片段前后保留的字符数
    @return: 供模型阅读的文本
    """
    text = (query or "").strip()
    if not text:
        return "请提供要检索的剧情关键词。"

    keys = normalize_scope(scope)
    per_scope = max(1, min(int(limit or 4), 10))
    blocks: list[str] = []

    for key in keys:
        config = STORY_SCOPES[key]
        try:
            page = await client.search(
                config["dataset"], query=text, fields=config["fields"], limit=per_scope
            )
        except DnaError as exc:
            blocks.append(f"【{config['title']}】查询失败：{exc}")
            continue

        items = page.get("items") or []
        if not items:
            continue

        chain_meta: dict[str, dict] = {}
        if key == "summary":
            chain_meta = await _chain_meta(client, [str(i.get("key")) for i in items])

        role_ids: list[int] = [
            i.get("data", {}).get("charId")
            for i in items
            if isinstance(i.get("data"), dict)
        ]
        role_names = await _role_names(client, role_ids)

        lines = [
            f"【{config['title']}】命中 {page.get('total')} 条，显示 {len(items)} 条"
        ]
        for item in items:
            where = _describe(key, item, chain_meta, role_names)
            piece = render.snippet(
                render.flatten_text(item.get("data") or {}), text, width
            )
            lines.append(f"- {where}")
            if piece:
                lines.append(f"  {piece}")

        blocks.append("\n".join(lines))

    if not blocks:
        return (
            f"没找到含「{text}」的剧情内容。可以换关键词（例如角色名、别名、章节名），"
            "或用 dna_search_data 到具体数据集里查。"
        )

    return "\n\n".join(blocks)


async def read_story(
    client: DnaClient,
    key: str,
    include_dialogue: bool = False,
    max_dialogues: int = 12,
) -> str:
    """
    按任务链 key 组装剧情详情。

    @param client: 资料库客户端
    @param key: 任务链 / 剧情概要的 key（两者一致，例如 100101）
    @param include_dialogue: 是否附带对话原文节选
    @param max_dialogues: 对话节选条数上限
    @return: 供模型阅读的文本
    """
    chain_key = str(key or "").strip()
    if not chain_key:
        return "请提供任务链 id（可用 dna_search_story 先查到，例如 100101）。"

    lines: list[str] = []

    try:
        chain = await client.record("questchain", chain_key)
    except DnaError as exc:
        return f"查询任务链失败：{exc}"

    if chain:
        data = chain.get("data") or {}
        title = " · ".join(
            str(data.get(f) or "")
            for f in ("name", "chapterName", "chapterNumber", "episode")
        ).strip(" ·")
        lines.append(f"【任务链 {chain_key}】{title}")
        if data.get("版本"):
            lines.append(f"版本：{data['版本']}")
    else:
        lines.append(f"【任务链 {chain_key}】未找到任务链记录，下面只给剧情概要。")

    try:
        summary = await client.record("storysummary", chain_key)
    except DnaError:
        summary = None

    if summary:
        value = (summary.get("data") or {}).get("value")
        if value:
            lines.append(f"\n【剧情概要】\n{value}")

    if include_dialogue:
        try:
            quest = await client.record("quest", chain_key)
        except DnaError as exc:
            quest = None
            lines.append(f"\n【对话节选】查询失败：{exc}")

        if quest:
            dialogues = _collect_dialogues(
                quest.get("data") or {}, max(1, min(int(max_dialogues or 12), 40))
            )
            names = await client.npc_names(
                [d["npc"] for d in dialogues if isinstance(d.get("npc"), int)]
            )
            lines.append(f"\n【对话节选】共取 {len(dialogues)} 条")
            lines.extend(
                f"- {_speaker(d.get('npc'), names)}：{render.clean_game_text(d['text'])}"
                for d in dialogues
            )

    lines.append(
        "\n提示：需要更多台词可调大 max_dialogues；跨表找同一关键词用 dna_search_story(query=...)。"
    )

    return "\n".join(lines)


def _speaker(npc: Any, names: dict[int, str]) -> str:
    """
    把台词里的 npc id 渲染成说话人名字。

    `{nickname}` 是资料库里主角的占位符（玩家自定义名字），这里统一显示成「你」。

    @param npc: 台词里的 npc 字段
    @param names: npc id → 名字
    @return: 说话人文本
    """
    if not isinstance(npc, int):
        return "旁白"

    name = names.get(npc)
    if name == "{nickname}":
        return "你"

    return render.clean_game_text(name) if name else f"NPC{npc}"


def _collect_dialogues(quest_data: dict, max_dialogues: int) -> list[dict]:
    """
    展平 quest 记录里的对话节点。

    @param quest_data: quest 数据集的一条记录
    @param max_dialogues: 最多收集多少条台词
    @return: [{"npc": npc_id, "text": 台词, "quest": 任务名}, ...]
    """
    collected: list[dict] = []
    for quest in quest_data.get("quests") or []:
        quest_name = quest.get("name")
        for node in quest.get("nodes") or []:
            for dialogue in node.get("dialogues") or []:
                content = dialogue.get("content")
                if not content:
                    continue

                collected.append(
                    {"npc": dialogue.get("npc"), "text": content, "quest": quest_name}
                )

                if len(collected) >= max_dialogues:
                    return collected

    return collected
