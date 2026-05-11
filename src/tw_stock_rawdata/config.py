from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class AppConfig:
    """RawData 用的精簡配置——只保留 DB 相關欄位。"""

    database_url: str
    use_db: bool

    @classmethod
    def from_env(cls) -> "AppConfig":
        """從 .env 讀取設定。"""
        return cls(
            database_url=os.getenv("DATABASE_URL", ""),
            use_db=os.getenv("USE_DB", "false").lower() in ("true", "1", "yes"),
        )
