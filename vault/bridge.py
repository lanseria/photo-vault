"""宿主机桥 —— 容器里的 app 碰不到 Windows 桌面的三件事，都在这里做。

    uv run photo-vault bridge            # 默认 127.0.0.1:8099

为什么需要它（架构上的硬边界）：

  · app 跑在 Docker 里，挂载点是 /host，进程在 Linux 命名空间内 ——
    它既没有 Windows 桌面，也调不到 explorer.exe / 剪贴板 / OLE 拖拽；
  · 浏览器同样不行：从 http 页面拖一个 <img>/<a> 出去，外部程序只能拿到
    text/uri-list，拿不到真实文件路径（Chromium 出于安全不会把本地路径交出去）。

所以"把素材交给剪映"只能由**宿主机上的进程**代劳。这个桥只做四件事，
全部是幂等的、不改素材的：

    POST /reveal     {path}     → 资源管理器打开目录 / 定位并选中文件
    POST /clipboard  {paths}    → 把文件列表放进剪贴板（CF_HDROP，多选）
    POST /open       {path}     → 用系统默认播放器打开影视文件（MKV/AVI 浏览器放不了）
    GET  /health                → 前端探测用

安全：只绑 127.0.0.1，且只接受来自本机 127.0.0.1/localhost 页面的跨域请求 ——
否则任何网页都能往你的剪贴板里塞文件，那是个真实的钓鱼面。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Photo Vault Bridge")

# 只放行本机页面：127.0.0.1 / localhost 的任意端口（8081=容器、8082=宿主模式…）
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^http://(127\.0\.0\.1|localhost)(:\d+)?$",
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)


class RevealBody(BaseModel):
    path: str


class ClipboardBody(BaseModel):
    paths: list[str]


# ---------------------------------------------------------------- 剪贴板 CF_HDROP
# 资源管理器里 Ctrl+C 一个文件，放进剪贴板的就是 CF_HDROP：一个 DROPFILES 头
# 加上 UTF-16 双 \0 结尾的路径列表。这里手工拼同样的一段内存，不需要 pywin32。

CF_HDROP = 15
GMEM_MOVEABLE = 0x0002


class DROPFILES(ctypes.Structure):
    _fields_ = [
        ("pFiles", wintypes.DWORD),
        ("pt", wintypes.POINT),
        ("fNC", wintypes.BOOL),
        ("fWide", wintypes.BOOL),
    ]


def _win_dlls():
    if os.name != "nt":
        raise HTTPException(501, "桥只能在 Windows 上跑")
    u = ctypes.WinDLL("user32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    u.OpenClipboard.argtypes = [wintypes.HWND]
    u.OpenClipboard.restype = wintypes.BOOL
    u.EmptyClipboard.restype = wintypes.BOOL
    u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u.SetClipboardData.restype = wintypes.HANDLE
    u.CloseClipboard.restype = wintypes.BOOL
    k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k.GlobalAlloc.restype = wintypes.HGLOBAL
    k.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k.GlobalLock.restype = wintypes.LPVOID
    k.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    return u, k


def set_clipboard_files(paths: list[str]) -> int:
    u, k = _win_dlls()
    files = [str(Path(p)) for p in paths if Path(p).exists()]
    if not files:
        raise HTTPException(400, "没有存在的文件")

    payload = ("".join(f + "\0" for f in files) + "\0").encode("utf-16-le")
    head = DROPFILES(pFiles=ctypes.sizeof(DROPFILES), fWide=True)
    total = ctypes.sizeof(DROPFILES) + len(payload)

    h = k.GlobalAlloc(GMEM_MOVEABLE, total)
    if not h:
        raise HTTPException(500, "GlobalAlloc 失败")
    p = k.GlobalLock(h)
    ctypes.memmove(p, ctypes.byref(head), ctypes.sizeof(head))
    ctypes.memmove(p + ctypes.sizeof(head), payload, len(payload))
    k.GlobalUnlock(h)

    if not u.OpenClipboard(None):
        raise HTTPException(500, "剪贴板被别的程序占着")
    try:
        u.EmptyClipboard()
        if not u.SetClipboardData(CF_HDROP, h):
            raise HTTPException(500, "SetClipboardData 失败")
        # 成功后内存所有权归系统，不能再 GlobalFree
    finally:
        u.CloseClipboard()
    return len(files)


def _shell_open(verb: str, target: str, args: str | None = None) -> None:
    res = ctypes.windll.shell32.ShellExecuteW(None, verb, target, args, None, 1)
    if res <= 32:
        raise HTTPException(500, f"ShellExecute 失败 (code={res})")


# ---------------------------------------------------------------- 接口

@app.get("/health")
def health() -> dict:
    return {"ok": True, "platform": sys.platform, "pid": os.getpid()}


@app.post("/reveal")
def reveal(body: RevealBody) -> dict:
    """目录 → 打开；文件 → 打开所在目录并选中它。

    explorer 的 /select, 参数需要"整串"传给 ShellExecute，
    用 subprocess 列表参数会因为空格被二次转义而失效。
    """
    p = Path(body.path)
    if not p.exists():
        raise HTTPException(404, f"不存在：{body.path}")
    if p.is_dir():
        _shell_open("open", str(p))
    else:
        _shell_open("open", "explorer.exe", f'/select,"{p}"')
    return {"ok": True, "path": str(p)}


@app.post("/clipboard")
def clipboard(body: ClipboardBody) -> dict:
    n = set_clipboard_files(body.paths)
    return {"ok": True, "files": n}


@app.post("/open")
def open_file(body: RevealBody) -> dict:
    """用系统默认程序打开一个文件。影视库用它绕开浏览器的容器限制：
    Chrome 放不了 MKV/AVI/TS，但本机的 PotPlayer/VLC/MPC 都能放，
    而且同目录的 .srt/.ass 字幕会被播放器自动加载。"""
    p = Path(body.path)
    if not p.is_file():
        raise HTTPException(404, f"不存在：{body.path}")
    _shell_open("open", str(p))
    return {"ok": True, "path": str(p)}


# ---------------------------------------------------------------- 开机自启
# 桥不常开，"出稿"就退化成"复制路径"——体验差一档。所以给一个一键自启：
# 写 HKCU\...\Run（只影响当前用户，不碰系统级设置），用 pythonw 起，不留黑窗。

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "PhotoVaultBridge"


def _launch_cmd() -> str:
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")       # 无控制台窗口
    target = pyw if pyw.exists() else exe
    return f'"{target}" -m vault.bridge'


def install_autostart() -> str:
    if os.name != "nt":
        raise RuntimeError("只能装在 Windows 上")
    import winreg

    cmd = _launch_cmd()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, cmd)
    return cmd


def uninstall_autostart() -> bool:
    if os.name != "nt":
        raise RuntimeError("只能在 Windows 上卸")
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, RUN_NAME)
        return True
    except FileNotFoundError:
        return False


def run(host: str = "127.0.0.1", port: int = 8099) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
