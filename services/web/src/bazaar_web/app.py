"""Conference web service: submit a strategy, queue its run, and serve the live board."""

import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import string
import threading
import time
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
from pydantic import BaseModel, ConfigDict, field_validator
from pydantic_core import PydanticCustomError

from bazaar_web import stats, telemetry
from bazaar_web.board import (
    BoardSource,
    board_factory,
    board_rows,
    board_window,
    build_board,
)
from bazaar_web.browser import telemetry_module
from bazaar_web.bundle import BundleSource
from bazaar_web.dataset import ClientFactory
from bazaar_web.house import Strategist
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
from bazaar_web.worker import WAITING_FOR_BUNDLE, RunSubmission, Worker

STATIC = Path(__file__).parent / "static"
PLACEHOLDER = "<!doctype html><title>Bazaar</title><p>{} is coming soon.</p>"
FONT_NAME = re.compile(r"^[A-Za-z0-9_-]+\.woff2$")
# BAZAAR_QR_RANDOM's text: lowercase and digits, like a link, so the code's density is the same.
QR_DECOY_ALPHABET = string.ascii_lowercase + string.digits

log = logging.getLogger("bazaar_web.app")


# A conservative check, not RFC 5322 (no email-validator dependency): one "@", no whitespace, a
# local part, and a dotted domain with no empty label.
EMAIL = re.compile(r"^[^@\s]+@(?!\.)[^@\s]*[^@\s.]\.[^@\s]*(?<!\.)$")


def clean_email(value: str) -> str:
    """Stripped and lowercased; a 422 with the form's message when it isn't a valid address."""
    value = value.strip().lower()
    if len(value) > 254 or not EMAIL.match(value) or ".." in value.split("@")[-1]:
        raise PydanticCustomError("value_error", "Enter a valid email address")
    return value


# A strategy name as the form shows it ("you'll play as"); the server normalises it the same way.
NAME = re.compile(r"[a-z0-9-]{3,40}")
NAME_MESSAGE = "Use 3 to 40 letters, digits or hyphens"


def normalize_name(value: str) -> str:
    """Lowercase; each whitespace run becomes "-"; anything else outside [a-z0-9-] is dropped;
    runs of "-" collapse to one and are trimmed from both ends; cut to 40 characters."""
    name = re.sub(r"[^a-z0-9-]", "", re.sub(r"\s+", "-", value.lower()))
    return re.sub(r"-+", "-", name).strip("-")[:40].rstrip("-")


def contact_text(value: str, *, limit: int, message: str) -> str:
    """Strip; a 422 with exactly `message` (no "Value error, " prefix) when empty or too long."""
    value = value.strip()
    if not 1 <= len(value) <= limit:
        raise PydanticCustomError("value_error", message)
    return value


# The board tags rows with this handle "AI": only the operators' house scripts (with the admin
# token) and the house lane's own server-made rows may carry it.
RESERVED_HANDLE = "house"


def reserved_handle() -> HTTPException:
    """A new 422 per refusal: a shared exception would keep the last request's frames (and its
    parsed body, contact details included) alive until the next raise."""
    return HTTPException(
        422, [{"type": "value_error", "loc": ["body", "handle"], "msg": "That handle is reserved"}]
    )


class SubmissionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str  # any text up to 200 characters, normalised (normalize_name)
    handle: str | None = None
    instructions: str
    # Personal data, stored in the web DB only: see Store's email column. Company and role are
    # optional (v11): validated when given, NULL when left out.
    email: str
    company: str | None = None
    role: str | None = None

    def __repr_args__(self):  # keep contact details out of any repr, log or traceback
        hidden = {"email", "company", "role"}
        return [(k, "..." if k in hidden else v) for k, v in super().__repr_args__()]

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        name = normalize_name(value) if len(value) <= 200 else ""
        if not NAME.fullmatch(name):
            raise PydanticCustomError("value_error", NAME_MESSAGE)
        return name

    @field_validator("email")
    @classmethod
    def clean_email(cls, value: str) -> str:
        return clean_email(value)

    @field_validator("company")
    @classmethod
    def clean_company(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return contact_text(value, limit=100, message="Enter your company (up to 100 characters)")

    @field_validator("role")
    @classmethod
    def clean_role(cls, value: str | None) -> str | None:
        if value is None:
            return None
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


class LeadIn(BaseModel):
    """The form's first step: an email, kept even if its owner never submits a strategy."""

    model_config = ConfigDict(extra="forbid")

    email: str  # personal data, as on SubmissionIn

    def __repr_args__(self):
        return [("email", "...")]

    @field_validator("email")
    @classmethod
    def clean_email(cls, value: str) -> str:
        return clean_email(value)


CONTACT_COLUMNS = [
    "name", "handle", "email", "company", "role", "created_at", "return_pct", "rank", "source",
]  # fmt: skip
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


SLOW_BOARD_S = 2.0  # a /api/board slower than this logs its two phases (it is not traced)


class BoardCache:
    """Stale-while-revalidate over the /api/board payload: a build parses run dirs and queries
    the store (about 97 s cold after a deploy, up to 8 s warm), so requests rarely wait on one.

    At most one build runs at a time, started at boot, when the payload is older than ttl, and
    on invalidate(). invalidate() (any committed store write, and BoardSource.invalidate) bumps
    a generation: a request whose payload predates it waits up to fresh_wait for the build that
    covers it, so a state change shows on the next poll, then serves the previous payload.
    TTL expiry never waits. Each good payload is persisted atomically next to the web database
    and loaded at boot, marked stale until this process's first build. A failed build is
    logged and never cached; the next trigger retries.
    """

    def __init__(
        self,
        build: Callable[[], dict[str, Any]],
        path: Path,
        *,
        ttl: float = 2.0,
        fresh_wait: float = 1.0,
    ) -> None:
        self.build, self.path, self.ttl, self.fresh_wait = build, path, ttl, fresh_wait
        self._cond = threading.Condition()
        self._payload: dict[str, Any] | None = None
        self._built_at: float | None = None  # monotonic time of this process's last good build
        self._gen = 0  # bumped by invalidate()
        self._built_gen = -1  # the generation the payload covers
        self._failed_gen = -1  # the generation of the last failed build
        self._building: int | None = None  # the generation the running build covers
        self._thread: threading.Thread | None = None
        self._stopped = False
        try:
            self._payload = {**json.loads(path.read_text()), "stale": True}
            self._built_gen = 0  # older than the TTL, but no state change since boot
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.warning("persisted board unreadable (%s)", type(exc).__name__)

    def start(self) -> None:
        with self._cond:
            self._kick()

    def stop(self) -> None:
        """No build starts after this; waits (up to 5 s) for the one running."""
        with self._cond:
            self._stopped = True
            thread = self._thread
        if thread is not None:
            thread.join(timeout=5)

    def invalidate(self) -> None:
        with self._cond:
            self._gen += 1
            self._kick()

    def get(self) -> dict[str, Any] | None:
        with self._cond:
            gen = self._gen
            expired = self._built_at is None or time.monotonic() - self._built_at >= self.ttl
            if expired or self._built_gen < gen:
                self._kick()
            covered = lambda: self._built_gen >= gen or self._failed_gen >= gen
            if self._payload is None:
                # A first boot with nothing persisted: nothing to serve, so wait for the build
                # (never starting a second). None if it failed.
                self._cond.wait_for(covered)
            elif self._built_gen < gen:
                self._cond.wait_for(covered, timeout=self.fresh_wait)
            return self._payload

    def settled(self, timeout: float = 10.0) -> bool:
        """Wait until the payload covers every invalidation so far and no build is running."""
        with self._cond:
            return self._cond.wait_for(
                lambda: self._thread is None and self._built_gen >= self._gen, timeout=timeout
            )

    def _kick(self) -> None:
        """Start a build unless one is running; it covers the current generation. Under _cond."""
        if self._thread is None and not self._stopped:
            self._building = gen = self._gen
            self._thread = threading.Thread(
                target=self._rebuild, args=(gen,), name="board-rebuild", daemon=True
            )
            self._thread.start()

    def _rebuild(self, gen: int) -> None:
        payload = None
        try:
            with logfire.suppress_instrumentation():  # like the polled route
                payload = self.build()
        except Exception as exc:  # noqa: BLE001 - keep serving the last good payload
            log.warning("board rebuild failed (%s)", type(exc).__name__)
        if payload is not None:
            self._persist(payload)
        with self._cond:
            if payload is not None:
                self._payload, self._built_at, self._built_gen = payload, time.monotonic(), gen
            else:
                self._failed_gen = gen
            self._thread = self._building = None
            if payload is not None and self._gen > gen:  # invalidated while this build ran
                self._kick()
            self._cond.notify_all()

    def _persist(self, payload: dict[str, Any]) -> None:
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(jsonable_encoder(payload)))
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("board not persisted (%s)", type(exc).__name__)
            tmp.unlink(missing_ok=True)


