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
    # 宿主机上 HOST_ROOT 的真实路径字符串（E:\photo 之类）。容器里看不到这个路径，
    # 只用来生成"给人/给剪辑软件看的路径"，落盘一律用 vault_root/derived_root。
    host_path_root: str = ""
    # 影视库的独立挂载点：容器里看到的是 media_root，宿主机上是 media_host_root。
    # 影视文件不进 vault（不搬家），只登记 root_key + 相对路径，所以换挂载点
    # 不用改库里的数据，改 env 就行。
    media_root: Path = Path("/media")
    media_host_root: str = ""

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
            host_path_root=os.environ.get("HOST_PATH_ROOT", "").strip(),
            media_root=_env_path("MEDIA_ROOT", "/media"),
            media_host_root=os.environ.get("MEDIA_HOST_ROOT", "").strip(),
        )

    def ensure_dirs(self) -> None:
        self.vault_root.mkdir(parents=True, exist_ok=True)
        self.landing_root.mkdir(parents=True, exist_ok=True)
        (self.derived_root / "thumbs").mkdir(parents=True, exist_ok=True)
        (self.derived_root / "media").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 影视库路径
    # 库里的影视只记 (root_key, rel_path)，不记绝对路径：
    #   media → MEDIA_ROOT（影视独立挂载点）
    #   host  → HOST_ROOT（vault_root 的父目录）
    # 这样宿主机换盘符、容器换挂载点都不用动数据。

    def roots(self) -> list[tuple[str, Path]]:
        return [("media", self.media_root), ("host", self.vault_root.parent)]

    def classify(self, path: Path) -> tuple[str, str]:
        """本机可读路径 → (root_key, posix 相对路径)；不在任何已知根下就报错。"""
        p = path.resolve()
        for key, root in self.roots():
            try:
                rel = p.relative_to(root.resolve())
            except ValueError:
                continue
            if str(rel) == ".":
                raise ValueError(f"{p} 是影视根目录本身，请选具体的文件或子目录")
            return key, rel.as_posix()
        roots = "、".join(f"{k}={r}" for k, r in self.roots())
        raise ValueError(f"{p} 不在已挂载的影视目录里（{roots}）")

    def path_for(self, root_key: str, rel_path: str) -> Path:
        for key, root in self.roots():
            if key == root_key:
                return root / Path(rel_path)
        raise ValueError(f"未知的 root_key: {root_key}")

    def host_path_for(self, root_key: str, rel_path: str) -> str:
        """给资源管理器/播放器看的宿主机路径（容器里拼字符串，不落盘）。"""
        host_root = {
            "media": self.media_host_root,
            "host": self.host_path_root,
        }.get(root_key, "")
        host_root = (host_root or "").rstrip("\\/")
        if not host_root:
            return str(self.path_for(root_key, rel_path))
        return host_root + "\\" + rel_path.replace("/", "\\")