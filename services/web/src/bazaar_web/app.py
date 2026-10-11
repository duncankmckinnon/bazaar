"""Conference web service: submit a strategy, queue its run, and serve the live board."""

import csv
import hashlib
import hmac
import io
import json
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

import logfire
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from bazaar_web import stats, telemetry
from bazaar_web.board import BoardSource, build_board
from bazaar_web.bundle import BundleSource
from bazaar_web.house import catalog
from bazaar_web.settings import Settings
from bazaar_web.store import (
    LOCK_ERRORS,
    CapReached,
    Contact,
    Hidden,
    HouseRun,
    NameTaken,
    NotFound,
    Now,
    StillInFlight,
    Store,
)
from bazaar_web.tickers import TickerSource
from bazaar_web.worker import RunSubmission, Worker

STATIC = Path(__file__).parent / "static"
PLACEHOLDER = "<!doctype html><title>Bazaar</title><p>{} is coming soon.</p>"
FONT_NAME = re.compile(r"^[A-Za-z0-9_-]+\.woff2$")


# A conservative check, not RFC 5322 (no email-validator dependency): one "@", no whitespace, a
# local part, and a dotted domain with no empty label.
EMAIL = re.compile(r"^[^@\s]+@(?!\.)[^@\s]*[^@\s.]\.[^@\s]*(?<!\.)$")


def contact_text(value: str, *, limit: int, message: str) -> str:
    """Strip; a 422 with exactly `message` (no "Value error, " prefix) when empty or too long."""
    value = value.strip()
    if not 1 <= len(value) <= limit:
        raise PydanticCustomError("value_error", message)
    return value


class SubmissionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(pattern=r"^[a-z0-9-]{3,40}$")]
    handle: str | None = None
    instructions: str
    # Personal data, stored in the web DB only: see Store's email column.
    email: str
    company: str
    role: str

    def __repr_args__(self):  # keep contact details out of any repr, log or traceback
        hidden = {"email", "company", "role"}
        return [(k, "..." if k in hidden else v) for k, v in super().__repr_args__()]

    @field_validator("email")
    @classmethod
    def clean_email(cls, value: str) -> str:
        value = value.strip().lower()
        if len(value) > 254 or not EMAIL.match(value) or ".." in value.split("@")[-1]:
            raise PydanticCustomError("value_error", "Enter a valid email address")
        return value

    @field_validator("company")
    @classmethod
    def clean_company(cls, value: str) -> str:
        return contact_text(value, limit=100, message="Enter your company (up to 100 characters)")

    @field_validator("role")
    @classmethod
    def clean_role(cls, value: str) -> str:
        return contact_text(value, limit=100, message="Enter your role (up to 100 characters)")

    def contact(self) -> Contact:
        return Contact(email=self.email, company=self.company, role=self.role)

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