def create_app(
    settings: Settings | None = None,
    run_submission: RunSubmission | None = None,
    on_scored: Callable[[str, Path], None] | None = None,
    now: Now | None = None,
    on_saved: Callable[[], None] | None = None,
    bundle_source: BundleSource | None = None,
    strategist: Strategist | None = None,
    dataset_client: ClientFactory | None = None,
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
            store,
            settings,
            board,
            run_submission,
            on_scored=lambda: app.state.on_scored,
            bundles=bundles,
            strategist=strategist,
            dataset_client=dataset_client,
        )
        app.state.store, app.state.board, app.state.worker = store, board, worker
        cache = app.state.board_cache = BoardCache(
            lambda: board_payload(app.state),
            settings.web_db.parent / "board-payload.json",
            fresh_wait=settings.board_fresh_wait_ms / 1000,
        )
        board.on_invalidate = store.on_change = cache.invalidate  # any change: the next poll
        cache.start()
        await worker.start()
        try:
            yield
        finally:
            await worker.stop()
            cache.stop()

    app = FastAPI(title="Bazaar web", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """FastAPI's 422 shape without the "input" echo (and "ctx"): for a missing field the
        input is the whole body, contact details included, and the form shows details as is."""
        detail = [{k: v for k, v in e.items() if k not in ("input", "ctx")} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(detail)})

    app.state.on_scored = on_scored
    # Called after a new lead or submission has committed (modal_app.py: a debounced volume
    # commit). It must return at once; a failure is logged by class and never fails the request.
    app.state.on_saved = on_saved

    def saved(request: Request) -> None:
        hook = request.app.state.on_saved
        if hook is None:
            return
        try:
            hook()
        except Exception as exc:  # noqa: BLE001 - the row is already committed
            log.warning("on_saved hook failed (%s)", type(exc).__name__)

    tickers = TickerSource(settings.market_db)
    bundles = bundle_source or BundleSource(settings.market_url, settings.data_bundle)

    def all_rows(request: Request) -> list[dict[str, Any]]:
        """Every row, ranked over all of them (the board payload sends only the top rows)."""
        return board_rows(request.app.state.board, request.app.state.store, settings.logfire_url)[0]

    def board_payload(state: Any) -> dict[str, Any]:
        return build_board(
            state.board,
            state.store,
            settings.logfire_url,
            bundles.get(),
            house_enabled=settings.house_enabled,
        )

    @app.get("/", include_in_schema=False)
    def board_page(request: Request) -> Response:
        page = STATIC / "board.html"
        if not page.is_file():
            return HTMLResponse(PLACEHOLDER.format("The live board"))
        base = settings.public_url or str(request.base_url)
        qr_text = base.rstrip("/") + "/submit"
        if settings.qr_random:
            # Random text of the link's length: the same size of code, but it leads nowhere.
            qr_text = "".join(secrets.choice(QR_DECOY_ALPHABET) for _ in qr_text)
        # A JS string literal inside <script>: JSON, with "</" broken so it can't end the tag.
        submit_url = json.dumps(qr_text).replace("</", "<\\/")
        html = page.read_text(encoding="utf-8").replace("__SUBMIT_URL__", submit_url, 1)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/fonts/{name}", include_in_schema=False)
    def font(name: str) -> Response:
        # The brand font is licensed and baked into the image, never committed.
        fonts = settings.fonts_dir
        if fonts is None or not FONT_NAME.fullmatch(name) or not (fonts / name).is_file():
            raise HTTPException(404, "not found")
        return FileResponse(fonts / name, media_type="font/woff2")

    @app.get("/browser-telemetry.js", include_in_schema=False)
    def browser_telemetry(request: Request) -> Response:
        """The page's Logfire setup (bazaar_web.browser); a bare comment when it is off. Never
        cached: a token change or removal takes effect on the next page load."""
        return Response(
            telemetry_module(settings.logfire_browser_token, request.query_params.getlist("page")),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/submit", include_in_schema=False)
    def submit_page() -> Response:
        return static_page("submit.html", "The submission form")

    def is_admin(token: str | None) -> bool:
        """The admin check every admin route uses: a constant-time compare with the token."""
        return (
            settings.admin_token is not None
            and token is not None
            and hmac.compare_digest(token.encode(), settings.admin_token.encode())
        )

    def require_admin(token: str | None) -> None:
        if settings.admin_token is None:
            raise HTTPException(404, "not found")
        if not is_admin(token):
            raise HTTPException(403, "forbidden")

    AdminToken = Annotated[str | None, Header(alias="X-Bazaar-Admin-Token")]

    @app.post("/api/submissions", status_code=201)
    def submit(body: SubmissionIn, request: Request, token: AdminToken = None) -> dict[str, Any]:
        # The same 422 whether the token is missing, wrong or unset: no token oracle.
        handle = body.handle
        if handle is not None and handle.lower() == RESERVED_HANDLE:
            if not is_admin(token):
                raise reserved_handle()
            handle = RESERVED_HANDLE  # stored exactly, so the board's AI tag matches
        store: Store = request.app.state.store
        ip_hash = hashlib.sha256(client_ip(request).encode()).hexdigest()
        # The run joins this request's trace: the worker re-attaches this context.
        carrier = logfire.propagate.get_context()
        try:
            submission_id = store.create(
                name=body.name,
                handle=handle,
                instructions=body.instructions,
                ip_hash=ip_hash,
                max_queue=settings.max_queue,
                max_per_day=settings.max_per_day,
                max_per_ip_hour=settings.max_per_ip_hour,
                trace_context=json.dumps(carrier) if carrier else None,
                contact=body.contact(),
            )
        except NameTaken:  # 20 random suffixes all taken: not expected to happen
            raise HTTPException(422, "that name is taken") from None
        except CapReached as cap:
            raise HTTPException(429, cap.detail) from None
        # Committed: Store.create returns after its transaction. A taken name got a random
        # suffix there, so the final name is the stored one.
        saved(request)
        name = store.get(submission_id)["name"]
        position = store.position(submission_id)  # read before a worker can pick it up
        request.app.state.worker.enqueue(submission_id)
        return {
            "id": submission_id,
            "status": "queued",
            "position": position,
            "name": name,
            "renamed": name != body.name,
        }

    @app.post("/api/leads", status_code=204)
    def lead(body: LeadIn, request: Request) -> Response:
        """Keep an email from the form's first step. Personal data: untraced (EXCLUDED_URLS and
        suppressed here, so the sqlite spans go too) and never logged. 204 even for an email
        already kept, so the reply says nothing about who signed up."""
        no_store = {"Cache-Control": "no-store"}
        with logfire.suppress_instrumentation():
            ip_hash = hashlib.sha256(client_ip(request).encode()).hexdigest()
            try:
                request.app.state.store.add_lead(
                    email=body.email,
                    ip_hash=ip_hash,
                    max_per_ip_hour=settings.max_leads_per_ip_hour,
                )
            except CapReached as cap:
                raise HTTPException(429, cap.detail, headers=no_store) from None
        saved(request)  # add_lead returns after its transaction has committed
        return Response(status_code=204, headers=no_store)

    @app.get("/api/submissions/{submission_id}")
    def submission_status(submission_id: str, request: Request) -> dict[str, Any]:
        # Polled every few seconds: no request span (excluded URL) and no sqlite spans either,
        # which would otherwise become orphan root traces.
        with logfire.suppress_instrumentation():
            store: Store = request.app.state.store
            submission = store.get(submission_id)
            if submission is None or submission["hidden"]:
                raise HTTPException(404, "no such submission")
            row = next((r for r in all_rows(request) if r["id"] == submission_id), None)
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
        start = time.perf_counter()
        payload = request.app.state.board_cache.get()
        cached = time.perf_counter()
        if payload is None:  # a first boot whose first build failed; the page retries
            raise HTTPException(503, "board not ready")
        store = request.app.state.store
        with logfire.suppress_instrumentation():  # polled; see submission_status
            # Cheap parts stay live on a cached board: the factory line, the trading window (the
            # bundle source caches it), and moderation: the rows' hidden ids and the events
            # (recent_events leaves hidden rows out), so a stale or persisted payload never
            # names a hidden row.
            factory = board_factory(store, settings.house_enabled)
            cash = (payload.get("window") or {}).get("starting_cash", 10000.0)
            window = board_window(bundles.get(), cash)
            hidden = store.hidden_ids()
            events = store.recent_events(20)
        rows = [r for r in payload.get("rows", []) if r.get("id") not in hidden]
        done = time.perf_counter()
        if done - start > SLOW_BOARD_S:
            log.warning(
                "slow /api/board: cache %.2f s, live parts %.2f s", cached - start, done - cached
            )
        return {**payload, "window": window, "rows": rows, "events": events, "factory": factory}

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
        """CSV of the submitters who left contact details, to reach winners, then the emails
        left at the form's first step that never submitted (source "lead": email and time only).
        A submission's created_at is its email's earliest (its lead's, when that came first).
        Admin only and untraced: it is the one place personal data leaves the web DB."""
        require_admin(token)
        with logfire.suppress_instrumentation():
            store: Store = request.app.state.store
            rows = {r["id"]: r for r in all_rows(request)}
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
                    person["source"],
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
        factory = store.factory_state()
        until, retry = worker.house_backoff_until, worker.propose_retry_at
        cap = settings.house_max_per_hour
        return {
            "enabled": settings.house_enabled and settings.house_concurrent > 0,
            "paused": state["paused"],
            "slots": settings.house_concurrent if settings.house_enabled else 0,
            "running": state["running"],
            **factory,
            "last_propose_at": at.isoformat() if (at := worker.last_propose_at) else None,
            "last_error_class": worker.last_propose_error,
            "strategist_retry_at": retry.isoformat() if retry and retry > store.now() else None,
            "waiting_for_bundle": worker.house_waiting == WAITING_FOR_BUNDLE,
            "state": worker.house_waiting,
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
