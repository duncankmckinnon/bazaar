import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

log = logging.getLogger("bazaar_web.settings")
DEFAULT_BUNDLE = "demo-bundle-v1"


def data_bundle(env: "os._Environ[str] | dict[str, str]") -> str:
    """BAZAAR_DATA_BUNDLE, the one bundle id (the runner reads the same). The old
    BAZAAR_TICKER_DATA_VERSION still works, with a warning, until deploys move over."""
    if env.get("BAZAAR_DATA_BUNDLE"):
        return env["BAZAAR_DATA_BUNDLE"]
    if env.get("BAZAAR_TICKER_DATA_VERSION"):
        log.warning("BAZAAR_TICKER_DATA_VERSION is deprecated; set BAZAAR_DATA_BUNDLE instead")
        return env["BAZAAR_TICKER_DATA_VERSION"]
    return DEFAULT_BUNDLE


# Env vars whose values must never reach a log line; redacted by value.
SECRET_ENV_VARS = (
    "PYDANTIC_AI_GATEWAY_API_KEY",
    "BAZAAR_RUNNER_TOKEN",
    "BAZAAR_ADMIN_TOKEN",
    "LOGFIRE_TOKEN",
)
MAX_CONCURRENT_RANGE = range(1, 33)


def parse_max_concurrent(raw: str | None) -> int:
    """BAZAAR_MAX_CONCURRENT: an int in 1..32, or 3 when unset."""
    if raw is None:
        return Settings.max_concurrent
    try:
        return int(raw.strip())
    except ValueError:
        raise ValueError(
            f"BAZAAR_MAX_CONCURRENT must be an integer from 1 to 32, got {raw!r}"
        ) from None


@dataclass(frozen=True)
class Settings:
    market_url: str = "http://127.0.0.1:8000"
    runner_token: str = ""
    runs_dir: Path = Path("runs")
    public_url: str = ""
    web_db: Path = Path("data/web.sqlite3")
    max_queue: int = 30
    max_per_day: int = 150
    max_per_ip_hour: int = 5
    admin_token: str | None = None
    fonts_dir: Path | None = None
    logfire_dashboard_url: str | None = None  # template with "{strategy}"
    market_db: Path | None = None  # read-only, for the ticker bar
    data_bundle: str = DEFAULT_BUNDLE  # the bundle whose window, universe and bars are shown
    max_concurrent: int = 3

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_concurrent, bool)
            or not isinstance(self.max_concurrent, int)
            or self.max_concurrent not in MAX_CONCURRENT_RANGE
        ):
            raise ValueError(
                "BAZAAR_MAX_CONCURRENT must be an integer from 1 to 32, "
                f"got {self.max_concurrent!r}"
            )
        template = self.logfire_dashboard_url
        if template is None:
            return
        parts = urlsplit(template)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("BAZAAR_LOGFIRE_DASHBOARD_URL must be an http:// or https:// URL")
        if "{strategy}" not in template:
            raise ValueError("BAZAAR_LOGFIRE_DASHBOARD_URL must contain {strategy}")

    def logfire_url(self, strategy: str) -> str | None:
        """The dashboard link for one strategy name, or None when no template is configured."""
        if self.logfire_dashboard_url is None:
            return None
        # replace, not format: other braces in the template are left as they are.
        return self.logfire_dashboard_url.replace("{strategy}", quote(strategy, safe=""))

    def __repr__(self) -> str:
        return "Settings(...)"  # keeps the runner and admin tokens out of logs and tracebacks

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            market_url=env.get("BAZAAR_MARKET_URL", cls.market_url),
            runner_token=env.get("BAZAAR_RUNNER_TOKEN", ""),
            runs_dir=Path(env.get("BAZAAR_RUNS_DIR", "runs")),
            public_url=env.get("PUBLIC_URL", ""),
            web_db=Path(env.get("BAZAAR_WEB_DB", "data/web.sqlite3")),
            max_queue=int(env.get("BAZAAR_MAX_QUEUE", "30")),
            max_per_day=int(env.get("BAZAAR_MAX_SUBMISSIONS_PER_DAY", "150")),
            max_per_ip_hour=int(env.get("BAZAAR_MAX_PER_IP_PER_HOUR", "5")),
            admin_token=env.get("BAZAAR_ADMIN_TOKEN") or None,
            fonts_dir=Path(env["BAZAAR_FONTS_DIR"]) if env.get("BAZAAR_FONTS_DIR") else None,
            logfire_dashboard_url=env.get("BAZAAR_LOGFIRE_DASHBOARD_URL") or None,
            market_db=Path(env["BAZAAR_MARKET_DB"]) if env.get("BAZAAR_MARKET_DB") else None,
            data_bundle=data_bundle(env),
            max_concurrent=parse_max_concurrent(env.get("BAZAAR_MAX_CONCURRENT")),
        )
