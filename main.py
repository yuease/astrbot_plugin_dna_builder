"""
AstrBot 插件：DOB 二重螺旋资料库（数据来自 DNA Builder，简称 DOB）。

两条使用路径：

1. 函数工具（主路径）：注册 dna_list_data_modules / dna_search_data / dna_get_entry /
   dna_list_field_values / dna_search_story / dna_read_story，模型在正常对话里按需调用，
   查到的都是资料库里的准确字段，不做记忆式编造；
2. 指令（调试与手查）：/dna 帮助、/dna 模块、/dna 剧情 <关键词>、/dna 详情 <id>、
   /dna 查 <数据集> <关键词>、/dna 条目 <数据集> <key>。

数据源是 DNA Builder（https://github.com/pa001024/dna-builder）公开的 GraphQL 接口，
本插件只读、带缓存，不写入任何数据。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .dna import render, story
from .dna.client import DEFAULT_ENDPOINT, DnaClient, DnaError, clip
from .dna.tools import build_tools

COMMAND_MAX_CHARS = 1500
"""指令回复的字符上限：QQ 单条消息放不下长结果，这里主动截断。"""

HELP_TEXT = (
    "DOB 二重螺旋资料库（数据来自 DNA Builder）：\n"
    "/dna 模块 [关键词] — 列出可查询的模块与数据集\n"
    "/dna 剧情 <关键词> — 跨剧情概要/任务链/语音/档案检索\n"
    "/dna 详情 <任务链id> [台词] — 剧情概要 + 章节信息（加「台词」附带对话原文）\n"
    "/dna 查 <数据集> <关键词> — 结构化检索，例如 /dna 查 char 贝蕾妮卡\n"
    "/dna 条目 <数据集> <key> — 读一条记录的完整字段\n"
    "直接说需求（例如「芙罗拉的技能是什么」）也可以，模型会自己调用工具查。"
)


@register(
    "astrbot_plugin_dna_builder",
    "yuease",
    "基于 DOB（DNA Builder）的《二重螺旋》资料库查询：角色/武器/魔之楔等准确数据 + 剧情检索。",
    "1.1.0",
    "https://github.com/pa001024/dna-builder",
)
class DnaBuilderPlugin(Star):
    """插件主体：构造客户端、注册函数工具、提供调试指令。"""

    def __init__(self, context: Context, config: Any = None):
        """
        @param context: AstrBot 插件上下文
        @param config: 插件配置（WebUI 的插件配置页；缺省时用默认值）
        """
        super().__init__(context)

        cfg = config or {}
        self.client = DnaClient(
            endpoint=str(cfg.get("api_endpoint") or DEFAULT_ENDPOINT),
            timeout=cfg.get("timeout") or 20,
            max_chars=cfg.get("max_chars") or 2600,
            proxy=str(cfg.get("proxy") or ""),
        )
        self.tools = build_tools(self.client)

        if cfg.get("enable_llm_tools", True):
            self._register_llm_tools()
        else:
            logger.info(
                "已按配置跳过函数工具注册（enable_llm_tools=false），/dna 指令仍可用。"
            )

    # ------------------------------------------------------------------ 注册

    def _register_llm_tools(self) -> None:
        """把工具挂到 AstrBot 的全局工具表；老版本走兼容路径。"""
        add_llm_tools = getattr(self.context, "add_llm_tools", None)

        if callable(add_llm_tools):
            try:
                add_llm_tools(*self.tools)
                logger.info(
                    "已注册 %d 个 DNA Builder 工具（AstrBot >= 4.5.1 接口）",
                    len(self.tools),
                )

                return
            except Exception:
                logger.exception("add_llm_tools 注册失败，尝试兼容旧接口")

        try:
            manager = self.context.provider_manager.llm_tools
            manager.func_list.extend(self.tools)
            logger.info("已注册 %d 个 DNA Builder 工具（旧接口）", len(self.tools))
        except Exception:
            logger.exception(
                "注册函数工具失败；/dna 指令仍可用，请在插件配置里检查 AstrBot 版本。"
            )

    # ------------------------------------------------------------------ 指令

    @filter.command("dna")
    async def dna(self, event: AstrMessageEvent):
        """查询《二重螺旋》资料库：/dna 帮助、模块、剧情、详情、查、条目。"""
        async for result in self._dispatch(event):
            yield result

    @filter.command("螺旋")
    async def dna_cn(self, event: AstrMessageEvent):
        """《二重螺旋》资料库查询（/dna 的中文别名）。"""
        async for result in self._dispatch(event):
            yield result

    async def _dispatch(self, event: AstrMessageEvent):
        """解析指令并回复；任何异常都转成可读文本，不让插件崩掉。"""
        try:
            text = await self._handle(self._extract_argument(event.message_str or ""))
        except DnaError as exc:
            text = f"查询失败：{exc}"
        except Exception as exc:  # noqa: BLE001 - 兜底，避免单个异常影响机器人
            logger.exception("dna 指令处理失败")
            text = f"处理出错：{exc}"

        yield event.plain_result(clip(text, COMMAND_MAX_CHARS))

    @staticmethod
    def _extract_argument(message: str) -> str:
        """去掉开头的指令词，只留参数部分。"""
        parts = (message or "").strip().split(maxsplit=1)
        if not parts:
            return ""

        head = parts[0].lstrip("/／").lower()
        if head in ("dna", "螺旋"):
            return parts[1].strip() if len(parts) > 1 else ""

        return " ".join(parts).strip()

    async def _handle(self, argument: str) -> str:
        """
        指令分发。

        @param argument: 指令后面的参数
        @return: 回复文本
        """
        if not argument or argument in ("帮助", "help", "-h", "--help"):
            return HELP_TEXT

        head, _, rest = argument.partition(" ")
        rest = rest.strip()

        if head in ("模块", "modules", "module"):
            modules = await self.client.modules()
            datasets = await self.client.datasets()

            return render.render_modules(modules, datasets, keyword=rest, limit=30)

        if head in ("剧情", "story"):
            if not rest:
                return "用法：/dna 剧情 <关键词>，例如 /dna 剧情 贝蕾妮卡"

            return await story.search_story(self.client, rest, scope="all", limit=3)

        if head in ("详情", "read"):
            parts = rest.split()
            if not parts:
                return "用法：/dna 详情 <任务链id> [台词]，例如 /dna 详情 100101 台词"

            return await story.read_story(
                self.client,
                parts[0],
                include_dialogue=len(parts) > 1
                and parts[1] in ("台词", "对话", "dialog"),
                max_dialogues=10,
            )

        if head in ("查", "search"):
            dataset, _, keyword = rest.partition(" ")
            if not dataset:
                return "用法：/dna 查 <数据集> <关键词>，例如 /dna 查 char 芙罗拉"

            dataset_id, _module = await self.client.resolve_dataset(dataset)
            page = await self.client.search(dataset_id, query=keyword.strip(), limit=5)

            return render.render_page(page)

        if head in ("条目", "entry", "get"):
            dataset, _, key = rest.partition(" ")
            if not dataset or not key.strip():
                return "用法：/dna 条目 <数据集> <key>，例如 /dna 条目 weapon 21001"

            dataset_id, _module = await self.client.resolve_dataset(dataset)
            record = await self.client.record(dataset_id, key.strip())

            return render.render_record(record, dataset_id, key.strip())

        # 默认把整段当成剧情关键词，最贴近日常问法
        return await story.search_story(self.client, argument, scope="all", limit=3)

    # ---------------------------------------------------------------- 生命周期

    async def terminate(self):
        """插件卸载 / 停用时释放 HTTP 连接。"""
        try:
            await self.client.aclose()
        except Exception:
            logger.exception("关闭资料库连接失败")
