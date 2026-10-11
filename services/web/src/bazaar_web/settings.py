import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

# Env vars whose values must never reach a log line; redacted by value.
SECRET_ENV_VARS = (
    "PYDANTIC_AI_GATEWAY_API_KEY",
    "BAZAAR_RUNNER_TOKEN",
    "BAZAAR_ADMIN_TOKEN",
    "LOGFIRE_TOKEN",
)
MAX_CONCURRENT_RANGE = range(1, 33)
HOUSE_CONCURRENT_RANGE = range(17)
TRUE, FALSE = {"1", "true", "yes", "on"}, {"0", "false", "no", "off", ""}


def parse_int(name: str, raw: str | None, default: int | None, allowed: range) -> int | None:
    """An int env var in the allowed range, or the default when unset. ValueError otherwise."""
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        value = None
    if value is None or value not in allowed:
        raise ValueError(
            f"{name} must be an integer from {allowed[0]} to {allowed[-1]}, got {raw!r}"
        )
    return value


def parse_flag(name: str, raw: str | None) -> bool:
    """A boolean env var; unset or empty is off."""
    value = (raw or "").strip().lower()
    if value not in TRUE | FALSE:
        raise ValueError(f"{name} must be one of 1/0, true/false, yes/no, on/off, got {raw!r}")
    return value in TRUE


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
    ticker_data_version: str = "demo-bundle-v1"
    max_concurrent: int = 3
    # The house lane: its own slots, filled with house strategies while enabled and not paused.
    house_enabled: bool = False
    house_concurrent: int = 3
    house_max_per_hour: int | None = None  # None: no limit

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
        if self.house_concurrent not in HOUSE_CONCURRENT_RANGE:
            raise ValueError(
                f"BAZAAR_HOUSE_CONCURRENT must be an integer from 0 to 16, got {self.house_concurrent!r}"
            )
        if self.house_max_per_hour is not None and self.house_max_per_hour < 1:
            raise ValueError("BAZAAR_HOUSE_MAX_PER_HOUR must be at least 1 when set")
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
            ticker_data_version=env.get("BAZAAR_TICKER_DATA_VERSION") or "demo-bundle-v1",
            max_concurrent=parse_max_concurrent(env.get("BAZAAR_MAX_CONCURRENT")),
            house_enabled=parse_flag("BAZAAR_HOUSE_ENABLED", env.get("BAZAAR_HOUSE_ENABLED")),
            house_concurrent=parse_int(
                "BAZAAR_HOUSE_CONCURRENT",
                env.get("BAZAAR_HOUSE_CONCURRENT"),
                3,
                HOUSE_CONCURRENT_RANGE,
            ),
            house_max_per_hour=parse_int(
                "BAZAAR_HOUSE_MAX_PER_HOUR",
                env.get("BAZAAR_HOUSE_MAX_PER_HOUR") or None,
                None,
                range(1, 100_001),
            ),
        )
