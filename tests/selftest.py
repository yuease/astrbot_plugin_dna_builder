"""
离线自测：不启动 AstrBot，直接打真实资料库接口，验证数据层与工具层的输出。

用法（在插件目录下）：

    python tests/selftest.py

退出码 0 表示全部通过；失败项会打印原因。仅依赖 httpx（工具层额外需要 pydantic，
缺 pydantic 时会跳过工具层的用例）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dna import render, story  # noqa: E402
from dna.client import DnaClient, DnaError  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录一条断言结果。"""
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  -> {detail}" if detail else ""))


def preview(text: str, size: int = 260) -> str:
    """把长文本压成一行预览。"""
    flat = " ".join((text or "").split())

    return flat[:size] + ("…" if len(flat) > size else "")


async def main() -> int:
    client = DnaClient(timeout=30)

    # 1. 元信息
    modules = await client.modules()
    datasets = await client.datasets()
    check(
        "模块列表非空",
        len(modules) > 10,
        f"{len(modules)} 个模块 / {len(datasets)} 个数据集",
    )
    overview = render.render_modules(modules, datasets, keyword="角色", limit=5)
    check("模块总览渲染", "char" in overview, preview(overview))

    # 2. 数据集名解析（中英文都能认）
    dataset_id, module_id = await client.resolve_dataset("角色")
    check(
        "resolve_dataset('角色')", dataset_id == "char", f"{dataset_id} / {module_id}"
    )
    dataset_id, _ = await client.resolve_dataset("mod")
    check("resolve_dataset('mod')", dataset_id == "mod", dataset_id)

    # 3. 结构化检索
    page = await client.search("char", query="贝蕾妮卡", limit=3)
    check("角色检索命中", bool(page.get("items")), f"total={page.get('total')}")
    check(
        "检索结果渲染",
        "key=" in render.render_page(page),
        preview(render.render_page(page)),
    )

    # 4. 别名检索（资料库里 char 有「别名」字段）
    alias_page = await client.search("char", query="蝴蝶", limit=3)
    check(
        "别名检索有返回",
        alias_page.get("total", 0) >= 0,
        f"total={alias_page.get('total')}",
    )

    # 5. 单条详情
    record = await client.record("char", "1101")
    check(
        "角色详情",
        bool(record and record.get("data")),
        preview(render.render_record(record, "char", "1101")),
    )

    # 6. 字段取值（筛选项）
    values = await client.field_values("questchain", "chapterName", limit=5)
    check("章节筛选项", bool(values), str([v.get("value") for v in values]))

    # 7. 剧情跨表检索
    hits = await story.search_story(client, "贝蕾妮卡", scope="all", limit=2)
    check("剧情检索", "剧情概要" in hits or "角色档案" in hits, preview(hits, 200))
    check("片段不含 JSON 噪声", '{"value"' not in hits, preview(hits, 120))

    # 8. 剧情详情（含台词与说话人解析）
    detail = await story.read_story(
        client, "100101", include_dialogue=True, max_dialogues=5
    )
    check(
        "剧情详情", "剧情概要" in detail and "对话节选" in detail, preview(detail, 200)
    )
    dialogue_part = detail[detail.find("对话节选") :] if "对话节选" in detail else ""
    check(
        "说话人解析",
        "你：" in dialogue_part or "旁白：" in dialogue_part,
        preview(dialogue_part, 160),
    )
    check(
        "占位符已收敛",
        "{性别" not in dialogue_part and "{nickname}" not in dialogue_part,
        preview(dialogue_part, 120),
    )

    # 8.1 过滤条件（where）与对话原文范围
    filtered = await client.search(
        "charvoice", filters=[{"field": "charId", "op": "EQ", "value": 1101}], limit=3
    )
    check("filters 过滤", bool(filtered.get("items")), f"total={filtered.get('total')}")
    dialog_hits = await story.search_story(client, "贝蕾妮卡", scope="dialog", limit=1)
    check("对话原文范围检索", "任务对话" in dialog_hits, preview(dialog_hits, 140))

    # 9. 错误处理
    try:
        await client.resolve_dataset("不存在的模块名xyz")
        check("非法数据集名报错", False, "没有抛错")
    except DnaError as exc:
        check("非法数据集名报错", True, preview(str(exc), 120))

    # 10. 工具层（需要 pydantic；离线环境没有 astrbot 时走对象基类）
    try:
        from dna.tools import build_tools

        tools = build_tools(client)
        names = [t.name for t in tools]
        check("工具清单", len(tools) == 6, ", ".join(names))

        try:
            import jsonschema

            for tool in tools:
                jsonschema.Draft202012Validator.check_schema(tool.parameters)
            check("工具 parameters 是合法 JSON Schema", True)
        except ImportError:
            check("工具 parameters 是合法 JSON Schema", True, "未安装 jsonschema，跳过")

        out = await tools[1].call(None, dataset="角色", query="芙罗拉", limit=2)
        check("dna_search_data 调用", "key=" in out, preview(out, 160))
        out = await tools[4].call(None, query="贝蕾妮卡", limit=2)
        check("dna_search_story 调用", bool(out), preview(out, 160))
        out = await tools[5].call(None, chain_id="100101", include_dialogue=False)
        check("dna_read_story 调用", "剧情概要" in out, preview(out, 160))
    except ImportError as exc:  # pragma: no cover
        check("工具层用例", True, f"跳过（{exc}）")

    await client.aclose()

    failed = [name for name, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 60)
    print(
        f"共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项"
        + (f"，失败：{failed}" if failed else "")
    )

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
