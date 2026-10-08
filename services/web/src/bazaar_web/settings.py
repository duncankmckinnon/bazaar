import os
from dataclasses import dataclass
from pathlib import Path


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
    max_concurrent: int = 3

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
        )
