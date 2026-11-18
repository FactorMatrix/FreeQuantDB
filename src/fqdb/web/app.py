from __future__ import annotations

import asyncio
import json
from pathlib import Path
import secrets
import threading

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..layout import runtime_path
from ..service.lifetime import Lifetime
from ..storage.query import get_price
from .dashboard import Dashboard, UnknownView

STATIC = Path(__file__).parent / "static"


def create_app(owner, token):
    app = FastAPI(title="FreeQuantDB", docs_url=None, redoc_url=None, openapi_url=None)
    if not hasattr(owner, "lifetime"):
        owner.lifetime = Lifetime(owner)
    if not hasattr(owner, "lifecycle_requests"):
        owner.lifecycle_requests = 0
    if not hasattr(owner, "lifecycle_lock"):
        owner.lifecycle_lock = threading.RLock()
        owner.lifecycle_stopping = False
    dashboard = Dashboard(owner)

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        host = request.url.hostname
        if host not in ("127.0.0.1", "localhost"):
            return JSONResponse({"error": "Local host required"}, 403)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"error": "Cross-origin control is disabled"}, 403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"error": "Cross-site access is disabled"}, 403)
        if request.url.path.startswith("/api/"):
            supplied = request.headers.get("authorization", "").removeprefix(
                "Bearer "
            ) or request.cookies.get("fqdb", "")
            if not secrets.compare_digest(supplied, token):
                return JSONResponse({"error": "Service token required"}, 401)
        if request.url.path == "/" or request.url.path.startswith(
            ("/static/", "/api/console/", "/api/query")
        ):
            if not owner.lifetime.has_gateway():
                return JSONResponse({"error": "管理界面未开启，请运行 fqdb gateway"}, 403)
        with owner.lifecycle_lock:
            if owner.lifecycle_stopping:
                return JSONResponse({"error": "worker_stopping", "retryable": True}, 503)
            owner.lifecycle_requests += 1
        try:
            response = await call_next(request)
        finally:
            with owner.lifecycle_lock:
                owner.lifecycle_requests -= 1
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index():
        response = FileResponse(STATIC / "index.html")
        response.set_cookie("fqdb", token, httponly=True, samesite="strict")
        return response

    @app.get("/api/health")
    def health():
        return {"data_dir": str(owner.config.root), "status": "running"}

    @app.get("/api/status")
    def status():
        return owner.status()

    @app.get("/api/gateway/{session_id}")
    async def gateway_connection(session_id: str):
        connection_id = owner.lifetime.attach(session_id)
        if connection_id is None:
            return JSONResponse({"error": "Gateway session has ended"}, 410)

        async def events():
            try:
                while not getattr(owner, "lifecycle_exiting", lambda: False)():
                    session = owner.lifetime.heartbeat(session_id, browser=True)
                    yield "data: " + json.dumps(session) + "\n\n"
                    if not session["active"]:
                        break
                    await asyncio.sleep(2)
            finally:
                owner.lifetime.detach(session_id, connection_id)

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/api/console/{view}")
    def console_view(view: str, request: Request):
        try:
            return dashboard.view(view, dict(request.query_params))
        except UnknownView as exc:
            return JSONResponse({"error": str(exc)}, 404)
        except (ValueError, TypeError, OSError) as exc:
            return JSONResponse({"error": str(exc), "type": type(exc).__name__}, 400)

    @app.post("/api/console/plan")
    def console_plan(args: dict):
        try:
            return dashboard.operations.plan(**args)
        except (ValueError, TypeError, OSError) as exc:
            return JSONResponse({"error": str(exc), "type": type(exc).__name__}, 400)

    @app.get("/api/reports")
    def reports():
        return owner.store.reports()

    @app.get("/api/logs")
    def logs():
        path = runtime_path(owner.config.root, "logs") / "service.log"
        if not path.exists():
            return {"text": "Foreground service: logs are written to its terminal"}
        with path.open("rb") as f:
            f.seek(max(0, path.stat().st_size - 32000))
            return {"text": f.read().decode(errors="replace")}

    @app.post("/api/action/{action}")
    def action(action: str, args: dict):
        if action == "shutdown":
            owner.shutdown()
            return {"status": "stopping"}
        try:
            if action == "gateway_open":
                return owner.lifetime.open()
            if action == "gateway_heartbeat":
                return owner.lifetime.heartbeat(args["session_id"], bool(args.get("browser", False)))
            if action == "gateway_close":
                return owner.lifetime.close(args["session_id"], args.get("grace", 0))
            return owner.dispatch(action, args)
        except Exception as exc:
            return JSONResponse({"error": str(exc), "type": type(exc).__name__}, 400)

    @app.post("/api/query")
    def query(args: dict):
        try:
            export = args.pop("export", False)
            limit = min(max(int(args.pop("limit", 100)), 1), 1000)
            offset = max(int(args.pop("offset", 0)), 0)
            dashboard.datasets.validate_query(args)
            frame = get_price(owner.store, **args, limit=limit, offset=offset)
            if export:
                return Response(
                    frame.to_csv(index=False),
                    media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="prices.csv"'},
                )
            return {
                "records": json.loads(frame.to_json(orient="records", date_format="iso")),
                "metadata": frame.attrs,
                "offset": offset,
                "limit": limit,
                "next_offset": offset + len(frame) if frame.attrs.get("page_has_more") else None,
            }
        except Exception as exc:
            return JSONResponse({"error": str(exc), "type": type(exc).__name__}, 400)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
