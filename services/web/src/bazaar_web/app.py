"""Conference web service: submit a strategy, queue its run, and serve the live board."""

import hashlib
import hmac
import json
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import logfire
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from bazaar_web import telemetry
from bazaar_web.board import BoardSource, build_board
from bazaar_web.settings import Settings
from bazaar_web.store import CapReached, NameTaken, Now, Store
from bazaar_web.worker import RunSubmission, Worker

STATIC = Path(__file__).parent / "static"
PLACEHOLDER = "<!doctype html><title>Bazaar</title><p>{} is coming soon.</p>"
FONT_NAME = re.compile(r"^[A-Za-z0-9_-]+\.woff2$")


class SubmissionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(pattern=r"^[a-z0-9-]{3,40}$")]
    handle: str | None = None
    instructions: str

    @field_validator("handle")
    @classmethod
    def clean_handle(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if len(value) > 40:
            raise ValueError("handle must be at most 40 characters")
        return value or None

    @field_validator("instructions")
    @classmethod
    def clean_instructions(cls, value: str) -> str:
        value = value.strip()
        if not 20 <= len(value) <= 4000:
            raise ValueError("instructions must be 20 to 4000 characters")
        return value


def client_ip(request: Request) -> str:
    # The front proxy appends the real client address, so trust only the rightmost entry.
    forwarded = request.headers.get("x-forwarded-for", "")
    entries = [part.strip() for part in forwarded.split(",") if part.strip()]
    if entries:
        return entries[-1]
    return request.client.host if request.client else "unknown"


def static_page(name: str, label: str) -> Response:
    path = STATIC / name
    if path.is_file():
        return FileResponse(path, media_type="text/html")
    return HTMLResponse(PLACEHOLDER.format(label))


def create_app(
    settings: Settings | None = None,
    run_submission: RunSubmission | None = None,
    on_scored: Callable[[str, Path], None] | None = None,
    now: Now | None = None,
) -> FastAPI:
    """Build the app. Nothing touches disk until the lifespan starts."""
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        telemetry.configure()  # before the worker starts; never at import
        settings.runs_dir.mkdir(parents=True, exist_ok=True)
        with logfire.suppress_instrumentation():  # schema and migration queries at boot
            store = Store(settings.web_db, now) if now else Store(settings.web_db)
        board = BoardSource(settings.runs_dir)
        worker = Worker(
            store, settings, board, run_submission, on_scored=lambda: app.state.on_scored
        )
        app.state.store, app.state.board, app.state.worker = store, board, worker
        await worker.start()
        try:
            yield
        finally:
            await worker.stop()

    app = FastAPI(title="Bazaar web", lifespan=lifespan)
    app.state.on_scored = on_scored

    def board_payload(request: Request) -> dict[str, Any]:
        return build_board(request.app.state.board, request.app.state.store, settings.logfire_url)

    @app.get("/", include_in_schema=False)
    def board_page(request: Request) -> Response:
        page = STATIC / "board.html"
        if not page.is_file():
            return HTMLResponse(PLACEHOLDER.format("The live board"))
        base = settings.public_url or str(request.base_url)
        # A JS string literal inside <script>: JSON, with "</" broken so it can't end the tag.
        submit_url = json.dumps(base.rstrip("/") + "/submit").replace("</", "<\\/")
        html = page.read_text(encoding="utf-8").replace("__SUBMIT_URL__", submit_url, 1)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/fonts/{name}", include_in_schema=False)
    def font(name: str) -> Response:
        # The brand font is licensed and baked into the image, never committed.
        fonts = settings.fonts_dir
        if fonts is None or not FONT_NAME.fullmatch(name) or not (fonts / name).is_file():
            raise HTTPException(404, "not found")
        return FileResponse(fonts / name, media_type="font/woff2")

    @app.get("/submit", include_in_schema=False)
    def submit_page() -> Response:
        return static_page("submit.html", "The submission form")

    @app.post("/api/submissions", status_code=201)
    def submit(body: SubmissionIn, request: Request) -> dict[str, Any]:
        store: Store = request.app.state.store
        ip_hash = hashlib.sha256(client_ip(request).encode()).hexdigest()
        # The run joins this request's trace: the worker re-attaches this context.
        carrier = logfire.propagate.get_context()
        try:
            submission_id = store.create(
                name=body.name,
                handle=body.handle,
                instructions=body.instructions,
                ip_hash=ip_hash,
                max_queue=settings.max_queue,
                max_per_day=settings.max_per_day,
                max_per_ip_hour=settings.max_per_ip_hour,
                trace_context=json.dumps(carrier) if carrier else None,
            )
        except NameTaken:
            raise HTTPException(422, "that name is taken") from None
        except CapReached as cap:
            raise HTTPException(429, cap.detail) from None
        position = store.position(submission_id)  # read before a worker can pick it up
        request.app.state.worker.enqueue(submission_id)
        return {"id": submission_id, "status": "queued", "position": position}

    @app.get("/api/submissions/{submission_id}")
    def submission_status(submission_id: str, request: Request) -> dict[str, Any]:
        # Polled every few seconds: no request span (excluded URL) and no sqlite spans either,
        # which would otherwise become orphan root traces.
        with logfire.suppress_instrumentation():
            store: Store = request.app.state.store
            submission = store.get(submission_id)
            if submission is None or submission["hidden"]:
                raise HTTPException(404, "no such submission")
            row = next(
                (r for r in board_payload(request)["rows"] if r["id"] == submission_id), None
            )
            scored = row is not None and row["status"] == "scored"
            provisional = row is not None and row["status"] == "running" and row["provisional"]
            return {
                "id": submission_id,
                "name": submission["name"],
                "status": submission["status"],
                "day": submission["day"],
                "error": submission["error"],
                "position": store.position(submission_id),
                "return_pct": row["return_pct"] if scored or provisional else None,
                "rank": row["rank"] if scored else None,
                "provisional": provisional,
                "logfire_url": settings.logfire_url(submission["name"]),
            }

    @app.get("/api/board")
    def board(request: Request) -> dict[str, Any]:
        with logfire.suppress_instrumentation():  # polled; see submission_status
            return board_payload(request)

    def require_admin(token: str | None) -> None:
        if settings.admin_token is None:
            raise HTTPException(404, "not found")
        if token is None or not hmac.compare_digest(token.encode(), settings.admin_token.encode()):
            raise HTTPException(403, "forbidden")

    AdminToken = Annotated[str | None, Header(alias="X-Bazaar-Admin-Token")]

    @app.post("/api/admin/submissions/{submission_id}/hide", status_code=204)
    def hide(submission_id: str, request: Request, token: AdminToken = None) -> Response:
        require_admin(token)
        if not request.app.state.store.hide(submission_id):
            raise HTTPException(404, "no such submission")
        request.app.state.board.invalidate()
        return Response(status_code=204)

    @app.get("/api/admin/whoami")
    def whoami(request: Request, token: AdminToken = None) -> dict[str, str | None]:
        """Temporary: shows which address the per-IP cap sees behind the deploy's proxy."""
        require_admin(token)
        return {
            "x_forwarded_for": request.headers.get("x-forwarded-for"),
            "peer": request.client.host if request.client else None,
            "cap_ip": client_ip(request),
        }

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    logfire.instrument_fastapi(
        app,
        capture_headers=False,
        # The default mapper records endpoint arguments, including the admin token header.
        request_attributes_mapper=lambda request, attributes: None,
        excluded_urls=list(telemetry.EXCLUDED_URLS),
    )
    return app


app = create_app()