CONTACT_COLUMNS = ["name", "handle", "email", "company", "role", "created_at", "return_pct", "rank"]
# A spreadsheet runs a cell starting with these as a formula (CSV injection).
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def csv_cell(value: str | None) -> str:
    """A user-supplied value made safe for a spreadsheet: a leading quote defuses a formula."""
    text = value or ""
    return "'" + text if text.startswith(FORMULA_START) else text


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
    bundle_source: BundleSource | None = None,
) -> FastAPI:
    """Build the app. Nothing touches disk until the lifespan starts, nor the market until a
    request needs the trading window."""
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

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """FastAPI's 422 shape without the "input" echo (and "ctx"): for a missing field the
        input is the whole body, contact details included, and the form shows details as is."""
        detail = [{k: v for k, v in e.items() if k not in ("input", "ctx")} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(detail)})

    app.state.on_scored = on_scored
    tickers = TickerSource(settings.market_db)
    bundles = bundle_source or BundleSource(settings.market_url, settings.ticker_data_version)

    def board_payload(request: Request) -> dict[str, Any]:
        return build_board(
            request.app.state.board, request.app.state.store, settings.logfire_url, bundles.get()
        )

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
                contact=body.contact(),
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
                "lane": submission["lane"] or "human",
                "status": submission["status"],
                "day": submission["day"],
                "error": submission["error"],
                "position": store.position(submission_id),
                "return_pct": row["return_pct"] if scored or provisional else None,
                "rank": row["rank"] if scored else None,
                "provisional": provisional,
                "logfire_url": settings.logfire_url(submission["name"]),
            }

    @app.get("/api/tickers")
    def ticker_days() -> dict[str, Any]:
        with logfire.suppress_instrumentation():  # polled; see submission_status
            return tickers.payload(bundles.get())

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

    @app.post("/api/admin/submissions/{submission_id}/rerun", status_code=201)
    def rerun(submission_id: str, request: Request, token: AdminToken = None) -> dict[str, Any]:
        require_admin(token)
        store: Store = request.app.state.store
        carrier = logfire.propagate.get_context()  # the rerun's run joins this request's trace
        try:
            new_id = store.rerun(
                submission_id, trace_context=json.dumps(carrier) if carrier else None
            )
        except NotFound:
            raise HTTPException(404, "no such submission") from None
        except StillInFlight:
            raise HTTPException(409, "that submission is still queued or running") from None
        except Hidden:
            raise HTTPException(409, "that submission was already rerun or is hidden") from None
        except HouseRun:
            raise HTTPException(409, "house runs refill automatically") from None
        position = store.position(new_id)  # read before a worker can pick it up
        request.app.state.worker.enqueue(new_id)
        request.app.state.board.invalidate()
        return {"id": new_id, "status": "queued", "position": position}

    @app.get("/api/admin/contacts")
    def contacts(request: Request, token: AdminToken = None) -> Response:
        """CSV of the submitters who left contact details, to reach winners. Admin only and
        untraced: it is the one place personal data leaves the web DB."""
        require_admin(token)
        with logfire.suppress_instrumentation():
            store: Store = request.app.state.store
            rows = {r["id"]: r for r in board_payload(request)["rows"]}
            people = store.contacts()
            stamp = store.now().strftime("%Y%m%dT%H%M%SZ")
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(CONTACT_COLUMNS)
        for person in people:
            row = rows.get(person["id"])
            scored = row is not None and row["status"] == "scored"
            writer.writerow(
                [
                    *(csv_cell(person[k]) for k in ("name", "handle", "email", "company", "role")),
                    person["created_at"],
                    "" if not scored or row["return_pct"] is None else row["return_pct"],
                    row["rank"] if scored else "",
                ]
            )
        return Response(
            out.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="bazaar-contacts-{stamp}.csv"',
                "Cache-Control": "no-store",  # personal data: no browser or proxy keeps a copy
            },
        )

    @app.get("/api/admin/whoami")
    def whoami(request: Request, token: AdminToken = None) -> dict[str, str | None]:
        """Temporary: shows which address the per-IP cap sees behind the deploy's proxy."""
        require_admin(token)
        return {
            "x_forwarded_for": request.headers.get("x-forwarded-for"),
            "peer": request.client.host if request.client else None,
            "cap_ip": client_ip(request),
        }

    @app.get("/api/admin/stats")
    def admin_stats(
        request: Request, token: AdminToken = None, since: str | None = None
    ) -> dict[str, Any]:
        """Throughput since ?since= (ISO; default the last 6 hours). Untraced, like the polls."""
        require_admin(token)
        store: Store = request.app.state.store
        try:
            # An unencoded "+00:00" offset arrives as " 00:00".
            start = (
                stats.parse_time(since.replace(" ", "+"))
                if since
                else store.now() - timedelta(hours=6)
            )
        except ValueError:
            raise HTTPException(422, "since must be an ISO 8601 timestamp") from None
        with logfire.suppress_instrumentation():
            rows = store.timings()
            counts = store.lane_counts()
            house = house_status(store, request.app.state.worker)
        lanes = {
            "human": {
                "slots": settings.max_concurrent,
                "running": counts["human"].get("running", 0),
                "queued": counts["human"].get("queued", 0),
            },
            "house": house,
        }
        return stats.summarize(
            rows,
            since=start,
            max_concurrent=settings.max_concurrent,
            lock_errors=LOCK_ERRORS.count,
            lanes=lanes,
        )

    def house_status(store: Store, worker: Worker) -> dict[str, Any]:
        state = store.house_state()
        until = worker.house_backoff_until
        cap = settings.house_max_per_hour
        return {
            "enabled": settings.house_enabled and settings.house_concurrent > 0,
            "paused": state["paused"],
            "slots": settings.house_concurrent if settings.house_enabled else 0,
            "running": state["running"],
            "cursor": state["cursor"],
            "cycle": state["cycle"],
            "catalog_size": len(catalog()),
            "total_started": state["total_started"],
            "started_last_hour": state["last_hour"],
            "max_per_hour": cap,
            "rate_limited": cap is not None and state["last_hour"] >= cap,
            "consecutive_failures": worker.house_failures,
            "backoff_until": until.isoformat() if until and until > store.now() else None,
        }

    @app.get("/api/admin/house")
    def house(request: Request, token: AdminToken = None) -> dict[str, Any]:
        require_admin(token)
        return house_status(request.app.state.store, request.app.state.worker)

    @app.post("/api/admin/house/pause")
    def house_pause(request: Request, token: AdminToken = None) -> dict[str, Any]:
        """Stop starting house runs (running ones finish). Kept across restarts."""
        require_admin(token)
        request.app.state.store.set_house_paused(True)
        return house_status(request.app.state.store, request.app.state.worker)

    @app.post("/api/admin/house/resume")
    def house_resume(request: Request, token: AdminToken = None) -> dict[str, Any]:
        require_admin(token)
        request.app.state.store.set_house_paused(False)
        request.app.state.worker.wake_house()
        return house_status(request.app.state.store, request.app.state.worker)

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
