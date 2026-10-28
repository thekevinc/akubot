from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
import time
from urllib.parse import urlsplit

from aiohttp import web

from .control import RevisionConflict, Tuning
from dataclasses import asdict
from .model import ModelError
from .service import Notice, Request


STATIC = Path(__file__).with_name("web")
HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
}


def local_host(value: str):
    try:
        parsed = urlsplit("http://" + value)
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            return None
        return parsed.hostname, parsed.port or 80
    except ValueError:
        return None


@web.middleware
async def private_boundary(request: web.Request, handler):
    host = local_host(request.headers.get("Host", ""))
    if host is None:
        return web.json_response({"error": "Open the dashboard using localhost."}, status=403, headers=HEADERS)
    if request.path.startswith("/api/"):
        origin = request.headers.get("Origin")
        if origin:
            try:
                parsed = urlsplit(origin)
                valid = parsed.scheme == "http" and (parsed.hostname, parsed.port or 80) == host and not parsed.username and not parsed.password and not parsed.path and not parsed.query and not parsed.fragment
            except ValueError:
                valid = False
            if not valid:
                return web.json_response({"error": "Cross-origin dashboard access is blocked."}, status=403, headers=HEADERS)
        if request.headers.get("X-Aku-Local") != "1" or request.headers.get("Sec-Fetch-Site") == "cross-site":
            return web.json_response({"error": "Use the local dashboard to access these controls."}, status=403, headers=HEADERS)
        if request.method not in {"GET", "PUT", "POST"}:
            return web.json_response({"error": "Method not allowed."}, status=405, headers=HEADERS)
        if request.method in {"PUT", "POST"} and request.content_type != "application/json":
            return web.json_response({"error": "Expected JSON."}, status=415, headers=HEADERS)
    try:
        response = await handler(request)
    except RevisionConflict as error:
        response = web.json_response({"error": str(error)}, status=409)
    except Notice as error:
        response = web.json_response({"error": str(error)}, status=429)
    except (ValueError, KeyError, TypeError):
        response = web.json_response({"error": "Check the supplied fields and their allowed ranges."}, status=400)
    except web.HTTPException as error:
        response = web.json_response({"error": "The dashboard request could not be completed."}, status=error.status)
    except Exception:
        # No request bodies, exception text, prompts, or replies enter logs.
        response = web.json_response({"error": "The local dashboard could not complete that request."}, status=500)
    response.headers.update(HEADERS)
    return response


