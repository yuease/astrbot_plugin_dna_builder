"""
官方数据包本地后端。

DNA Builder 的桌面版 / 移动端并不逐条在线查询，而是下载一份官方数据包（zip 内是
msgpack 编好的各模块数据），本地解码后离线查询。本插件复刻这条路径：

1. `GET {base}/versions.json` 取版本列表（几 KB，用来判断要不要更新）；
2. 下载 `{base}/{version}.zip`（约 22 MB，Cloudflare CDN），落到插件数据目录缓存；
3. 按需解出 `modules/<模块>.data.msgpack`，归一成记录表后本地检索。

好处是查询不再请求在线服务（只有版本检查与偶尔的数据包下载，走的是 CDN），
代价是数据新鲜度以数据包发布为准（官方发版很勤，实测当天就有新包）。
解码实测：char 1.7ms、questchain 1.2ms、quest 31ms/20MB，完全够用。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import zipfile
from collections import OrderedDict
from pathlib import Path
from typing import Any

import httpx

from .client import DnaError
from .labels import HEAVY_MODULES, module_label
from .store import LocalDataset, normalize

DEFAULT_PACK_BASE_URL = "https://cdn.dobapp.cc/data-pack/"
"""官方数据包 CDN 基址（与 DOB 客户端一致，可在配置里覆盖成自建镜像）。"""

USER_AGENT = "astrbot-plugin-dna-builder/1.2.0 (+https://github.com/yuease/astrbot_plugin_dna_builder)"

MANIFEST_FILE = "manifest.json"
VERSIONS_FILE = "versions.json"
STATE_FILE = "state.json"

MODULE_CACHE_SIZE = 4
"""解码后的模块值缓存个数（超出按 LRU 淘汰）。"""

DATASET_CACHE_SIZE = 12
"""归一化后的数据集缓存个数。"""

_LOCALE_SUFFIXES = ("en", "fr", "jp", "kr", "tc")


class DnaPack:
    """官方数据包的下载、缓存与查询实现（行为与 DnaClient 对齐）。"""

    def __init__(
        self,
        cache_dir: str | Path,
        base_url: str = DEFAULT_PACK_BASE_URL,
        timeout: float = 60.0,
        refresh_hours: float = 12.0,
        max_mb: int = 128,
        max_chars: int = 2600,
    ) -> None:
        """
        @param cache_dir: 数据包缓存目录（插件数据目录下）
        @param base_url: 数据包 CDN 基址
        @param timeout: 单次请求超时（秒）
        @param refresh_hours: 间隔多久去检查一次新版本
        @param max_mb: 允许下载的数据包体积上限（MB）
        @param max_chars: 单次工具返回字符上限（与在线查询后端保持一致）
        """
        self.base = (base_url or DEFAULT_PACK_BASE_URL).rstrip("/") + "/"
        self.cache_dir = Path(cache_dir)
        self.timeout = max(5.0, float(timeout or 60.0))
        self.refresh_hours = max(0.0, float(refresh_hours or 12.0))
        self.max_bytes = max(1, int(max_mb or 128)) * 1024 * 1024
        self.max_chars = max(500, int(max_chars or 2600))

        self.version = ""
        self.built_at = ""
        self.package_file = ""
        self.checked_at = 0.0
        self.last_error = ""

        self._http: httpx.AsyncClient | None = None
        self._manifest_cache: dict | None = None
        self._zip: zipfile.ZipFile | None = None
        self._modules: OrderedDict[str, dict] = OrderedDict()
        self._datasets: OrderedDict[str, LocalDataset] = OrderedDict()
        self._dataset_index: dict[str, tuple[str, str, str, int]] | None = None
        self._counts: dict[str, int | None] = {}
        self._lock = asyncio.Lock()

        self._load_state()

    # ------------------------------------------------------------ 缓存与状态

    @property
    def zip_path(self) -> Path:
        """当前版本数据包的本地路径。"""
        return self.cache_dir / f"{self.version or 'pack'}.zip"

    def _load_state(self) -> None:
        """启动时读取本地状态；数据包完好就直接进入就绪状态。"""
        try:
            state = json.loads(
                (self.cache_dir / STATE_FILE).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return

        self.version = str(state.get("version") or "")
        self.built_at = str(state.get("builtAt") or "")
        self.package_file = str(state.get("packageFile") or "")
        self.checked_at = float(state.get("checkedAt") or 0)

        if self.version and not self.zip_path.exists():
            self.version = ""

    def _save_state(self) -> None:
        """把版本信息写回磁盘（失败不影响使用）。"""
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            (self.cache_dir / STATE_FILE).write_text(
                json.dumps(
                    {
                        "version": self.version,
                        "builtAt": self.built_at,
                        "packageFile": self.package_file,
                        "checkedAt": self.checked_at,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            self.last_error = f"写入状态失败：{exc}"

    def is_ready(self) -> bool:
        """数据包是否已就绪（本地有包且 manifest 可读）。"""
        if not self.version:
            return False

        try:
            return MANIFEST_FILE in self._open_zip().namelist()
        except (OSError, zipfile.BadZipFile):
            self.version = ""

            return False

    def status(self) -> dict:
        """给指令 / 日志用的状态摘要。"""
        size = (
            self.zip_path.stat().st_size
            if self.version and self.zip_path.exists()
            else 0
        )

        return {
            "ready": self.is_ready(),
            "version": self.version,
            "builtAt": self.built_at,
            "checkedAt": self.checked_at,
            "cacheDir": str(self.cache_dir),
            "sizeMB": round(size / 1048576, 1),
            "lastError": self.last_error,
        }

    async def _client(self) -> httpx.AsyncClient:
        """懒加载 HTTP 客户端。"""
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )

        return self._http

    async def aclose(self) -> None:
        """释放连接与 zip 句柄。"""
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
        self._http = None

        if self._zip is not None:
            try:
                self._zip.close()
            except Exception:  # noqa: BLE001 - 关闭失败无需影响插件卸载
                pass
            self._zip = None

    # -------------------------------------------------------- 版本检查与下载

    async def ensure(self, force: bool = False) -> bool:
        """
        确保本地数据包可用：必要时检查新版本并下载。

        @param force: 忽略刷新间隔，强制检查
        @return: 是否可用
        @raises DnaError: 本地没有包且无法下载时抛出（调用方据此回退在线查询）
        """
        async with self._lock:
            if self.is_ready() and not force and not self._need_check():
                return True

            try:
                versions = await self._fetch_versions()
            except DnaError as exc:
                if self.is_ready():  # 已经在用旧包，检查失败不影响使用
                    self.last_error = str(exc)

                    return True

                raise

            latest = versions[0]
            self.checked_at = time.time()

            if latest.get("version") == self.version and self.is_ready():
                self._save_state()

                return True

            await self._install(latest)

            return True

    def _need_check(self) -> bool:
        """是否到了下一次版本检查时间。"""
        if self.refresh_hours <= 0:
            return False

        return time.time() - self.checked_at > self.refresh_hours * 3600

    async def _fetch_versions(self) -> list[dict]:
        """取版本列表（最新在前）。"""
        client = await self._client()

        try:
            response = await client.get(f"{self.base}{VERSIONS_FILE}")
        except httpx.HTTPError as exc:
            raise DnaError(f"数据包版本列表获取失败：{exc}") from exc

        if response.status_code != 200:
            raise DnaError(f"数据包版本列表返回 HTTP {response.status_code}")

        try:
            versions = response.json()
        except ValueError as exc:
            raise DnaError("数据包版本列表不是合法 JSON") from exc

        if not isinstance(versions, list) or not versions:
            raise DnaError("数据包版本列表为空")

        return versions

    async def _install(self, latest: dict) -> None:
        """下载并安装某个版本的数据包。"""
        version = str(latest.get("version") or "")
        package_file = str(latest.get("packageFile") or f"{version}.zip")
        if not version:
            raise DnaError("数据包版本信息缺少 version 字段")

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        target = self.cache_dir / f"{version}.zip"
        url = f"{self.base}{package_file}"

        if not target.exists() or target.stat().st_size == 0:
            size = await self._download(url, target)
            if size > self.max_bytes:
                target.unlink(missing_ok=True)
                raise DnaError(
                    f"数据包体积 {size / 1048576:.1f}MB 超过上限，已放弃下载"
                )

        manifest = self._read_manifest(target)
        if not manifest:
            target.unlink(missing_ok=True)
            raise DnaError("数据包缺少 manifest.json，可能下载不完整")

        # 切换版本：清掉旧句柄与缓存
        if self._zip is not None:
            try:
                self._zip.close()
            except Exception:  # noqa: BLE001
                pass
            self._zip = None
        self._modules.clear()
        self._datasets.clear()
        self._dataset_index = None
        self._counts.clear()
        self._manifest_cache = None

        self.version = str(manifest.get("version") or version)
        self.built_at = str(manifest.get("builtAt") or latest.get("builtAt") or "")
        self.package_file = package_file
        self.last_error = ""
        self._save_state()

    async def _download(self, url: str, target: Path) -> int:
        """
        下载数据包：先试整包流式下载，若 CDN 返回空体则退回分片下载。

        （实测该 CDN 对整包 GET 会返回 200 但空体，Range 请求正常，因此必须保留两条路径）

        @param url: 包地址
        @param target: 目标文件
        @return: 写入字节数
        """
        part = target.with_suffix(".part")
        written = await self._stream_to_file(url, part)

        if written == 0:
            written = await self._download_ranged(url, part)

        if written == 0:
            part.unlink(missing_ok=True)
            raise DnaError("数据包下载失败：服务器未返回内容")

        part.replace(target)

        return written

    async def _stream_to_file(self, url: str, dest: Path) -> int:
        """整包流式下载，返回写入字节数；网络异常时返回 0 由调用方兜底。"""
        client = await self._client()
        written = 0

        try:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise DnaError(f"数据包下载返回 HTTP {response.status_code}")

                with dest.open("wb") as handle:
                    async for chunk in response.aiter_bytes(1 << 20):
                        handle.write(chunk)
                        written += len(chunk)
        except httpx.HTTPError as exc:
            raise DnaError(f"数据包下载失败：{exc}") from exc

        return written

    async def _download_ranged(self, url: str, dest: Path) -> int:
        """分片下载（Range），用于 CDN 整包请求返回空体的情况。"""
        client = await self._client()
        chunk_size = 4 << 20

        probe = await client.get(url, headers={"Range": "bytes=0-0"})
        content_range = probe.headers.get("content-range") or ""
        match = re.search(r"/(\d+)\s*$", content_range)
        if probe.status_code != 206 or not match:
            return 0

        total = int(match.group(1))
        if total <= 0 or total > self.max_bytes:
            return 0

        written = 0
        with dest.open("wb") as handle:
            while written < total:
                end = min(written + chunk_size - 1, total - 1)
                response = await client.get(
                    url, headers={"Range": f"bytes={written}-{end}"}
                )
                response.raise_for_status()
                if not response.content:
                    break
                handle.write(response.content)
                written += len(response.content)

        return written

    def _read_manifest(self, path: Path) -> dict | None:
        """读数据包里的 manifest.json，顺带验证包完整性。"""
        try:
            with zipfile.ZipFile(path) as archive:
                return json.loads(archive.read(MANIFEST_FILE).decode("utf-8"))
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            return None

    def _open_zip(self) -> zipfile.ZipFile:
        """打开当前版本的数据包（复用句柄）。"""
        if self._zip is None:
            self._zip = zipfile.ZipFile(self.zip_path)

        return self._zip

    # -------------------------------------------------------------- 数据读取

    def _manifest(self) -> dict:
        """当前数据包的 manifest（缓存）。"""
        if self._manifest_cache is None:
            if not self.is_ready():
                raise DnaError("数据包尚未就绪")

            self._manifest_cache = self._read_manifest(self.zip_path) or {}

        return self._manifest_cache

    def _module_keys(self) -> dict[str, dict]:
        """manifest 里的模块表：模块键（abyss.data）→ 模块信息。"""
        modules = self._manifest().get("modules")

        return modules if isinstance(modules, dict) else {}

    def _module_value(self, module_key: str) -> dict:
        """解码并缓存一个模块（translations 不缓存）。"""
        cached = self._modules.get(module_key)
        if cached is not None:
            self._modules.move_to_end(module_key)

            return cached

        try:
            raw = self._open_zip().read(f"modules/{module_key}.msgpack")
        except (KeyError, OSError, zipfile.BadZipFile) as exc:
            raise DnaError(f"数据包里没有模块 {module_key}") from exc

        try:
            import msgpack
        except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确提示
            raise DnaError(
                "本地数据包需要 msgpack 依赖，请重装插件以安装 requirements.txt"
            ) from exc

        value = msgpack.unpackb(raw, raw=False, strict_map_key=False)
        if not isinstance(value, dict):
            value = {"default": value}

        base_id = (
            module_key[: -len(".data")] if module_key.endswith(".data") else module_key
        )
        if base_id not in HEAVY_MODULES:
            self._modules[module_key] = value
            while len(self._modules) > MODULE_CACHE_SIZE:
                self._modules.popitem(last=False)

        return value

    def _index(self) -> dict[str, tuple[str, str]]:
        """
        数据集 id → (模块键, 导出名, 形态, 条数) 的索引。

        命名口径与服务端注册表（server/src/db/mod/gameDataRegistry.ts）一致：
        只有「可查询」的导出（值为数组或对象）才算数据集——标量常量与被打包时丢弃的函数
        都不算；`default` 为主导出，占用模块 id；没有 `default` 但**只有一个**可查询导出时，
        该导出占用模块 id；其余具名导出一律是 `模块 id:导出名`。
        """
        if self._dataset_index is not None:
            return self._dataset_index

        index: dict[str, tuple[str, str, str, int]] = {}
        for module_key in self._module_keys():
            base_id = (
                module_key[: -len(".data")]
                if module_key.endswith(".data")
                else module_key
            )
            exports, primary, values = self._queryable_exports(module_key)
            if not values:
                continue

            if primary:
                index[base_id] = (
                    module_key,
                    primary,
                    self._kind_of(values[primary]),
                    self._count_of(values[primary]),
                )

            for export in exports:
                if export == primary:
                    continue
                index[f"{base_id}:{export}"] = (
                    module_key,
                    export,
                    self._kind_of(values[export]),
                    self._count_of(values[export]),
                )

        self._dataset_index = index

        return index

    def _queryable_exports(self, module_key: str) -> tuple[list[str], str | None, dict]:
        """
        取模块里可查询的导出（值为数组 / 对象）、主导出名与已解码的导出值。

        @param module_key: 模块键（abyss.data）
        @return: (可查询导出名列表, 主导出名或 None, {导出名: 值})
        """
        info = self._module_keys().get(module_key) or {}
        exports = [str(name) for name in (info.get("exports") or [])]
        if not exports:
            return [], None, {}

        try:
            raw = self._module_value(module_key)
        except DnaError:
            return [], None, {}

        values = {
            name: raw.get(name)
            for name in exports
            if isinstance(raw.get(name), (list, dict))
        }
        kept = [name for name in exports if name in values]
        if not kept:
            return [], None, {}

        if "default" in kept:
            primary: str | None = "default"
        elif len(kept) == 1:
            primary = kept[0]
        else:
            primary = None

        return kept, primary, values

    @staticmethod
    def _count_of(value: Any) -> int:
        """记录数：数组取长度，对象取键数。"""
        if isinstance(value, list):
            return len(value)

        if isinstance(value, dict):
            return len(value)

        return 0

    @staticmethod
    def _kind_of(value: Any) -> str:
        """
        导出形态：数组 / 对象。

        服务端还会区分 JS `Map`（kind=map），但 msgpack 里 Map 与普通对象都编码成 map，
        解码后无法区分，这里统一按 `object` 报告——只影响列表展示，不影响检索。
        """
        if isinstance(value, list):
            return "array"

        return "object"

    def _base_module_id(self, module_id: str) -> str:
        """去掉语言后缀（charext.en → charext）。"""
        head, _, tail = module_id.rpartition(".")

        return head if head and tail in _LOCALE_SUFFIXES else module_id

    def _module_locale(self, module_id: str) -> str:
        """取模块语言码（无后缀视为 zh）。"""
        _head, _, tail = module_id.rpartition(".")

        return tail if tail in _LOCALE_SUFFIXES else "zh"

    def _dataset_ids(self, module_id: str) -> list[str]:
        """某模块（含语言变体）下的全部数据集 id。"""
        base = self._base_module_id(module_id)

        return [
            dataset_id
            for dataset_id, (
                module_key,
                _export,
                _kind,
                _count,
            ) in self._index().items()
            if self._base_module_id(
                module_key[: -len(".data")]
                if module_key.endswith(".data")
                else module_key
            )
            == base
        ]

    def datasets_sync(self) -> list[dict]:
        """
        全量数据集列表（结构对齐在线查询的 gameDataSets）。

        数据包里同名数据的具名导出（如 charext 的 charExtData 与 default 内容一致）会被折叠，
        否则列表里会出现一堆重复数据集；判重是轻量的「长度 + 首条记录键」比较。
        """
        rows: list[dict] = []
        for dataset_id, (module_key, export, kind, count) in self._index().items():
            module_id = (
                module_key[: -len(".data")]
                if module_key.endswith(".data")
                else module_key
            )
            base_module = self._base_module_id(module_id)

            if dataset_id != module_id and self._is_duplicate_export(
                module_key, export
            ):
                continue

            label = module_label(module_id) + (
                "" if dataset_id == module_id else f" · {export}"
            )

            rows.append(
                {
                    "id": dataset_id,
                    "module": module_id,
                    "exportName": export,
                    "label": label,
                    "baseId": base_module,
                    "locale": self._module_locale(module_id),
                    "variants": self._dataset_ids(module_id),
                    "kind": kind,
                    "count": count,
                }
            )

        return rows

    def _is_duplicate_export(self, module_key: str, export: str) -> bool:
        """
        判断具名导出是否与模块主导出内容重复（服务端同样会跳过这类导出）。

        @param module_key: 模块键
        @param export: 具名导出名
        @return: 是否可折叠
        """
        _exports, primary_export, _values = self._queryable_exports(module_key)
        if not primary_export or export == primary_export:
            return False

        try:
            values = self._module_value(module_key)
        except DnaError:
            return False

        left = values.get(export)
        right = values.get(primary_export)
        if left is None or right is None or type(left) is not type(right):
            return False

        if isinstance(left, list):
            if len(left) != len(right):
                return False
            if not left:
                return True

            first_left, first_right = left[0], right[0]
            if isinstance(first_left, dict) and isinstance(first_right, dict):
                return first_left.get("id") == first_right.get("id") and first_left.get(
                    "名称"
                ) == first_right.get("名称")

            return first_left == first_right

        if isinstance(left, dict):
            return len(left) == len(right) and set(left) == set(right)

        return left == right

    def _dataset(self, dataset_id: str) -> LocalDataset:
        """取（并缓存）某个数据集。"""
        cached = self._datasets.get(dataset_id)
        if cached is not None:
            self._datasets.move_to_end(dataset_id)

            return cached

        entry = self._index().get(dataset_id)
        if entry is None:
            raise DnaError(f"本地数据包里没有数据集 {dataset_id}")

        module_key, export, kind, _count = entry
        value = self._module_value(module_key).get(export)
        if value is None:
            raise DnaError(f"数据包模块 {module_key} 缺少导出 {export}")

        module_id = (
            module_key[: -len(".data")] if module_key.endswith(".data") else module_key
        )
        dataset = LocalDataset(
            dataset_id, module_id, normalize(value), export_name=export, kind=kind
        )
        self._datasets[dataset_id] = dataset
        self._counts[dataset_id] = dataset.count
        while len(self._datasets) > DATASET_CACHE_SIZE:
            self._datasets.popitem(last=False)

        return dataset

    # ------------------------------------------------------- 与在线查询一致的方法签名

    async def modules(self, refresh: bool = False) -> list[dict]:  # noqa: ARG002 - 保持与 DnaClient 同签名
        """模块列表（名字取本地快照表，缺失回退模块 id）。"""
        rows: list[dict] = []
        for module_key in self._module_keys():
            module_id = (
                module_key[: -len(".data")]
                if module_key.endswith(".data")
                else module_key
            )
            base = self._base_module_id(module_id)
            rows.append(
                {
                    "id": module_id,
                    "label": module_label(module_id),
                    "file": f"modules/{module_key}.msgpack",
                    "baseId": base,
                    "locale": self._module_locale(module_id),
                    "variants": sorted(
                        key[: -len(".data")] if key.endswith(".data") else key
                        for key in self._module_keys()
                        if self._base_module_id(
                            key[: -len(".data")] if key.endswith(".data") else key
                        )
                        == base
                    ),
                }
            )

        return rows

    async def datasets(self, module: str | None = None) -> list[dict]:
        """数据集列表（可按模块过滤）。"""
        rows = self.datasets_sync()
        if not module:
            return rows

        base = self._base_module_id(module)

        return [row for row in rows if row["baseId"] == base]

    async def resolve_dataset(self, name: str) -> tuple[str, str]:
        """把名字解析成 (数据集 id, 模块 id)，规则与在线查询后端一致。"""
        raw = (name or "").strip()
        if not raw:
            raise DnaError("必须提供数据集名（dataset）")

        index = self._index()
        modules = await self.modules()

        module_ids = {row["id"] for row in modules}
        label_to_id = {row["label"]: row["id"] for row in modules}

        module_id = raw if raw in module_ids else label_to_id.get(raw)
        if module_id:
            for dataset_id, (module_key, _export) in index.items():
                if (
                    module_key[: -len(".data")]
                    if module_key.endswith(".data")
                    else module_key
                ) == module_id:
                    return dataset_id, module_id

        if raw in index:
            module_key = index[raw][0]

            return raw, module_key[: -len(".data")] if module_key.endswith(
                ".data"
            ) else module_key

        lowered = raw.lower()
        candidates = [
            row
            for row in self.datasets_sync()
            if lowered in f"{row['id']} {row['label']} {row['baseId']}".lower()
        ]
        candidates.sort(
            key=lambda row: (row["id"] != row["baseId"], row["locale"] != "zh")
        )
        if candidates:
            best = candidates[0]

            return best["id"], best["module"]

        hint = "、".join(
            f"{row['id']}（{row['label']}）" for row in self.datasets_sync()[:12]
        )
        raise DnaError(f"找不到数据集「{raw}」。可用模块例如：{hint}")

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
        """本地检索（返回结构对齐在线查询）。"""
        local = self._dataset(dataset)

        return await asyncio.to_thread(
            local.search,
            query,
            filters,
            fields,
            max(1, min(int(limit or 5), 50)),
            max(0, int(offset or 0)),
            sort,
        )

    async def record(
        self, dataset: str, key: str, fields: list[str] | None = None
    ) -> dict | None:
        """按 key 取单条记录。"""
        local = self._dataset(dataset)
        if fields:
            page = local.search(
                filters=[{"field": "id", "op": "EQ", "value": key}],
                fields=fields,
                limit=1,
            )
            items = page.get("items") or []

            return items[0] if items else None

        return local.record(key)

    async def field_names(self, dataset: str, limit: int = 60) -> list[str]:
        """取数据集的顶层字段名。"""
        return self._dataset(dataset).field_names(limit)

    async def field_values(
        self, dataset: str, field: str, limit: int = 50
    ) -> list[dict]:
        """取某字段的去重取值与出现次数。"""
        return await asyncio.to_thread(
            self._dataset(dataset).field_values,
            field,
            max(1, min(int(limit or 50), 200)),
        )

    async def npc_names(self, ids: list[int]) -> dict[int, str]:
        """批量把 NPC id 解析成名字（失败时返回空表）。"""
        unique = [int(value) for value in dict.fromkeys(ids) if value is not None]
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
