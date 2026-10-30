from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


@dataclass(frozen=True)
class Config:
    database_url: str
    vault_root: Path
    derived_root: Path
    landing_root: Path
    default_tz_offset_min: int
    thumb_size: int
    thumb_quality: int

    @classmethod
    def load(cls) -> "Config":
        vault = _env_path("VAULT_ROOT", "/data/vault")
        return cls(
            database_url=os.environ.get(
                "DATABASE_URL", "postgresql://vault:vault@db:5432/vault"
            ),
            vault_root=vault,
            derived_root=_env_path("DERIVED_ROOT", "/data/derived"),
            # landing 必须与 vault 同文件系统，才能用 os.replace 原子提交
            landing_root=_env_path("LANDING_ROOT", str(vault / ".landing")),
            default_tz_offset_min=int(os.environ.get("DEFAULT_TZ_OFFSET_MIN", "480")),
            thumb_size=int(os.environ.get("THUMB_SIZE", "512")),
            thumb_quality=int(os.environ.get("THUMB_QUALITY", "82")),
        )

    def ensure_dirs(self) -> None:
        self.vault_root.mkdir(parents=True, exist_ok=True)
        self.landing_root.mkdir(parents=True, exist_ok=True)
        (self.derived_root / "thumbs").mkdir(parents=True, exist_ok=True)