"""Conference web service: submit a strategy, queue its run, and serve the live board."""

import hashlib
import hmac
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from bazaar_web.board import BoardSource, build_board
from bazaar_web.settings import Settings
from bazaar_web.store import CapReached, NameTaken, Now, Store
from bazaar_web.worker import RunSubmission, Worker

STATIC = Path(__file__).parent / "static"
PLACEHOLDER = "<!doctype html><title>Bazaar</title><p>{} is coming soon.</p>"


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
        settings.runs_dir.mkdir(parents=True, exist_ok=True)
        store = Store(settings.web_db, now) if now else Store(settings.web_db)
        board = BoardSource(settings.runs_dir)
        worker = Worker(
            store, settings, board, run_submission, on_scored=lambda: app.state.on_scored
        )
        app.state.store, app.state.board, app.state.worker = store, board, worker
        worker.start()
        try:
            yield
        finally:
            await worker.stop()

    app = FastAPI(title="Bazaar web", lifespan=lifespan)
    app.state.on_scored = on_scored

    def board_payload(request: Request) -> dict[str, Any]:
        return build_board(request.app.state.board, request.app.state.store)

    @app.get("/", include_in_schema=False)
    def board_page() -> Response:
        return static_page("board.html", "The live board")

    @app.get("/submit", include_in_schema=False)
    def submit_page() -> Response:
        return static_page("submit.html", "The submission form")

    @app.post("/api/submissions", status_code=201)
    def submit(body: SubmissionIn, request: Request) -> dict[str, Any]:
        store: Store = request.app.state.store
        ip_hash = hashlib.sha256(client_ip(request).encode()).hexdigest()
        try:
            submission_id = store.create(
                name=body.name,
                handle=body.handle,
                instructions=body.instructions,
                ip_hash=ip_hash,
                max_queue=settings.max_queue,
                max_per_day=settings.max_per_day,
                max_per_ip_hour=settings.max_per_ip_hour,
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
        store: Store = request.app.state.store
        submission = store.get(submission_id)
        if submission is None or submission["hidden"]:
            raise HTTPException(404, "no such submission")
        row = next((r for r in board_payload(request)["rows"] if r["id"] == submission_id), None)
        scored = row is not None and row["status"] == "scored"
        return {
            "id": submission_id,
            "name": submission["name"],
            "status": submission["status"],
            "day": submission["day"],
            "error": submission["error"],
            "position": store.position(submission_id),
            "return_pct": row["return_pct"] if scored else None,
            "rank": row["rank"] if scored else None,
        }

    @app.get("/api/board")
    def board(request: Request) -> dict[str, Any]:
        return board_payload(request)

    @app.post("/api/admin/submissions/{submission_id}/hide", status_code=204)
    def hide(
        submission_id: str,
        request: Request,
        token: Annotated[str | None, Header(alias="X-Bazaar-Admin-Token")] = None,
    ) -> Response:
        if settings.admin_token is None:
            raise HTTPException(404, "not found")
        if token is None or not hmac.compare_digest(token.encode(), settings.admin_token.encode()):
            raise HTTPException(403, "forbidden")
        if not request.app.state.store.hide(submission_id):
            raise HTTPException(404, "no such submission")
        request.app.state.board.invalidate()
        return Response(status_code=204)

    return app


app = create_app()
