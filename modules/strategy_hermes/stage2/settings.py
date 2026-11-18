"""Isolated database and cache configuration for each environment."""
from dataclasses import dataclass
from pathlib import Path
import re
import shlex
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    env: str
    dsn: str
    redis_url: str
    redis_prefix: str
    namespace: str

    @classmethod
    def read(cls, path):
        path = Path(path)
        if not path.is_absolute() or path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("Stage 2 environment file must be private and absolute")
        values = {}
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, value = line.split("=", 1)
            if value[:1] in ("'", '"'):
                value, = shlex.split(value)
            values[key] = value
        return cls.from_values(values)

    @classmethod
    def from_values(cls, values):
        env = values["APP_ENV"]
        environments = {
            "development": ("stock_v4_dev", "127.0.0.1", 54320, "127.0.0.1", 63794, "/0", "stock-v4:dev:"),
            "test": ("stock_v4_test", "127.0.0.1", 54320, "127.0.0.1", 63794, "/1", "stock-v4:test:"),
            "production": ("stock_v4_prod", "postgresql", 5432, "redis", 6379, "/0", "stock-v4:prod:"),
        }
        if env not in environments:
            raise ValueError("unsupported Stage 2 environment")
        db_name, db_host, db_port, cache_host, cache_port, cache_path, prefix = environments[env]
        dsn = values["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
        db = urlsplit(dsn)
        redis_url = values["REDIS_URL"]
        cache = urlsplit(redis_url)
        if (db.path != "/" + db_name or db.hostname != db_host or db.port != db_port
                or cache.hostname != cache_host or cache.port != cache_port or cache.path != cache_path
                or values["REDIS_KEY_PREFIX"] != prefix
                or db.query or cache.query or db.fragment or cache.fragment
                or db.scheme != "postgresql" or cache.scheme != "redis"
                or env == "production" and db.username != db_name):
            raise ValueError("Stage 2 database or cache points outside its environment")
        namespace = values["AUTO_RUN_NAMESPACE"]
        if not namespace.startswith(prefix):
            raise ValueError("namespace points outside its environment")
        if env == "production" and (len(namespace) > 120
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", namespace.removeprefix(prefix))):
            raise ValueError("invalid production namespace")
        return cls(env, dsn, redis_url, prefix, namespace)
