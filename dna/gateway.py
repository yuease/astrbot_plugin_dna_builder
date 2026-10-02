"""
数据源路由：本地数据包优先，在线查询兜底。

三种模式（插件配置项 `data_source`）：

- `auto`（默认）：数据包已就绪就用本地，否则先用在线查询回答，同时在后台把数据包下下来，
  下好之后自动切到本地。这样用户第一次提问不会卡在 22MB 下载上，之后也不再请求在线服务。
- `pack`：只认本地数据包，没有就现下（首次查询会慢一次）。
- `api`：只用实时在线查询（数据最新，但每次查询都要联网）。

对上层（工具 / 剧情检索）暴露的方法与 DnaClient 完全一致，切换数据源不影响工具代码。
"""

from __future__ import annotations

import asyncio
from typing import Any

from .client import DnaClient, DnaError
from .pack import DnaPack

MODE_AUTO = "auto"
MODE_PACK = "pack"
MODE_API = "api"
VALID_MODES = (MODE_AUTO, MODE_PACK, MODE_API)


class _SilentLogger:
    """
    离线自测（脱离 AstrBot 跑 tests/selftest.py）时的空日志器。

    插件在 AstrBot 里运行时由 main.py 注入 `astrbot.api.logger`；这里不创建任何
    logging 记录器，也不输出内容，只是为了在没有 AstrBot 的环境下也能实例化网关。
    """

    def __getattr__(self, _name: str):
        return lambda *args, **kwargs: None


class DnaGateway:
    """在数据包与在线查询之间路由，并保持与后端一致的方法签名。"""

    def __init__(
        self,
        api: DnaClient,
        pack: DnaPack | None = None,
        mode: str = MODE_AUTO,
        max_chars: int = 2600,
        log: Any = None,
    ) -> None:
        """
        @param api: 实时在线查询后端
        @param pack: 本地数据包后端（mode=api 时可为 None）
        @param mode: auto / pack / api
        @param max_chars: 工具返回字符上限
        @param log: AstrBot 插件日志器（main.py 传入 `astrbot.api.logger`）；缺省时静默
        """
        self.api = api
        self.pack = pack
        self.mode = mode if mode in VALID_MODES else MODE_AUTO
        self.max_chars = max_chars
        self._log = log if log is not None else _SilentLogger()
        self._warm_task: asyncio.Task | None = None

    # ------------------------------------------------------------ 数据源选择

    @property
    def active_source(self) -> str:
        """当前实际使用的数据源（给指令 / 状态展示用）。"""
        if self.mode == MODE_API or self.pack is None:
            return MODE_API

        if self.pack.is_ready():
            return MODE_PACK

        return self.mode

    def status(self) -> dict:
        """数据源状态摘要。"""
        info: dict[str, Any] = {
            "mode": self.mode,
            "active": self.active_source,
            "endpoint": self.api.endpoint,
        }
        if self.pack is not None:
            info["pack"] = self.pack.status()
            info["downloading"] = bool(self._warm_task and not self._warm_task.done())

        return info

    def start_warmup(self) -> None:
        """启动后台下载数据包（重复调用无副作用）。"""
        if self.pack is None or self.mode == MODE_API:
            return

        if self.pack.is_ready() or (self._warm_task and not self._warm_task.done()):
            return

        try:
            self._warm_task = asyncio.get_running_loop().create_task(self._warm_pack())
        except RuntimeError:  # 没有事件循环时不强求，首次查询会再试
            self._warm_task = None

    async def _warm_pack(self) -> None:
        """后台下载数据包，失败只记日志。"""
        assert self.pack is not None
        try:
            await self.pack.ensure()
            self._log.info("DNA Builder 数据包已就绪：%s", self.pack.status())
        except Exception as exc:  # noqa: BLE001 - 后台任务不应抛到事件循环
            self._log.warning("DNA Builder 数据包下载失败，将继续使用在线查询：%s", exc)

    def _should_use_pack(self) -> bool:
        """本次调用是否走本地数据包。"""
        if self.pack is None or self.mode == MODE_API:
            return False

        return self.pack.is_ready()

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """
        调用后端方法：本地优先、在线查询兜底；pack 模式下会先把数据包准备好。

        @param method: 后端方法名
        @param args: 位置参数
        @param kwargs: 关键字参数
        @return: 后端返回值
        @raises DnaError: 两个后端都失败时抛出在线查询侧的错误
        """
        pack_error: DnaError | None = None

        if (
            self.pack is not None
            and self.mode == MODE_PACK
            and not self.pack.is_ready()
        ):
            await self.pack.ensure()  # 失败直接抛出，提示用户数据包不可用

        if self._should_use_pack():
            try:
                return await getattr(self.pack, method)(*args, **kwargs)
            except DnaError as exc:
                pack_error = exc
                if self.mode == MODE_PACK:
                    raise
                self._log.warning("本地数据包查询失败，回退在线查询：%s", exc)
        else:
            self.start_warmup()

        try:
            return await getattr(self.api, method)(*args, **kwargs)
        except DnaError:
            if pack_error is not None:
                raise pack_error from None
            raise

    # ------------------------------------------------------- 与后端一致的接口

    async def modules(self, refresh: bool = False) -> list[dict]:
        """模块列表。"""
        return await self._call("modules", refresh)

    async def datasets(self, module: str | None = None) -> list[dict]:
        """数据集列表。"""
        return await self._call("datasets", module)

    async def resolve_dataset(self, name: str) -> tuple[str, str]:
        """解析数据集名。"""
        return await self._call("resolve_dataset", name)

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
        """检索数据集。"""
        return await self._call(
            "search", dataset, query, filters, fields, limit, offset, sort
        )

    async def record(
        self, dataset: str, key: str, fields: list[str] | None = None
    ) -> dict | None:
        """按 key 取记录。"""
        return await self._call("record", dataset, key, fields)

    async def field_names(self, dataset: str, limit: int = 60) -> list[str]:
        """字段名列表。"""
        return await self._call("field_names", dataset, limit)

    async def field_values(
        self, dataset: str, field: str, limit: int = 50
    ) -> list[dict]:
        """字段取值统计。"""
        return await self._call("field_values", dataset, field, limit)

    async def npc_names(self, ids: list[int]) -> dict[int, str]:
        """NPC id → 名字。"""
        return await self._call("npc_names", ids)

    async def aclose(self) -> None:
        """释放两个后端的连接。"""
        if self._warm_task and not self._warm_task.done():
            self._warm_task.cancel()
        await self.api.aclose()
        if self.pack is not None:
            await self.pack.aclose()
