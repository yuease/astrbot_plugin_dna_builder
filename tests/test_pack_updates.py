"""数据包更新回归测试，无需联网或安装 AstrBot。

运行：python -m unittest discover -s tests -p 'test_pack_updates.py' -v
"""

from __future__ import annotations

import asyncio
import importlib
import io
import json
import sys
import tempfile
import time
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dna.client import DnaClient, DnaError
from dna.gateway import DnaGateway
from dna.pack import DnaPack


def package(version: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps({
            "version": version,
            "builtAt": "2026-10-22T00:00:00Z",
            "modules": {"char.data": {}},
        }))
    return buffer.getvalue()


class PackUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pack = DnaPack(self.temp.name)
        self.requests = []
        self.response_status = 200
        self.block = None
        self.pack._http = httpx.AsyncClient(transport=httpx.MockTransport(self.respond))
        self.gateway = DnaGateway(DnaClient(), self.pack)
        self.addAsyncCleanup(self.gateway.aclose)
        self.seed_old_pack()

    def seed_old_pack(self):
        self.pack.version = "old"
        self.pack.zip_path.write_bytes(package("old"))
        self.pack.checked_at = time.time() - 13 * 3600
        self.pack._save_state()

    async def respond(self, request):
        self.requests.append(request.url.path)
        if self.block is not None:
            await self.block.wait()
        if request.url.path.endswith("versions.json"):
            return httpx.Response(self.response_status, json=[{
                "version": "new", "packageFile": "new.zip",
            }])
        return httpx.Response(200, content=package("new"))

    async def finish_update(self):
        self.assertIsNotNone(self.gateway._warm_task)
        await asyncio.wait_for(self.gateway._warm_task, timeout=2)

    async def test_startup_updates_existing_stale_pack(self):
        self.gateway.start_warmup()
        self.gateway.start_warmup()
        await self.finish_update()
        self.assertEqual(self.pack.version, "new")
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(self.pack.is_ready())
        await self.gateway.modules()
        self.assertEqual(len(self.requests), 2)

    async def test_queries_keep_working_while_update_downloads(self):
        for mode in ("auto", "pack"):
            with self.subTest(mode=mode):
                self.gateway.mode = mode
                self.pack.checked_at = 0
                self.block = asyncio.Event()
                rows = await self.gateway.modules()
                self.assertEqual(rows[0]["id"], "char")
                self.assertEqual(self.pack.version, "old" if mode == "auto" else "new")
                self.block.set()
                await self.finish_update()
        self.assertEqual(self.pack.version, "new")

    async def test_fresh_pack_does_not_request_network(self):
        self.pack.checked_at = time.time()
        await self.gateway.modules()
        self.assertIsNone(self.gateway._warm_task)
        self.assertEqual(self.requests, [])

    async def test_disabled_checks_allow_manual_update(self):
        self.pack.refresh_hours = 0
        self.gateway.start_warmup()
        self.assertIsNone(self.gateway._refresh_task)
        self.assertIsNone(self.gateway._warm_task)
        self.gateway.start_warmup(force=True)
        await self.finish_update()
        self.assertEqual(self.pack.version, "new")

    async def test_disabled_checks_still_allow_first_download(self):
        await self.pack.aclose()
        self.pack.version = ""
        self.pack.refresh_hours = 0
        self.pack._http = httpx.AsyncClient(transport=httpx.MockTransport(self.respond))
        self.gateway.start_warmup()
        await self.finish_update()
        self.assertEqual(self.pack.version, "new")

    async def test_failed_check_keeps_old_pack_and_is_throttled(self):
        self.response_status = 503
        self.gateway.start_warmup()
        await self.finish_update()
        self.assertEqual(self.pack.version, "old")
        self.assertIn("503", self.pack.last_error)
        await self.gateway.modules()
        self.assertEqual(len(self.requests), 1)
        state = json.loads((Path(self.temp.name) / "state.json").read_text())
        self.assertGreater(state["checkedAt"], time.time() - 10)
        self.response_status = 200
        self.gateway.start_warmup(force=True)
        await self.finish_update()
        self.assertEqual(self.pack.version, "new")
        self.assertEqual(self.pack.last_error, "")

    async def test_bad_download_keeps_old_pack(self):
        self.pack._download = AsyncMock(side_effect=DnaError("download failed"))
        self.assertTrue(await self.pack.ensure())
        self.assertEqual(self.pack.version, "old")
        self.assertTrue(self.pack.is_ready())
        self.assertEqual(self.pack.last_error, "download failed")

    async def test_interrupted_stream_falls_back_to_complete_ranges(self):
        data = package("new")

        class BrokenStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield data[:10]
                raise httpx.RemoteProtocolError("interrupted download")

        def respond(request):
            byte_range = request.headers.get("range")
            if byte_range is None:
                return httpx.Response(200, stream=BrokenStream())
            start, end = map(int, byte_range.removeprefix("bytes=").split("-"))
            return httpx.Response(206, content=data[start:end + 1], headers={
                "Content-Range": f"bytes {start}-{end}/{len(data)}",
            })

        await self.pack._http.aclose()
        self.pack._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        target = Path(self.temp.name) / "download.zip"
        self.assertEqual(await self.pack._download("https://test/new.zip", target), len(data))
        self.assertEqual(target.read_bytes(), data)

    async def test_truncated_or_wrong_range_is_rejected(self):
        for headers, data in (
            ({"Content-Range": "bytes 0-9/10"}, b"short"),
            ({"Content-Range": "bytes 1-10/10"}, b"0123456789"),
        ):
            with self.subTest(headers=headers):
                def respond(request):
                    if request.headers["range"] == "bytes=0-0":
                        return httpx.Response(206, content=b"0", headers={
                            "Content-Range": "bytes 0-0/10",
                        })
                    return httpx.Response(206, content=data, headers=headers)

                await self.pack._http.aclose()
                self.pack._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
                with self.assertRaises(DnaError):
                    await self.pack._download_ranged(
                        "https://test/new.zip", Path(self.temp.name) / "download.part"
                    )

    async def test_transient_range_failure_is_retried(self):
        attempts = 0

        def respond(request):
            nonlocal attempts
            if request.headers["range"] == "bytes=0-0":
                return httpx.Response(206, content=b"0", headers={
                    "Content-Range": "bytes 0-0/10",
                })
            attempts += 1
            if attempts == 1:
                raise httpx.RemoteProtocolError("connection interrupted")
            return httpx.Response(206, content=b"0123456789", headers={
                "Content-Range": "bytes 0-9/10",
            })

        await self.pack._http.aclose()
        self.pack._http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch("dna.pack.asyncio.sleep", new_callable=AsyncMock):
            size = await self.pack._download_ranged(
                "https://test/new.zip", Path(self.temp.name) / "download.part"
            )
        self.assertEqual(size, 10)
        self.assertEqual(attempts, 2)

    async def test_error_clears_when_same_version_check_succeeds(self):
        await self.pack.ensure()
        self.pack.last_error = "previous failure"
        await self.pack.ensure(force=True)
        self.assertEqual(self.pack.last_error, "")
        self.assertEqual(self.requests.count("/data-pack/new.zip"), 1)

    async def test_timer_updates_without_queries(self):
        self.pack.checked_at = time.time()
        tick = asyncio.Event()
        sleeping = asyncio.Event()

        async def sleep(_seconds):
            sleeping.set()
            await tick.wait()
            tick.clear()

        with patch("dna.gateway.asyncio.sleep", sleep):
            self.gateway.start_warmup()
            await asyncio.wait_for(sleeping.wait(), timeout=2)
            self.pack.checked_at = 0
            tick.set()
            # 等待后台 HTTP 请求，未发起任何查询。
            for _ in range(20):
                await self.yield_loop()
                if self.gateway._warm_task is not None:
                    break
            await self.finish_update()
            self.assertEqual(self.pack.version, "new")

    async def yield_loop(self):
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        loop.call_soon(future.set_result, None)
        await future

    async def test_shutdown_awaits_timer_and_download_cancellation(self):
        self.block = asyncio.Event()
        self.gateway.start_warmup()
        tasks = (self.gateway._refresh_task, self.gateway._warm_task)
        await self.gateway.aclose()
        self.assertTrue(all(task.done() for task in tasks))

    async def test_api_mode_never_starts_pack_tasks(self):
        self.gateway.mode = "api"
        self.gateway.start_warmup(force=True)
        self.assertIsNone(self.gateway._warm_task)
        self.assertIsNone(self.gateway._refresh_task)

    async def test_missing_pack_download_failure_is_reported(self):
        await self.pack.aclose()
        self.pack.version = ""
        self.pack._http = httpx.AsyncClient(transport=httpx.MockTransport(self.respond))
        self.response_status = 503
        with self.assertRaises(DnaError):
            await self.pack.ensure()
        self.assertIn("503", self.pack.last_error)


class PluginUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_config_and_manual_command(self):
        import import_check

        import_check.install_astrbot_stubs()
        package_name = import_check.PACKAGE_NAME
        plugin_package = types.ModuleType(package_name)
        plugin_package.__path__ = [str(import_check.PLUGIN_DIR)]
        sys.modules.setdefault(package_name, plugin_package)
        module = importlib.import_module(f"{package_name}.main")
        with tempfile.TemporaryDirectory() as cache, patch.object(
            module.DnaBuilderPlugin, "_pack_cache_dir", return_value=Path(cache)
        ):
            plugin = module.DnaBuilderPlugin(import_check.FakeContext(), {
                "pack_refresh_hours": 0, "enable_llm_tools": False,
            })
            self.addAsyncCleanup(plugin.terminate)
            self.assertEqual(plugin.pack.refresh_hours, 0)
            with patch.object(plugin.client, "start_warmup") as warmup:
                text = await plugin._handle("数据包 更新")
                self.assertIn("数据源模式", text)
                self.assertEqual(warmup.call_args_list[0].kwargs, {"force": True})


if __name__ == "__main__":
    unittest.main()
