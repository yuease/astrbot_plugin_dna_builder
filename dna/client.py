"""
DNA Builder 数据访问层。

数据来自 DNA Builder（简称 DOB，https://github.com/pa001024/dna-builder）对外提供的
公开 GraphQL 接口，本模块只做「查询 + 元信息缓存 + 数据集名解析」，不依赖 AstrBot，
方便脱离机器人单独测试（见 tests/selftest.py）。

接口能力（均取自 server/src/db/mod/gameData.ts 的 schema）：

- gameDataModules：可查询的模块列表（对应 src/data/d/*.data.ts），不加载数据，很轻；
- gameDataSets：模块下的数据集（含记录数、语言变体）；
- gameData(input)：主力检索，支持 where 过滤、search 全文、sort 排序、fields 投影、分页；
- gameDataRecord：按 key 取单条完整记录；
- gameDataFields / gameDataFieldValues：字段清单与某字段的去重取值（做筛选项用）。
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

DEFAULT_ENDPOINT = "https://api.dna-builder.cn/graphql"
"""DNA Builder 公开 GraphQL 接口；可在插件配置里用 api_endpoint 覆盖。"""

META_TTL = 1800.0
"""模块 / 数据集元信息的进程内缓存时长（秒）。"""

ALLOWED_FILTER_OPS = ("EQ", "NE", "CONTAINS", "IN", "EXISTS")
"""接口支持的过滤算子（GameDataFilterOp 枚举）。"""

Q_MODULES = """
query { gameDataModules { id label file baseId locale variants } }
"""

Q_SETS = """
query($module: String) {
  gameDataSets(module: $module) {
    id module exportName label baseId locale variants kind count
  }
}
"""

Q_SEARCH = """
query($input: GameDataQuery!) {
  gameData(input: $input) { dataset total offset limit count items { key data } }
}
"""

Q_RECORD = """
query($dataset: String!, $key: String!) {
  gameDataRecord(dataset: $dataset, key: $key) { key data }
}
"""

Q_FIELDS = """
query($dataset: String!, $limit: Int) {
  gameDataFields(dataset: $dataset, limit: $limit)
}
"""

Q_FIELD_VALUES = """
query($dataset: String!, $field: String!, $limit: Int) {
  gameDataFieldValues(dataset: $dataset, field: $field, limit: $limit) { value count }
}
"""


class DnaError(RuntimeError):
    """资料库查询失败（网络、协议或参数问题），调用方直接展示给用户即可。"""


def dumps(value: Any) -> str:
    """把对象序列化成紧凑 JSON（保留中文，不转义）。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def clip(text: str, limit: int) -> str:
    """按字符数截断文本，超出时在末尾标注。"""
    if limit <= 0 or len(text) <= limit:
        return text

    return f"{text[:limit]}…（已截断，原文 {len(text)} 字）"


