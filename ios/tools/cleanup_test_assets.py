#!/usr/bin/env python3
"""清理服务器上**only**由 QA 产生的 iostest-* 测试资产。

安全原则
--------
服务器是用户的生产库（19919 张真实资产）。这个脚本只允许删三类东西，且每条删除
都要先命中一个**明确信号**：

  1. `origin` 以 `iostest-` 开头（我们自己给的名字）；
  2. `--ids` 显式给出的资产 id（curl/App 上传时逐条记下来的）；
  3. sha256 命中「已知测试素材」集合（seed manifest、模拟器相册里的实际文件、
     App 账本）—— 这条是给"App 用 IMG_0008.jpg 这种被改过的名字上传"兜底的，
     因为那时候 origin 前缀对不上了，但内容哈希一定对得上。

任何一条资产如果三个信号都不命中，脚本**不会**动它（会打印 NOT_MATCHED 并跳过）。
默认 dry-run，只有 `--yes` 才真的 POST trash + DELETE。

删除顺序按服务端契约：`POST /api/assets/{id}/trash` → `DELETE /api/assets/{id}`
（进回收站再彻底删）。最后核对 `/api/stats` 是否回到基线。

用法
----
    # 看一眼会删什么（不删）
    python3 ios/tools/cleanup_test_assets.py

    # 真删：前缀 + 本机模拟器相册里的 sha + App 账本
    python3 ios/tools/cleanup_test_assets.py --yes --udid <udid> --app-bundle com.photovault.ios

    # 只删指定 id（阶段 A 的 curl 资产）
    python3 ios/tools/cleanup_test_assets.py --yes --ids id1,id2

    # 兜底全量扫描（19k 条，~20s）：按 sha 找出所有测试资产，连被改名的也能抓到
    python3 ios/tools/cleanup_test_assets.py --yes --full-scan --udid <udid>

    # 核对基线
    python3 ios/tools/cleanup_test_assets.py --check-baseline 19919,2159266870148
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE = os.environ.get("PV_BASE", "http://bmc_desktop:7878")
DEFAULT_UDID = os.environ.get("QA_SIM_UDID", "E96F2777-7A6B-4018-B7E5-BD3937CD17A8")
DEVICE_DATA = Path.home() / "Library/Developer/CoreSimulator/Devices"
PREFIX = "iostest-"
MAX_DELETE_DEFAULT = 50


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
# ⚠️ 这台 Mac 系统代理是 127.0.0.1:7897，urllib 会**自动**读系统代理设置并把它用上，
# 而 curl 不会 —— 于是 curl 拿 200、python 拿 502（代理转发局域网地址失败）。
# 服务器在局域网里，必须绕过代理。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(base: str, path: str, method: str = "GET", timeout: float = 60.0):
    """带重试的 JSON 请求（绕过系统代理）。"""
    url = base.rstrip("/") + path
    attempts = 3 if method == "GET" else 1
    last: tuple[int, object] = (0, None)
    for i in range(attempts):
        req = urllib.request.Request(url, method=method)
        try:
            with _OPENER.open(req, timeout=timeout) as r:
                body = r.read()
                return r.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                last = (e.code, json.loads(body))
            except Exception:
                last = (e.code, {"raw": body[:300].decode("utf-8", "replace")})
            if e.code < 500:
                return last
        except Exception as exc:  # noqa: BLE001  连接被掐断
            last = (0, {"error": str(exc)})
        if i + 1 < attempts:
            print(f"  (HTTP {last[0]} 重试 {path} …)")
            time.sleep(1.0 + i)
    return last


def stats(base: str) -> dict:
    code, data = http_json(base, "/api/stats")
    if code != 200:
        raise SystemExit(f"/api/stats 失败：HTTP {code} {data}")
    return data


def list_page(base: str, limit: int, offset: int, q: str | None = None) -> dict:
    params = {"limit": limit, "offset": offset, "library": "gallery"}
    if q:
        params["q"] = q
    code, data = http_json(base, "/api/assets?" + urllib.parse.urlencode(params))
    if code != 200:
        raise SystemExit(f"/api/assets 失败：HTTP {code} {data}")
    return data


def get_asset(base: str, asset_id: str) -> dict | None:
    code, data = http_json(base, f"/api/assets/{asset_id}")
    if code == 404:
        return None
    if code != 200:
        raise SystemExit(f"/api/assets/{asset_id} 失败：HTTP {code} {data}")
    return data


# --------------------------------------------------------------------------- #
# 已知测试素材的 sha256 来源
# --------------------------------------------------------------------------- #
def shas_from_manifest() -> dict[str, str]:
    """ios/qa/seed/manifest.json 里记录的文件 sha256。"""
    out: dict[str, str] = {}
    p = ROOT / "ios/qa/seed/manifest.json"
    if p.exists():
        for m in json.loads(p.read_text()).get("assets", []):
            if m.get("sha256"):
                out[m["sha256"]] = f"seed manifest:{m['file']}"
    return out


def shas_from_simulator(udid: str, verbose: bool = True) -> dict[str, str]:
    """直接对模拟器相册里的实物文件算 sha256（DCIM 下的原始文件）。

    比 seed manifest 更权威：`simctl addmedia` 落进 DCIM 的文件可能被系统重写过
    （Live Photo 合成后静帧/动态都重存了一份），manifest 里的 sha 未必等于服务器上
    那条资产的内容哈希。这里按 ZDIRECTORY + ZFILENAME 精确定位文件，另外把同 stem
    的 .MOV 也算进去（覆盖"动态被单独上传成一条视频"的失败路径）。
    """
    ph = DEVICE_DATA / udid / "data/Media/PhotoData"
    if not (ph / "Photos.sqlite").exists():
        if verbose:
            print(f"  (跳过模拟器相册：找不到 {ph})")
        return {}
    tmp = Path(tempfile.mkdtemp(prefix="qacleanup-"))
    for name in ("Photos.sqlite", "Photos.sqlite-wal", "Photos.sqlite-shm"):
        if (ph / name).exists():
            shutil.copy2(ph / name, tmp / name)
    con = sqlite3.connect(f"file:{tmp/'Photos.sqlite'}?mode=ro", uri=True)
    try:
        rows = con.execute(
            """
            SELECT d.ZORIGINALFILENAME, a.ZDIRECTORY, a.ZFILENAME
            FROM ZASSET a
            LEFT JOIN ZADDITIONALASSETATTRIBUTES d ON d.ZASSET = a.Z_PK
            WHERE a.ZFILENAME LIKE ? OR d.ZORIGINALFILENAME LIKE ?
            """,
            (PREFIX + "%", PREFIX + "%"),
        ).fetchall()
    finally:
        con.close()
        shutil.rmtree(tmp, ignore_errors=True)

    media = DEVICE_DATA / udid / "data/Media"
    dcim = media / "DCIM"
    # DCIM 下的文件索引（注意别把 PhotoData/Thumbnails/V2/DCIM 里的缩略图算进来）
    index: dict[str, list[Path]] = {}
    if dcim.exists():
        for f in dcim.rglob("*"):
            if f.is_file():
                index.setdefault(f.name, []).append(f)

    out: dict[str, str] = {}
    for original, directory, filename in rows:
        stem = Path(filename).stem if filename else (Path(original).stem if original else None)
        want = {filename, original}
        if stem:
            for ext in (".MOV", ".mov", ".HEIC", ".JPG", ".jpg", ".PNG", ".png", ".MP4"):
                want.add(stem + ext)
        for name in want:
            if not name:
                continue
            for f in index.get(name, []):
                sha = hashlib.sha256(f.read_bytes()).hexdigest()
                out[sha] = f"模拟器相册:{f.name}"
            # ZDIRECTORY 精确路径（"DCIM/100APPLE"）
            if directory:
                f = media / directory / name
                if f.is_file():
                    out[hashlib.sha256(f.read_bytes()).hexdigest()] = f"模拟器相册:{directory}/{name}"
    if verbose:
        print(f"  模拟器相册 iostest-* 资产文件：{len(out)} 个 sha256")
    return out


def shas_from_app_ledger(udid: str, bundle: str, verbose: bool = True) -> tuple[dict[str, str], set[str]]:
    """从 App 容器里的同步账本取 (sha256→来源, serverAssetID 集合)。

    sync-core 的账本格式还没定稿，这里按"键名含 asset/id/sha"递归捞，兼容
    SyncLedger.json 的几种可能结构；捞不到就返回空，不报错。
    """
    shas: dict[str, str] = {}
    ids: set[str] = set()
    try:
        r = subprocess.run(
            ["xcrun", "simctl", "get_app_container", udid, bundle, "data"],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print(f"  (跳过 App 账本：simctl 调用失败 {exc})")
        return shas, ids
    if r.returncode != 0 or not r.stdout.strip():
        if verbose:
            print(f"  (跳过 App 账本：App 未安装？{r.stderr.strip()[:120]})")
        return shas, ids
    container = Path(r.stdout.strip())
    candidates = list(container.rglob("*.json"))
    if verbose:
        print(f"  App 容器：{container}（找到 {len(candidates)} 个 json）")

    def walk(obj, path=""):
        if isinstance(obj, dict):
            low = {str(k).lower(): v for k, v in obj.items()}
            # serverAssetID 记录
            for k, v in low.items():
                if isinstance(v, str) and _looks_uuid(v) and ("asset" in k or k == "id"):
                    ids.add(v)
                if isinstance(v, str) and _looks_sha(v) and ("sha" in k or "hash" in k):
                    shas[v] = f"App 账本:{'/' + path if path else k}"
            for k, v in obj.items():
                walk(v, f"{path}/{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk(v, f"{path}[{i}]")

    for f in candidates:
        try:
            data = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            continue
        walk(data, f.name)
    if verbose:
        print(f"  App 账本：serverAssetID {len(ids)} 个，sha256 {len(shas)} 个")
    return shas, ids


def _looks_uuid(s: str) -> bool:
    import re
    return bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", s))


def _looks_sha(s: str) -> bool:
    import re
    return bool(re.fullmatch(r"[0-9a-f]{64}", s.lower()))


# --------------------------------------------------------------------------- #
# 候选收集
# --------------------------------------------------------------------------- #
def collect_candidates(base: str, *, prefix: str, ids: list[str], sha_map: dict[str, str],
                       full_scan: bool, verbose: bool = True) -> dict[str, dict]:
    """返回 {asset_id: {asset, reasons:[...]}}，只包含命中信号的资产。"""
    found: dict[str, dict] = {}

    def add(asset: dict, reason: str) -> None:
        aid = asset["id"]
        entry = found.setdefault(aid, {"asset": asset, "reasons": []})
        if reason not in entry["reasons"]:
            entry["reasons"].append(reason)

    # 1) origin 前缀：先用 q= 粗查，再逐条核对 origin（q 是模糊匹配，不能盲信）
    if prefix:
        page = list_page(base, 300, 0, q=prefix.rstrip("-"))
        for it in page.get("items", []):
            origin = (it.get("origin") or "")
            if origin.lower().startswith(prefix.lower()):
                add(it, f"origin 前缀 {prefix}")

    # 2) 显式 id
    for aid in ids:
        a = get_asset(base, aid)
        if a:
            add(a, "显式 --ids / 账本 serverAssetID")

    # 3) sha256 命中：全量翻页（唯一可靠的兜底，能抓到被改名的资产）
    if full_scan and sha_map:
        offset, total, scanned = 0, None, 0
        while True:
            page = list_page(base, 300, offset)
            items = page.get("items", [])
            total = page.get("total")
            for it in items:
                scanned += 1
                sha = (it.get("sha") or "").lower()
                if sha in sha_map:
                    add(it, f"sha256 命中（{sha_map[sha]}）")
            if not items or (total is not None and scanned >= total):
                break
            offset += len(items)
            if verbose and offset % 3000 == 0:
                print(f"    全量扫描 {offset}/{total} …")
        if verbose:
            print(f"  全量扫描完成：{scanned} 条")

    return found


# --------------------------------------------------------------------------- #
# 删除
# --------------------------------------------------------------------------- #
def trash_and_purge(base: str, asset_id: str) -> dict:
    code_t, resp_t = http_json(base, f"/api/assets/{asset_id}/trash", method="POST")
    code_d, resp_d = http_json(base, f"/api/assets/{asset_id}", method="DELETE")
    return {
        "id": asset_id,
        "trash_http": code_t,
        "trash": resp_t,
        "delete_http": code_d,
        "delete": resp_d,
        "ok": code_t == 200 and code_d == 200,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--udid", default=DEFAULT_UDID)
    ap.add_argument("--prefix", default=PREFIX, help="只认这个前缀的 origin（默认 iostest-）")
    ap.add_argument("--ids", default="", help="逗号分隔的资产 id（curl/App 记录下来的）")
    ap.add_argument("--ledger", default="", help="额外账本 json（含 serverAssetID / sha256）")
    ap.add_argument("--app-bundle", default="", help="从这个 bundle 的 App 容器自动读账本")
    ap.add_argument("--no-simulator", action="store_true", help="不读模拟器相册算 sha")
    ap.add_argument("--full-scan", action="store_true", help="全量翻页按 sha256 兜底（~20s）")
    ap.add_argument("--yes", action="store_true", help="真的执行 trash+delete（默认 dry-run）")
    ap.add_argument("--max", type=int, default=MAX_DELETE_DEFAULT, help="一次最多删多少条，防手滑")
    ap.add_argument("--check-baseline", default="", help="只核对基线：count,bytes")
    ap.add_argument("--json-out", default="", help="把结果写成 json")
    args = ap.parse_args()

    before = stats(args.base)
    baseline = {"count": before["count"], "gallery": before["gallery"],
                "material": before["material"], "bytes": before["bytes"], "trash": before["trash"]}
    print(f"服务器 {args.base}")
    print(f"当前：count={baseline['count']} gallery={baseline['gallery']} "
          f"material={baseline['material']} bytes={baseline['bytes']} trash={baseline['trash']}")

    if args.check_baseline:
        want = [int(x) for x in args.check_baseline.split(",")]
        ok = want[0] == before["count"] and (len(want) < 2 or want[1] == before["bytes"])
        print(f"基线核对：期望 count={want[0]}" + (f" bytes={want[1]}" if len(want) > 1 else "")
              + f" → {'✔ 已回到基线' if ok else '✘ 尚未回到基线'}")
        return 0 if ok else 2

    sha_map: dict[str, str] = {}
    sha_map.update(shas_from_manifest())
    if not args.no_simulator:
        sha_map.update(shas_from_simulator(args.udid))
    ids = [x.strip() for x in args.ids.split(",") if x.strip()]
    if args.ledger:
        try:
            data = json.loads(Path(args.ledger).read_text())
            for v in _walk_ids(data):
                ids.append(v)
        except Exception as exc:  # noqa: BLE001
            print(f"  !! 读账本失败：{exc}")
    if args.app_bundle:
        ledger_shas, ledger_ids = shas_from_app_ledger(args.udid, args.app_bundle)
        sha_map.update(ledger_shas)
        ids.extend(sorted(ledger_ids))

    print(f"已知测试素材 sha256：{len(sha_map)} 个；显式 id：{len(set(ids))} 个")
    candidates = collect_candidates(args.base, prefix=args.prefix, ids=list(dict.fromkeys(ids)),
                                    sha_map=sha_map, full_scan=args.full_scan)
    print(f"\n命中信号的候选资产：{len(candidates)} 条")
    for aid, entry in candidates.items():
        a = entry["asset"]
        print(f"  {aid}  origin={a.get('origin'):<28} kind={a.get('kind'):<11} "
              f"bytes={a.get('bytes'):<10} sha={(a.get('sha') or '')[:12]}")
        for r in entry["reasons"]:
            print(f"      原因：{r}")

    if not candidates:
        print("\n没有需要清理的资产。")
        return 0
    if len(candidates) > args.max:
        print(f"\n!! 命中 {len(candidates)} 条 > --max {args.max}，拒绝执行（先人工确认）")
        return 3

    if not args.yes:
        print("\n[dry-run] 没加 --yes，不执行删除。")
        return 0

    print("\n开始 trash → delete：")
    results = []
    for aid, entry in candidates.items():
        r = trash_and_purge(args.base, aid)
        r["origin"] = entry["asset"].get("origin")
        r["sha"] = entry["asset"].get("sha")
        r["reasons"] = entry["reasons"]
        results.append(r)
        mark = "✔" if r["ok"] else "✘"
        print(f"  {mark} {aid} origin={r['origin']} "
              f"trash HTTP {r['trash_http']} delete HTTP {r['delete_http']}")

    after = stats(args.base)
    print(f"\n清理后：count={after['count']} gallery={after['gallery']} "
          f"material={after['material']} bytes={after['bytes']} trash={after['trash']}")
    print(f"对比基线：count {baseline['count']}→{after['count']} "
          f"({after['count']-baseline['count']:+d})，"
          f"bytes {baseline['bytes']}→{after['bytes']} ({after['bytes']-baseline['bytes']:+d})")
    # 剩余 iostest-* 复核
    left = collect_candidates(args.base, prefix=args.prefix, ids=[], sha_map={}, full_scan=False, verbose=False)
    print(f"残留 origin 前缀 {args.prefix} 的资产：{len(left)} 条"
          + (f" → {[ (v['asset']['id'], v['asset'].get('origin')) for v in left.values() ]}" if left else ""))

    payload = {"base": args.base, "baseline": baseline, "after": after,
               "deleted": results, "remaining_prefixed": len(left)}
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"结果 → {args.json_out}")
    return 0 if all(r["ok"] for r in results) else 1


def _walk_ids(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and _looks_uuid(v) and ("asset" in k.lower() or k.lower() == "id"):
                yield v
            else:
                yield from _walk_ids(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_ids(v)


if __name__ == "__main__":
    sys.exit(main())
