# DOB 二重螺旋资料库（astrbot_plugin_dna_builder）

基于 **DOB（[DNA Builder](https://github.com/pa001024/dna-builder)）** 的《二重螺旋》资料库插件：
让 AstrBot（QQ / 其他平台）里的机器人能查到**准确数据**，而不是靠模型记忆编。
包括结构化数据（角色 / 武器 / 魔之楔 / 怪物 / 成就 / 密函 …）和**剧情内容**（剧情概要、任务链、
角色语音、角色档案、书籍、光阴集、任务对话原文）。

数据来自 DOB 官方发布的**数据包**：下载一次（约 22 MB）缓存在本地，之后所有查询都在本地完成，
既快，也不会给上游服务器添负担。插件**只读**，不写入任何数据。

## 安装

1. 把整个 `astrbot_plugin_dna_builder` 目录放进 AstrBot 的 `data/plugins/` 下；
   或者把打包好的 zip 直接丢进 WebUI 插件页的「安装插件」。
2. 重启 / 重载插件。插件目录里有 `requirements.txt`（依赖 `httpx` 与 `msgpack`，都很小），
   AstrBot 会自动安装。
3. 到 WebUI 的 `插件 → 管理行为 → 函数工具` 确认这些工具是启用状态：

   `dna_list_data_modules`、`dna_search_data`、`dna_get_entry`、`dna_list_field_values`、
   `dna_search_story`、`dna_read_story`

4. 要用自然语言自动查资料，当前会话用的模型必须支持 function calling（DeepSeek V3.x、
   Qwen3、GLM-4.x、GPT-5.x、Claude 4.x、Gemini 3.x 均可）。

第一次查询时插件会自动下载约 22 MB 的官方数据包（见下一节），之后就是纯本地查询。

要求 AstrBot `>= 4.5.7`（工具用的是 `FunctionTool.call()` 新接口）。

## 指令（手查 / 调试）

| 指令 | 说明 |
| --- | --- |
| `/dna` 或 `/dna 帮助` | 用法说明 |
| `/dna 模块 [关键词]` | 列出可查询的模块与数据集（79 个模块） |
| `/dna 剧情 <关键词>` | 跨剧情概要 / 任务链 / 语音 / 档案检索，返回带出处的片段 |
| `/dna 详情 <任务链id> [台词]` | 剧情概要 + 章节信息；带「台词」时附对话原文（含说话人） |
| `/dna 查 <数据集> <关键词>` | 结构化检索，例如 `/dna 查 char 贝蕾妮卡` |
| `/dna 条目 <数据集> <key>` | 读一条记录的完整字段 |
| `/dna 数据包` | 查看数据源模式与本地数据包状态（未就绪时会触发后台下载） |
| `/螺旋 <关键词>` | `/dna` 的中文别名 |

不写指令、直接问也可以（例如「芙罗拉的技能和CV是什么」「贝蕾妮卡的剧情线讲了什么」），
模型会自己选择工具调用。

## 数据包（数据从哪来）

插件默认**不走网络查询**，而是用 DOB 官方发布的**数据包**在本地查询。

- **来源**：`https://cdn.dobapp.cc/data-pack/`——就是 DOB 桌面版 / 移动端自己在用的那份，
  托管在 Cloudflare CDN 上；
- **内容**：zip 里是 msgpack 编好的各模块数据（角色 / 武器 / 魔之楔 / 怪物 / 成就 / 任务链 /
  剧情 / 语音 / 档案 …，共 79 个模块、165 个数据集），当前版本约 **22 MB**；
- **缓存**：下载一次存在 AstrBot 的插件数据目录
  （`data/plugin_data/astrbot_plugin_dna_builder/data-pack/`），重装 / 升级插件都不会丢；
- **更新**：每 12 小时请求一次几 KB 的 `versions.json`，只有出现新版本才会重新下载。
  官方发版很勤（实现那天正好有一版新包）；
- **查询**：全部在本地解码检索，实测单次几毫秒到几十毫秒，剧情跨表检索也不用等网络。

用 `/dna 数据包` 可以随时看当前版本、构建时间与缓存位置。

插件也保留了**实时在线**模式作为可选项（走 `api.dna-builder.cn` 的公开 GraphQL 服务，
每次查询联网、数据永远最新），用配置项 `data_source` 切换：

| 模式 | 行为 | 适合 |
| --- | --- | --- |
| `auto`（默认） | 数据包就绪就用本地；第一次还没下好时先用在线查询回答，同时后台下载，下好自动切本地 | 绝大多数情况 |
| `pack` | 只用本地数据包，没有就现下 | 推荐 |
| `api` | 只用实时在线查询 | 非必要不建议 |



## 工具设计


列表类工具只回摘要 + `key`，详情用另一个工具按 `key` 取，避免一次把大 JSON 灌进上下文。

| 工具 | 作用 | 关键参数 |
| --- | --- | --- |
| `dna_list_data_modules` | 模块 / 数据集总览（带缓存） | `keyword`、`limit` |
| `dna_search_data` | 指定数据集全文检索 + 精确过滤 + 字段投影 | `dataset`、`query`、`filters`、`fields`、`limit`、`offset` |
| `dna_get_entry` | 按 key 读完整记录 | `dataset`、`key`、`fields` |
| `dna_list_field_values` | 字段去重取值（构造 filters 用） | `dataset`、`field` |
| `dna_search_story` | 跨剧情语料检索，返回出处 + 片段 | `query`、`scope`、`limit` |
| `dna_read_story` | 按任务链 id 组装概要 + 章节 +（可选）台词 | `chain_id`、`include_dialogue`、`max_dialogues` |

几个实现细节：

- `dataset` 支持三种写法：数据集 id（`char`）、模块 id（`mod`）、模块中文名（`角色` / `魔之楔`）；
- 剧情检索自动给概要补上「哪一章 · 哪一节 · 任务链名」，给语音 / 档案补上角色名；
- 台词里的 `{nickname}`、`{性别：她|他}` 这类占位符会被收敛成「你」「她/他」；
- 所有输出都做递归收缩（长字符串、长数组）并受 `max_chars` 限制，单条工具结果不会撑爆上下文。

## 配置

WebUI 插件配置页（`_conf_schema.json`）：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `data_source` | `auto` | `auto` / `pack` / `api`，见上一节 |
| `pack_base_url` | `https://cdn.dobapp.cc/data-pack/` | 官方数据包 CDN，可换成自建镜像 |
| `pack_refresh_hours` | `12` | 数据包更新检查间隔（小时），`0` 表示不主动检查 |
| `pack_timeout` | `60` | 数据包下载超时（秒） |
| `pack_max_mb` | `128` | 数据包体积上限，超限放弃下载并回落到在线查询 |
| `timeout` | `20` | 单次查询超时（秒） |
| `max_chars` | `2600` | 单次工具返回字符上限 |
| `proxy` | 空 | 可选 HTTP 代理 |
| `enable_llm_tools` | `true` | 关闭后模型不自动查，但 `/dna` 指令仍可用 |
| `api_endpoint` | `https://api.dna-builder.cn/graphql` | 仅在 `api` / `auto` 兜底时用到的实时在线查询地址 |

## 自测

不启动 AstrBot 也能验证整条数据链路：

```bash
python tests/selftest.py
```

会检查模块列表、数据集名解析、检索、详情、筛选项、剧情检索、台词说话人解析、错误处理
以及六个工具的调用与 JSON Schema 合法性；末尾还会下载官方数据包，把同一批查询在
「在线查询」与「本地数据包」两条路上跑一遍并逐项比对（首次运行需要约 22 MB 下载）。

## 已知限制

- 剧情对话原文（`quest`）单条记录 5~8KB，`dna_search_story` 默认不搜它，需要时显式传
  `scope="dialog"` / `scope="对话"`；
- 数据的新鲜度以官方数据包发布为准（`/dna 数据包` 能看到当前版本与构建时间）；需要绝对
  最新的内容，把 `data_source` 设成 `api` 走实时在线查询；
- 一次查询只针对一个数据集，没有跨数据集全文搜索；跨模块找线索请用 `dna_search_story`，
  或先用 `dna_list_data_modules` 定位模块；
- 多语言翻译表（`translations`，17 MB）不进内存缓存，只有显式查它才解码；
- QQ 单条消息长度有限，指令回复截断到 1500 字（可改 `main.py` 里的 `COMMAND_MAX_CHARS`）；
- 数据包模式联网极少（只有版本检查与下载）；如果切到 `api` 模式，也请别高频轮询，
  插件对元信息做了 30 分钟缓存。

## 致谢与许可

- 数据来源：[DNA Builder](https://github.com/pa001024/dna-builder) 官方数据包，
  插件也支持公开的实时在线查询；
- 本插件代码可自由修改分发，请遵守上游项目的许可与使用约定。