class DnaClient:
    """DNA Builder GraphQL 客户端（异步、带元信息缓存）。"""

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout: float = 20.0,
        max_chars: int = 2600,
        proxy: str = "",
    ) -> None:
        """
        @param endpoint: GraphQL 接口地址
        @param timeout: 单次请求超时（秒）
        @param max_chars: 单次工具返回给模型的字符上限
        @param proxy: 可选 HTTP 代理地址
        """
        self.endpoint = (endpoint or DEFAULT_ENDPOINT).strip()
        self.timeout = max(3.0, float(timeout or 20.0))
        self.max_chars = max(500, int(max_chars or 2600))
        self.proxy = (proxy or "").strip()

        self._http: httpx.AsyncClient | None = None
        self._modules: list[dict] | None = None
        self._modules_at = 0.0
        self._sets: dict[str, list[dict]] = {}
        self._all_sets: list[dict] | None = None

    # ---------------------------------------------------------------- 基础请求

    async def _client(self) -> httpx.AsyncClient:
        """懒加载并复用 httpx 客户端（不要每个请求都新建连接）。"""
        if self._http is None or self._http.is_closed:
            kwargs: dict[str, Any] = {
                "timeout": httpx.Timeout(self.timeout),
                "headers": {
                    "Content-Type": "application/json",
                    "User-Agent": "astrbot-plugin-dna-builder/1.0",
                },
            }
            if self.proxy:
                kwargs["proxy"] = self.proxy
            self._http = httpx.AsyncClient(**kwargs)

        return self._http

    async def aclose(self) -> None:
        """插件卸载时释放连接。"""
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
        self._http = None

    async def gql(self, query: str, variables: dict[str, Any] | None = None) -> dict:
        """
        执行一次 GraphQL 查询。

        @param query: GraphQL 文档
        @param variables: 变量表
        @return: data 字段（可能为空字典）
        @raises DnaError: 网络失败、非 200、或响应里带 errors
        """
        client = await self._client()

        try:
            resp = await client.post(
                self.endpoint, json={"query": query, "variables": variables or {}}
            )
        except httpx.HTTPError as exc:
            raise DnaError(f"连接资料库失败：{exc}") from exc

        if resp.status_code != 200:
            raise DnaError(f"资料库返回 HTTP {resp.status_code}")

        try:
            payload = resp.json()
        except ValueError as exc:
            raise DnaError("资料库返回了非 JSON 响应") from exc

        errors = payload.get("errors")
        if errors:
            message = (
                errors[0].get("message")
                if isinstance(errors[0], dict)
                else str(errors[0])
            )
            raise DnaError(f"资料库查询出错：{message}")

        return payload.get("data") or {}

    # ------------------------------------------------------------ 元信息与解析

    async def modules(self, refresh: bool = False) -> list[dict]:
        """取全部数据模块（带缓存）。"""
        now = time.monotonic()

        if (
            not refresh
            and self._modules is not None
            and now - self._modules_at < META_TTL
        ):
            return self._modules

        data = await self.gql(Q_MODULES)
        modules = data.get("gameDataModules") or []
        self._modules = modules
        self._modules_at = now
        self._all_sets = None
        self._sets.clear()

        return modules

    async def datasets(self, module: str | None = None) -> list[dict]:
        """取数据集列表；module 为空时返回全部模块的数据集（含语言变体，较大）。"""
        if module:
            cached = self._sets.get(module)
            if cached is not None:
                return cached

            sets = (await self.gql(Q_SETS, {"module": module})).get(
                "gameDataSets"
            ) or []
            self._sets[module] = sets

            return sets

        if self._all_sets is None:
            self._all_sets = (await self.gql(Q_SETS, {"module": None})).get(
                "gameDataSets"
            ) or []

        return self._all_sets

    async def resolve_dataset(self, name: str) -> tuple[str, str]:
        """
        把用户/模型给的名字解析成 (数据集 id, 模块 id)。

        支持三种写法：数据集 id（questchain）、模块 id（char / mod / weapon）、中文模块名（角色 / 魔之楔）。
        语言变体（quest.en 之类）不会自动选中，永远优先中文基准数据集。

        @param name: 数据集 id 或模块 id / 名称
        @return: (dataset_id, module_id)
        @raises DnaError: 无法唯一定位时，错误信息里给出候选
        """
        raw = (name or "").strip()
        if not raw:
            raise DnaError(
                "必须提供数据集名（dataset），例如 char / weapon / mod / questchain"
            )

        modules = await self.modules()
        by_id = {m["id"]: m for m in modules}
        lowered = raw.lower()

        # 1) 模块 id / 模块中文名精确命中 → 用该模块的基准数据集
        module_id = None
        if raw in by_id:
            module_id = raw
        else:
            for m in modules:
                if (m.get("label") or "").strip() == raw:
                    module_id = m["id"]
                    break

        if module_id:
            sets = await self.datasets(module_id)
            if not sets:
                raise DnaError(f"模块 {module_id} 下没有可查询的数据集")

            base = next((s for s in sets if s.get("id") == module_id), sets[0])

            return base["id"], module_id

        # 2) 数据集 id 精确命中（含语言变体）
        for s in await self.datasets():
            if s.get("id") == raw:
                return s["id"], s.get("module") or s["id"]

        # 3) 模糊匹配：id / label / baseId 里包含关键词
        candidates: list[dict] = []
        for s in await self.datasets():
            haystack = " ".join(
                str(s.get(field) or "") for field in ("id", "label", "baseId", "module")
            ).lower()
            if lowered in haystack:
                candidates.append(s)

        # 语言变体优先级最低，避免解析出 quest.en 这种
        candidates.sort(
            key=lambda s: (s.get("id") != s.get("baseId"), s.get("locale") != "zh")
        )

        if candidates:
            best = candidates[0]

            return best["id"], best.get("module") or best["id"]

        hint = "、".join(f"{m['id']}（{m.get('label')}）" for m in modules[:12])
        raise DnaError(
            f"找不到数据集「{raw}」。可用模块例如：{hint}。可先用 dna_list_data_modules 查看全部。"
        )

    # ------------------------------------------------------------------ 查询类

    async def search(
        self,
        dataset: str,
        query: str | None = None,
        filters: list[dict] | None = None,
        fields: list[str] | None = None,
        limit: int = 5,
        offset: int = 0,
        sort: list[dict] | None = None,
    ) -> dict:
        """
        统一检索（对应 gameData 查询）。

        @param dataset: 数据集 id
        @param query: 跨字段全文匹配关键词
        @param filters: 精确过滤条件，形如 [{"field": "charId", "op": "EQ", "value": 1101}]
        @param fields: 只返回这些顶层字段（降噪、控制体积）
        @param limit: 单页条数（1-50）
        @param offset: 分页偏移
        @param sort: 排序规则，形如 [{"field": "id", "order": "ASC"}]
        @return: GameDataPage（dataset / total / items ...）
        """
        payload: dict[str, Any] = {
            "dataset": dataset,
            "limit": max(1, min(int(limit or 5), 50)),
            "offset": max(0, int(offset or 0)),
        }

        if query and query.strip():
            payload["search"] = query.strip()
        if filters:
            payload["where"] = filters
        if fields:
            payload["fields"] = fields
        if sort:
            payload["sort"] = sort

        data = await self.gql(Q_SEARCH, {"input": payload})

        return data.get("gameData") or {}

    async def record(
        self, dataset: str, key: str, fields: list[str] | None = None
    ) -> dict | None:
        """
        按 key 取单条记录；不存在时返回 None。传 fields 时改用 gameData 做字段投影。

        @param dataset: 数据集 id
        @param key: 记录键（id / 名称 / name / Map 键）
        @param fields: 可选的字段投影
        @return: {"key": ..., "data": ...} 或 None
        """
        if fields:
            page = await self.search(
                dataset,
                filters=[{"field": "id", "op": "EQ", "value": key}],
                fields=fields,
                limit=1,
            )
            items = page.get("items") or []

            return items[0] if items else None

        data = await self.gql(Q_RECORD, {"dataset": dataset, "key": str(key)})

        return data.get("gameDataRecord")

    async def field_names(self, dataset: str, limit: int = 60) -> list[str]:
        """取数据集的顶层字段名。"""
        data = await self.gql(Q_FIELDS, {"dataset": dataset, "limit": limit})

        return data.get("gameDataFields") or []

    async def field_values(
        self, dataset: str, field: str, limit: int = 50
    ) -> list[dict]:
        """取某字段的去重取值与出现次数（做筛选项用）。"""
        data = await self.gql(
            Q_FIELD_VALUES, {"dataset": dataset, "field": field, "limit": limit}
        )

        return data.get("gameDataFieldValues") or []

    async def npc_names(self, ids: list[int]) -> dict[int, str]:
        """
        批量把 NPC id 解析成名字（剧情台词里用）。失败时返回空表，调用方回退成显示 id。

        @param ids: NPC id 列表
        @return: {npc_id: 名字}
        """
        unique = [int(i) for i in dict.fromkeys(ids) if i is not None]
        if not unique:
            return {}

        try:
            page = await self.search(
                "npc",
                filters=[{"field": "id", "op": "IN", "value": unique[:200]}],
                fields=["id", "name"],
                limit=min(len(unique), 50),
            )
        except DnaError:
            return {}

        names: dict[int, str] = {}
        for item in page.get("items") or []:
            data = item.get("data") or {}
            if isinstance(data.get("id"), int) and data.get("name"):
                names[data["id"]] = str(data["name"])

        return names
