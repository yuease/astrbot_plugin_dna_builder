"""
模拟 AstrBot 加载插件：用最小桩件替换 astrbot.* 后导入 main.py，
验证插件能构造、能注册工具、指令能跑通。

这里不是替代真机测试，而是把「没有 AstrBot 环境时也容易写错」的部分
（相对导入、工具基类、注册接口、指令分发）先钉死。

用法（在插件目录下）：

    python tests/import_check.py
"""

from __future__ import annotations

import asyncio
import re
import sys
import types
from pathlib import Path

import jsonschema
from pydantic.dataclasses import dataclass

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def plugin_package_name() -> str:
    """
    从 metadata.yaml 读插件标识作为包名。

    AstrBot 安装时目录名就是插件名，但直接 `git clone` 下来的目录名可能是仓库名，
    所以这里显式构造一个同名包指向插件目录，保证两种情况下都能导入 main.py。
    """
    try:
        text = (PLUGIN_DIR / "metadata.yaml").read_text(encoding="utf-8")
    except OSError:
        return PLUGIN_DIR.name

    match = re.search(r"(?m)^name:\s*(\S+)\s*$", text)

    return match.group(1) if match else PLUGIN_DIR.name


PACKAGE_NAME = plugin_package_name()
sys.path.insert(0, str(PLUGIN_DIR.parent))

HANDLERS: list[str] = []
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录一条断言结果。"""
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -> {detail}" if detail else ""))


def install_astrbot_stubs() -> None:
    """构造最小可用的 astrbot 桩件（接口形状与真实实现一致）。"""

    @dataclass
    class ToolSchema:
        """对应 astrbot.core.agent.tool.ToolSchema。"""

        name: str
        description: str
        parameters: dict

    @dataclass
    class FunctionTool(ToolSchema):
        """对应 astrbot.core.agent.tool.FunctionTool（只保留插件用到的部分）。"""

        handler: object = None

        async def call(self, context, **kwargs):
            raise NotImplementedError

    tool_mod = types.ModuleType("astrbot.core.agent.tool")
    tool_mod.ToolSchema = ToolSchema
    tool_mod.FunctionTool = FunctionTool
    tool_mod.ToolSet = list
    tool_mod.ToolExecResult = object

    def llm_tool_decorator(*args, **kwargs):
        """对应 astrbot.api.event.filter.llm_tool。"""

        def wrapper(func):
            return func

        return wrapper

    def command_decorator(name):
        """对应 astrbot.api.event.filter.command。"""

        def wrapper(func):
            HANDLERS.append(name)

            return func

        return wrapper

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.filter = types.SimpleNamespace(
        command=command_decorator, llm_tool=llm_tool_decorator
    )

    class AstrMessageEvent:
        """最小消息事件桩件。"""

        def __init__(self, message_str: str = "") -> None:
            self.message_str = message_str
            self.sent: list[str] = []

        def plain_result(self, text: str):
            self.sent.append(text)

            return text

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageEventResult = object

    class Star:
        """对应 astrbot.api.star.Star。"""

        def __init__(self, context) -> None:
            self.context = context

    class Context:
        """占位类型。"""

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Star = Star
    star_mod.Context = Context
    star_mod.register = lambda *args, **kwargs: lambda cls: cls

    class PluginContextLogger:
        """静默日志。"""

        def __getattr__(self, item):
            return lambda *args, **kwargs: None

    api_mod = types.ModuleType("astrbot.api")
    api_mod.logger = PluginContextLogger()
    api_mod.FunctionTool = FunctionTool

    astrbot_mod = types.ModuleType("astrbot")
    astrbot_mod.api = api_mod

    for name, module in {
        "astrbot": astrbot_mod,
        "astrbot.api": api_mod,
        "astrbot.api.event": event_mod,
        "astrbot.api.star": star_mod,
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.agent": types.ModuleType("astrbot.core.agent"),
        "astrbot.core.agent.tool": tool_mod,
    }.items():
        sys.modules[name] = module


class FakeContext:
    """记录 add_llm_tools 的调用。"""

    def __init__(self) -> None:
        self.registered: list = []

    def add_llm_tools(self, *tools) -> None:
        self.registered.extend(tools)


async def main() -> int:
    install_astrbot_stubs()

    import importlib

    # 目录名与插件名不一致时（例如 clone 下来的仓库目录），挂一个同名包指向插件目录
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [str(PLUGIN_DIR)]
    sys.modules.setdefault(PACKAGE_NAME, package)

    module = importlib.import_module(f"{PACKAGE_NAME}.main")

    context = FakeContext()
    plugin = module.DnaBuilderPlugin(context, {"timeout": 30})

    check("插件构造", plugin is not None)
    check("注册了 2 个指令", sorted(HANDLERS) == ["dna", "螺旋"], str(sorted(HANDLERS)))
    check(
        "注册了 6 个工具",
        len(context.registered) == 6,
        ", ".join(t.name for t in context.registered),
    )

    # 工具基类会用 jsonschema 校验 parameters（真实 AstrBot 同样会校验）
    ok = True
    for tool in context.registered:
        try:
            jsonschema.Draft202012Validator.check_schema(tool.parameters)
        except Exception as exc:  # noqa: BLE001
            ok = False
            check(f"工具 {tool.name} parameters 合法", False, str(exc))
    if ok:
        check("工具 parameters 通过 JSON Schema 校验", True)

    check("工具类型正确", all(hasattr(t, "call") for t in context.registered))

    # 指令路径
    help_text = await plugin._handle("")
    check("/dna 帮助", "DNA Builder" in help_text, help_text.splitlines()[0])

    overview = await plugin._handle("模块 角色")
    check("/dna 模块 角色", "char" in overview, overview.splitlines()[1][:60])

    story_text = await plugin._handle("剧情 贝蕾妮卡")
    check(
        "/dna 剧情 贝蕾妮卡", "剧情概要" in story_text, story_text.splitlines()[0][:60]
    )

    lookup = await plugin._handle("查 char 芙罗拉")
    check("/dna 查 char 芙罗拉", "key=1102" in lookup, lookup.splitlines()[2][:60])

    entry = await plugin._handle("条目 char 1101")
    check("/dna 条目 char 1101", "贝蕾妮卡" in entry)

    detail = await plugin._handle("详情 100101")
    check(
        "/dna 详情 100101",
        "剧情概要" in detail and "任务链" in detail,
        detail.splitlines()[0][:60],
    )

    # 事件路径（验证 plain_result 真的被调用）
    event = sys.modules["astrbot.api.event"].AstrMessageEvent("/dna 剧情 贝蕾妮卡")
    async for _ in plugin.dna(event):
        pass
    check(
        "事件分发",
        bool(event.sent) and "剧情概要" in event.sent[0],
        event.sent[0].splitlines()[0][:60],
    )

    await plugin.terminate()

    failed = [name for name, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 60)
    print(
        f"共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项"
        + (f"，失败：{failed}" if failed else "")
    )

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
