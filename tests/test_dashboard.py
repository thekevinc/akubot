from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from aiohttp.test_utils import make_mocked_request

from akubot.config import Persona
from akubot.control import Control, RevisionConflict, Tuning
from akubot.dashboard import Dashboard
from akubot.memory import Memory
from akubot.metrics import Metrics
from akubot.service import Gate, Notice, Request, Responder
from akubot.storage import Store


PERSONA = Persona("Aku", "Synthetic test persona", ("Short replies",), ())
MODEL = "llama3.1:8b"


class FakeModel:
    def __init__(self):
        self.calls = []
        self.last_metrics = {"model_seconds": .3, "load_seconds": .01, "generation_seconds": .2, "input_tokens": 10, "output_tokens": 5}

    async def generate(self, messages):
        self.calls.append(messages)
        return "SYNTHETIC_REPLY_CANARY"

    async def status(self):
        return {"available": True, "loaded": True, "name": MODEL, "installed": [MODEL], "gpu_percent": 100, "vram_gb": 5.3}

    async def warm(self):
        return {"model_seconds": .1, "load_seconds": .1}


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "aku.sqlite3")
        self.control = Control(self.store, MODEL, PERSONA)
        self.model = FakeModel()
        self.responder = Responder(self.store, self.model, PERSONA, control=self.control)
        self.client = SimpleNamespace(
            control=self.control, store=self.store, responder=self.responder,
            settings=SimpleNamespace(guild_ids=frozenset({1})), latency=.03,
            is_ready=lambda: True,
        )
        self.dashboard = Dashboard(self.client)
        self.app = self.dashboard.app()
        self.app.freeze()
        await self.app.startup()

    async def asyncTearDown(self):
        await self.dashboard.close()
        await self.app.cleanup()
        self.store.close()
        self.temp.cleanup()

    async def request(self, method, path, body=None, headers=None):
        values = {"Host": "localhost:8765", "X-Aku-Local": "1"}
        if body is not None:
            values["Content-Type"] = "application/json"
        values.update(headers or {})
        values = {key: value for key, value in values.items() if value is not None}
        request = make_mocked_request(method, path, headers=values, app=self.app)
        if body is not None:
            request._read_bytes = json.dumps(body).encode()
        response = await self.app._handle(request)
        return response

    async def json_request(self, *args, **kwargs):
        response = await self.request(*args, **kwargs)
        return response.status, json.loads(response.text)

    async def test_dashboard_assets_and_security_headers(self):
        for path in ("/", "/app.js", "/style.css"):
            response = await self.request("GET", path, headers={"X-Aku-Local": None})
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    async def test_foreign_origin_rebinding_and_missing_local_header_blocked(self):
        for headers in (
            {"Host": "attacker.example:8765"},
            {"Origin": "https://attacker.example"},
            {"Origin": "http://localhost:9000"},
            {"Origin": "null"},
            {"X-Aku-Local": None},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            status, _ = await self.json_request("GET", "/api/examples", headers=headers)
            self.assertEqual(status, 403)
        status, _ = await self.json_request("GET", "/api/status", headers={"Origin": "http://localhost:8765"})
        self.assertEqual(status, 200)

    async def test_cross_site_forms_and_preflight_cannot_change_settings(self):
        status, _ = await self.json_request("POST", "/api/pause", {"paused": True}, headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 415)
        status, _ = await self.json_request("OPTIONS", "/api/pause")
        self.assertEqual(status, 405)
        self.assertFalse(self.store.is_paused(1))

    async def test_tuning_applies_to_cooldown_without_restart_and_persists(self):
        status, original = await self.json_request("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(original["tuning"]["cooldown_seconds"], 2)
        tuning = {**original["tuning"], "cooldown_seconds": 0, "max_tokens": 64}
        status, saved = await self.json_request("PUT", "/api/settings", {"revision": original["revision"], "tuning": tuning})
        self.assertEqual(status, 200)
        await self.responder.reply(Request(1, 2, 3, "first"))
        await self.responder.reply(Request(1, 2, 3, "second"))
        restored = Control(self.store, "other-model", PERSONA)
        self.assertEqual(restored.tuning.max_tokens, 64)
        self.assertEqual(restored.tuning.cooldown_seconds, 0)
        self.assertEqual(saved["revision"], 1)

    async def test_stale_tabs_cannot_overwrite_newer_settings(self):
        tuning = asdict(self.control.tuning)
        self.assertEqual((await self.json_request("PUT", "/api/settings", {"revision": 0, "tuning": tuning}))[0], 200)
        self.assertEqual((await self.json_request("PUT", "/api/settings", {"revision": 0, "tuning": tuning}))[0], 409)

    async def test_invalid_or_remote_model_options_rejected(self):
        for change in ({"temperature": float("nan")}, {"temperature": True}, {"max_tokens": 1}, {"memory_turns": -1}, {"keep_alive": []}, {"preload": "true"}, {"model": "https://outside.example"}, {"extra": "value"}):
            tuning = {**asdict(self.control.tuning), **change}
            status, _ = await self.json_request("PUT", "/api/settings", {"revision": 0, "tuning": tuning})
            self.assertEqual(status, 400, change)
        self.assertEqual(self.control.revision, 0)

    async def test_local_preview_is_ephemeral_and_activity_never_contains_content(self):
        status, data = await self.json_request("POST", "/api/preview", {"prompt": "SYNTHETIC_INPUT_CANARY"})
        self.assertEqual(status, 200)
        self.assertEqual(data["reply"], "SYNTHETIC_REPLY_CANARY")
        self.assertEqual(self.store.count(), 0)
        self.assertFalse(self.responder.memory.sessions)
        _, status_data = await self.json_request("GET", "/api/status")
        rendered = json.dumps(status_data)
        self.assertNotIn("SYNTHETIC_INPUT_CANARY", rendered)
        self.assertNotIn("SYNTHETIC_REPLY_CANARY", rendered)
        self.assertEqual(status_data["metrics"]["counts"]["preview_reply"], 1)

    async def test_pause_resume_and_clear_are_scoped_and_keep_library(self):
        self.store.add_examples(["synthetic reviewed example"])
        self.responder.memory.append((1, 2, 3), "synthetic input", "synthetic output")
        self.responder.memory.append((9, 2, 3), "other scope", "other scope")
        self.assertEqual((await self.json_request("POST", "/api/pause", {"paused": True}))[0], 200)
        self.assertTrue(self.store.is_paused(1))
        self.assertFalse(self.responder.memory.get((1, 2, 3)))
        self.assertTrue(self.responder.memory.get((9, 2, 3)))
        self.assertEqual(self.store.count(), 1)
        self.assertEqual((await self.json_request("POST", "/api/pause", {"paused": False}))[0], 200)
        self.assertFalse(self.store.is_paused(1))
        self.assertEqual((await self.json_request("POST", "/api/forget", {}))[0], 200)

    async def test_personality_update_cancels_stale_generation(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def generate(messages):
            started.set()
            await finish.wait()
            return "old-personality reply"
        self.model.generate = generate
        task = asyncio.create_task(self.responder.reply(Request(1, 2, 3, "synthetic prompt")))
        await started.wait()
        persona = asdict(PERSONA)
        persona["public_facts"] = ["A synthetic public fact"]
        status, _ = await self.json_request("PUT", "/api/persona", {"revision": 0, "persona": persona})
        self.assertEqual(status, 200)
        finish.set()
        with self.assertRaises(Notice):
            await task
        self.assertFalse(self.responder.memory.sessions)

    async def test_library_paging_literal_search_and_word_frequencies(self):
        self.store.add_examples(["the pizza pizza rockets", "Rockets and games", "100% synthetic"])
        status, summary = await self.json_request("GET", "/api/library")
        self.assertEqual(status, 200)
        counts = {entry["word"]: entry["count"] for entry in summary["words"]}
        self.assertEqual(counts["pizza"], 2)
        self.assertEqual(counts["rockets"], 2)
        self.assertNotIn("the", counts)
        status, page = await self.json_request("GET", "/api/examples?q=%25")
        self.assertEqual(status, 200)
        self.assertEqual(page["total"], 1)
        status, page = await self.json_request("GET", "/api/examples?q=%27%20OR%201%3D1")
        self.assertEqual(status, 200)
        self.assertEqual(page["total"], 0)
        self.assertEqual(self.store.count(), 3)
        self.assertEqual((await self.json_request("GET", "/api/examples?offset=-1"))[0], 400)

    async def test_model_warm_is_bounded(self):
        started, finish = asyncio.Event(), asyncio.Event()
        async def warm():
            started.set()
            await finish.wait()
            return {"load_seconds": .1}
        self.model.warm = warm
        self.assertEqual((await self.json_request("POST", "/api/warm", {}))[0], 202)
        await started.wait()
        self.assertEqual((await self.json_request("POST", "/api/warm", {}))[0], 409)
        finish.set()
        await self.dashboard.warm_task
        self.assertEqual(self.responder.metrics.snapshot()["counts"]["warm_ready"], 1)


class RuntimeTests(unittest.TestCase):
    def test_zero_memory_turns_really_disables_memory(self):
        memory = Memory(max_turns=0)
        for _ in range(10):
            memory.append((1, 2, 3), "synthetic input", "synthetic output")
        self.assertEqual(memory.get((1, 2, 3)), [])
        self.assertFalse(memory.sessions)

    def test_metrics_are_bounded_without_identities_or_contents(self):
        metrics = Metrics()
        for _ in range(150):
            identifier = metrics.begin()
            metrics.active(identifier)
            metrics.finish(identifier, "reply", {"load_seconds": .1, "text": "SYNTHETIC_SECRET"})
        data = metrics.snapshot()
        self.assertEqual(len(data["recent"]), 100)
        self.assertEqual(data["counts"]["reply"], 150)
        self.assertNotIn("SYNTHETIC_SECRET", json.dumps(data))


if __name__ == "__main__":
    unittest.main()