class Dashboard:
    def __init__(self, client):
        self.client = client
        self.runner: web.AppRunner | None = None
        self.warm_task: asyncio.Task | None = None
        self.model_cache: dict = {}
        self.model_checked = -100.0
        self.model_lock = asyncio.Lock()
        self.library_cache: dict = {}
        self.library_checked = -100.0
        self.library_count = -1

    @property
    def responder(self):
        return self.client.responder

    def app(self) -> web.Application:
        app = web.Application(middlewares=[private_boundary], client_max_size=65_536)
        app.router.add_get("/", self.index)
        app.router.add_get("/app.js", self.javascript)
        app.router.add_get("/style.css", self.stylesheet)
        app.router.add_get("/api/status", self.status)
        app.router.add_get("/api/settings", self.settings)
        app.router.add_put("/api/settings", self.save_settings)
        app.router.add_get("/api/persona", self.persona)
        app.router.add_put("/api/persona", self.save_persona)
        app.router.add_post("/api/pause", self.pause)
        app.router.add_post("/api/forget", self.forget)
        app.router.add_post("/api/warm", self.warm)
        app.router.add_post("/api/preview", self.preview)
        app.router.add_get("/api/library", self.library)
        app.router.add_get("/api/examples", self.examples)
        return app

    async def start(self, port: int) -> None:
        self.runner = web.AppRunner(self.app(), access_log=None)
        await self.runner.setup()
        try:
            # Only the Docker host's 127.0.0.1 mapping is published by Compose.
            await web.TCPSite(self.runner, "0.0.0.0", port).start()
        except Exception:
            await self.runner.cleanup()
            self.runner = None
            raise

    async def close(self) -> None:
        if self.warm_task is not None:
            self.warm_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.warm_task
        if self.runner is not None:
            await self.runner.cleanup()

    async def index(self, request):
        return web.Response(text=(STATIC / "index.html").read_text(), content_type="text/html")

    async def javascript(self, request):
        return web.Response(text=(STATIC / "app.js").read_text(), content_type="application/javascript")

    async def stylesheet(self, request):
        return web.Response(text=(STATIC / "style.css").read_text(), content_type="text/css")

    async def model_status(self, force: bool = False) -> dict:
        if force or time.monotonic() - self.model_checked > 5:
            async with self.model_lock:
                if force or time.monotonic() - self.model_checked > 5:
                    try:
                        self.model_cache = await self.responder.model.status()
                    except ModelError:
                        self.model_cache = {"available": False, "loaded": False, "name": self.client.control.tuning.model, "installed": []}
                    self.model_checked = time.monotonic()
        return {**self.model_cache, "warming": bool(self.warm_task and not self.warm_task.done())}

    async def status(self, request):
        ready = self.client.is_ready()
        latency = self.client.latency
        paused = sum(self.client.store.is_paused(identifier) for identifier in self.client.settings.guild_ids)
        return web.json_response({
            "bot": {"connected": ready, "gateway_ms": round(latency * 1000) if ready and 0 <= latency < 100 else None, "servers": len(self.client.settings.guild_ids), "paused_servers": paused},
            "model": await self.model_status(),
            "metrics": self.responder.metrics.snapshot(),
            "examples": self.client.store.count(),
            "sessions": len(self.responder.memory.sessions),
            "revision": self.client.control.revision,
            "tuning": self.client.control.document()["tuning"],
        })

    async def settings(self, request):
        document = self.client.control.document()
        return web.json_response({"revision": document["revision"], "tuning": document["tuning"], "defaults": asdict(Tuning(self.client.control.tuning.model))})

    async def save_settings(self, request):
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"revision", "tuning"}:
            raise ValueError
        if not isinstance(body["tuning"], dict):
            raise ValueError
        if body["tuning"].get("model") != self.client.control.tuning.model:
            state = await self.model_status(force=True)
            if body["tuning"].get("model") not in state["installed"]:
                raise ValueError
        self.client.control.update_tuning(body["revision"], body["tuning"])
        self.responder.apply_tuning()
        self.model_checked = -100
        return await self.settings(request)

    async def persona(self, request):
        document = self.client.control.document()
        return web.json_response({"revision": document["revision"], "persona": document["persona"]})

    async def save_persona(self, request):
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"revision", "persona"}:
            raise ValueError
        self.client.control.update_persona(body["revision"], body["persona"])
        for identifier in self.client.settings.guild_ids:
            self.responder.clear_guild(identifier)
        return await self.persona(request)

    async def pause(self, request):
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"paused"} or type(body["paused"]) is not bool:
            raise ValueError
        for identifier in self.client.settings.guild_ids:
            self.responder.pause(identifier, body["paused"])
        return web.json_response({"paused": body["paused"]})

    async def forget(self, request):
        if await request.json() != {}:
            raise ValueError
        for identifier in self.client.settings.guild_ids:
            self.responder.clear_guild(identifier)
        return web.json_response({"cleared": True})

    def request_warm(self) -> bool:
        if (self.warm_task and not self.warm_task.done()) or self.responder.gate.pending:
            return False
        self.warm_task = asyncio.create_task(self._warm_model())
        return True

    async def _warm_model(self) -> None:
        identifier = self.responder.metrics.begin("warm")
        outcome, timing = "error", None
        try:
            async with self.responder.slot:
                self.responder.metrics.active(identifier)
                timing = await self.responder.model.warm()
                outcome = "ready"
        except ModelError:
            pass
        except Exception:
            # Warm-up has no user prompt. Still keep unexpected exception text private.
            pass
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            self.responder.metrics.finish(identifier, outcome, timing)
            self.model_checked = -100

    async def warm(self, request):
        if await request.json() != {}:
            raise ValueError
        if not self.request_warm():
            return web.json_response({"error": "A reply or model load is already in progress."}, status=409)
        return web.json_response({"warming": True}, status=202)

    async def preview(self, request):
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"prompt"} or not isinstance(body["prompt"], str):
            raise ValueError
        answer = await asyncio.wait_for(self.responder.reply(Request(0, 0, 0, body["prompt"], source="preview")), timeout=240)
        return web.json_response({"reply": answer})

    async def library(self, request):
        count = self.client.store.count()
        if request.query.get("refresh") == "1" or count != self.library_count or time.monotonic() - self.library_checked > 60:
            texts = self.client.store.library_texts()
            self.library_cache = await asyncio.to_thread(self.client.store.summarize_texts, texts)
            self.library_checked, self.library_count = time.monotonic(), count
        return web.json_response({**self.library_cache, "total": count})

    async def examples(self, request):
        query = request.query.get("q", "")
        offset = int(request.query.get("offset", "0"))
        if len(query) > 150 or not 0 <= offset <= 100000:
            raise ValueError
        return web.json_response(self.client.store.page(query=query, offset=offset))
