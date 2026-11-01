#!/usr/bin/env python3
"""端到端验证：App 同步完以后，从**服务器侧**核对到底发生了什么。

为什么单独一个脚本
------------------
App 自己报告「已上传 9 个」只是它的一面之词。真正要回答的是：

  1. 服务器上确实多了那批测试素材吗（origin / kind / 尺寸 / 缩略图都对）？
  2. **有没有多出任何不属于测试素材的资产**？ ← 最重要，直接关系到用户真实库安全
  3. Live Photo 是不是合成成一条 kind=live_photo，而不是一条照片+一条视频？
  4. 第二次同步是不是幂等（不新增）？
  5. 清理后有没有回到基线？

做法是「前后两次全量快照 + 差集」：先把 gallery 的全部 asset id 抓一遍，同步后再抓一遍，
新增的 id 集合必须 **100% 命中测试信号**（origin 前缀 iostest- / 已知 sha256 / 账本 id），
否则直接 FAIL。这样即使 App 因为日期筛选没生效把系统样例照传上去，也会被当场抓住。

用法
----
    # 同步前：拍一张快照
    python3 ios/tools/e2e_check.py snapshot --out ios/qa/evidence/e2e-before.json

    # 同步后：带上快照核对（差集 + 安全断言 + kind/缩略图逐条检查）
    python3 ios/tools/e2e_check.py verify --before ios/qa/evidence/e2e-before.json

    # 第二次同步后：断言"没有任何新增"（幂等）
    python3 ios/tools/e2e_check.py verify --before <第二次同步前快照> --expect-no-new

    # 清理后：断言回到基线、且服务器上再没有 iostest-* 资产
    python3 ios/tools/e2e_check.py assert-clean --baseline-count 19919

退出码：0 = 全过；1 = 有 FAIL（可接 CI）；2 = 环境/参数问题。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from seed_simulator_media import DEFAULT_UDID, PREFIX, open_photos_db  # noqa: E402

BASE = os.environ.get("PV_BASE", "http://bmc_desktop:7878")
DEVICE_DATA = Path.home() / "Library/Developer/CoreSimulator/Devices"

# 这台 Mac 系统代理是 127.0.0.1:7897，urllib 会自动用上，局域网请求会变 502；必须绕开
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http_json(path: str, method: str = "GET", timeout: float = 60.0):
    url = BASE.rstrip("/") + path
    last = (0, None)
    for i in range(3 if method == "GET" else 1):
        try:
            with _OPENER.open(urllib.request.Request(url, method=method), timeout=timeout) as r:
                body = r.read()
                return r.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                last = (e.code, json.loads(body))
            except Exception:
                last = (e.code, {"raw": body[:200].decode("utf-8", "replace")})
            if e.code < 500:
                return last
        except Exception as exc:  # noqa: BLE001
            last = (0, {"error": str(exc)})
        if i < 2:
            time.sleep(1.0)
    return last


def http_bytes(path: str, timeout: float = 60.0) -> tuple[int, bytes, dict]:
    url = BASE.rstrip("/") + path
    try:
        with _OPENER.open(url, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)
    except Exception:  # noqa: BLE001
        return 0, b"", {}


def stats() -> dict:
    code, data = http_json("/api/stats")
    if code != 200:
        raise SystemExit(f"/api/stats 失败：HTTP {code} {data}")
    return data


def list_all_gallery() -> dict[str, dict]:
    """翻页抓 gallery 全部资产：id → 精简字段。"""
    out: dict[str, dict] = {}
    offset = 0
    while True:
        code, page = http_json(f"/api/assets?limit=300&offset={offset}&library=gallery")
        if code != 200:
            raise SystemExit(f"/api/assets 失败：HTTP {code} {page}")
        items = page.get("items", [])
        for it in items:
            out[it["id"]] = {
                "origin": it.get("origin"), "kind": it.get("kind"), "bytes": it.get("bytes"),
                "sha": it.get("sha"), "has_thumb": it.get("has_thumb"),
                "width": it.get("width"), "height": it.get("height"),
                "taken": it.get("taken"), "model": it.get("model"),
            }
        total = page.get("total")
        offset += len(items)
        if not items or (total is not None and offset >= total):
            break
    return out


# --------------------------------------------------------------------------- #
# 模拟器相册侧：期望上传哪些资产 + 它们的 sha256
# --------------------------------------------------------------------------- #
def simulator_expectations(udid: str) -> tuple[list[dict], dict[str, str]]:
    """返回 (期望的资产列表, sha256→文件说明)。

    期望列表从 PhotoKit 的真实状态推导，而不是硬编码文件名：Live Photo 的静帧会
    带着 pairedVideo 一起被 App 拆成「先静帧后动态」两次上传，服务端应合成一条
    kind=live_photo。这里把规则写死，是**验收标准**的一部分（断言用）。
    """
    rows = _verify_photo_kit(udid)
    expected: list[dict] = []
    sha_map: dict[str, str] = {}
    for r in rows:
        name = r.get("ZORIGINALFILENAME") or r["ZFILENAME"]
        if not name:
            continue
        if r["live_photo"]:
            # 静帧原名 + 动态用静帧 stem + ".MOV"（SyncPlanner 的约定）
            stem = Path(name).stem
            expected.append({"origin": name, "kind": "live_photo", "live": True,
                             "motion_origin": stem + ".MOV", "source": "PhotoKit:live"})
        elif (r["ZKIND"] or 0) == 1:
            expected.append({"origin": name, "kind": "video", "live": False, "source": "PhotoKit:video"})
        else:
            expected.append({"origin": name, "kind": "photo", "live": False, "source": "PhotoKit:photo"})
    # sha256：直接对相册里落盘的实物文件算（比 manifest 可靠）
    media = DEVICE_DATA / udid / "data/Media"
    dcim = media / "DCIM"
    index: dict[str, list[Path]] = {}
    if dcim.exists():
        for f in dcim.rglob("*"):
            if f.is_file():
                index.setdefault(f.name, []).append(f)
    for r in rows:
        filename = r["ZFILENAME"]
        stem = Path(filename).stem
        for nm in {filename, r.get("ZORIGINALFILENAME")} | {stem + e for e in
                                                             (".MOV", ".mov", ".HEIC", ".JPG", ".PNG", ".MOV")}:
            if not nm:
                continue
            for f in index.get(nm, []):
                sha_map[hashlib.sha256(f.read_bytes()).hexdigest()] = f.name
    return expected, sha_map


def _verify_photo_kit(udid: str) -> list[dict]:
    """复用 seed 脚本里的只读核验逻辑（避免两处各写一份 SQL）。"""
    from seed_simulator_media import verify as seed_verify
    return seed_verify(udid)


# --------------------------------------------------------------------------- #
# 快照 / 核对
# --------------------------------------------------------------------------- #
def cmd_snapshot(args) -> int:
    data = list_all_gallery()
    st = stats()
    payload = {"taken_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "base": BASE, "stats": {k: st[k] for k in
                                       ("count", "gallery", "material", "bytes", "trash")},
               "assets": data}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False))
    print(f"快照：{len(data)} 条 gallery 资产 → {out}")
    print(f"stats：{payload['stats']}")
    return 0


def cmd_verify(args) -> int:
    before = json.loads(Path(args.before).read_text())
    before_assets: dict[str, dict] = before["assets"]
    before_stats: dict = before["stats"]

    after_assets = list_all_gallery()
    after_stats = {k: stats()[k] for k in ("count", "gallery", "material", "bytes", "trash")}

    expected, sha_map = simulator_expectations(args.udid)
    new_ids = [i for i in after_assets if i not in before_assets]
    gone_ids = [i for i in before_assets if i not in after_assets]

    fails: list[str] = []
    warns: list[str] = []

    print("=" * 78)
    print("1) 服务器计数前后对比")
    print("=" * 78)
    print(f"{'字段':<10}{'同步前':>16}{'同步后':>16}{'差':>10}")
    for k in ("count", "gallery", "material", "bytes", "trash"):
        print(f"{k:<10}{before_stats[k]:>16}{after_stats[k]:>16}{after_stats[k]-before_stats[k]:>+10}")

    print()
    print("=" * 78)
    print(f"2) 新增资产（{len(new_ids)} 条）逐条核对")
    print("=" * 78)
    expected_by_origin = {e["origin"].lower(): e for e in expected}
    matched_expected: dict[str, str] = {}
    suspicious: list[tuple[str, dict]] = []

    for aid in new_ids:
        a = after_assets[aid]
        origin = (a["origin"] or "")
        low = origin.lower()
        reasons = []
        if low.startswith(PREFIX.lower()):
            reasons.append("origin 前缀 iostest-")
        if (a["sha"] or "").lower() in sha_map:
            reasons.append(f"sha256 命中模拟器相册:{sha_map[(a['sha'] or '').lower()]}")
        exp = expected_by_origin.get(low)
        if exp:
            reasons.append(f"匹配 PhotoKit 期望（{exp['kind']}）")
            matched_expected[low] = aid
        mark = "✔" if reasons else "✘ 可疑"
        print(f"  {mark} {aid}")
        print(f"     origin={origin!r:<26} kind={a['kind']:<11} bytes={a['bytes']:<9} "
              f"{a['width']}x{a['height']} sha={(a['sha'] or '')[:12]}")
        if reasons:
            print(f"     命中：{'；'.join(reasons)}")
        else:
            suspicious.append((aid, a))

    if args.expect_no_new:
        if new_ids:
            fails.append(f"--expect-no-new 但服务器新增了 {len(new_ids)} 条资产（幂等性失败）")
        if after_stats["count"] != before_stats["count"]:
            fails.append(f"--expect-no-new 但 count 从 {before_stats['count']} 变了 "
                         f"{after_stats['count']}")

    print()
    print("=" * 78)
    print("3) 期望资产是否全部到位（按 PhotoKit 里的真实内容）")
    print("=" * 78)
    print(f"  PhotoKit 里共 {len(expected)} 条 iostest-* 资产，应产出 "
          f"{len(expected)} 条服务器资产（Live Photo 两条文件合成一条）")
    for e in expected:
        aid = matched_expected.get(e["origin"].lower())
        if not aid:
            print(f"  ✘ 缺失：{e['origin']}（期望 kind={e['kind']}）")
            fails.append(f"期望资产没有上传成功：{e['origin']}（{e['kind']}）")
            continue
        a = after_assets[aid]
        kind_ok = a["kind"] == e["kind"]
        print(f"  {'✔' if kind_ok else '✘'} {e['origin']:<24} kind={a['kind']:<11} "
              f"（期望 {e['kind']}）id={aid}")
        if not kind_ok:
            fails.append(f"{e['origin']} 的 kind 是 {a['kind']}，期望 {e['kind']}")
        if e.get("live"):
            code, body, _ = http_bytes(f"/file/{aid}?role=live_motion")
            print(f"      实况动态文件 /file?role=live_motion → HTTP {code}, {len(body)} bytes")
            if code != 200 or not body:
                fails.append(f"Live Photo {e['origin']} 取不到 role=live_motion 文件")

    print()
    print("=" * 78)
    print("4) 缩略图 / 预览可用性（WebP 解码前提）")
    print("=" * 78)
    for aid in (matched_expected.values() if matched_expected else []):
        a = after_assets[aid]
        sha = a["sha"]
        code, body, hdrs = http_bytes(f"/thumbs/{sha}.webp")
        is_webp = body[:4] == b"RIFF" and body[8:12] == b"WEBP"
        pcode, pbody, phdrs = http_bytes(f"/preview/{aid}")
        print(f"  {a['origin']:<24} thumbs HTTP {code} {len(body):>7}B webp={is_webp} "
              f"| preview HTTP {pcode} {len(pbody):>8}B {phdrs.get('Content-Type','')}")
        if code != 200 or not is_webp:
            warns.append(f"{a['origin']} 缩略图异常（HTTP {code}, webp={is_webp}）")
        if pcode != 200:
            warns.append(f"{a['origin']} preview 异常（HTTP {pcode}，视频已知 500，UI 回退缩略图）")

    print()
    print("=" * 78)
    print("5) 安全断言：新增资产必须 100% 命中测试信号")
    print("=" * 78)
    if suspicious:
        print(f"  ✘ 有 {len(suspicious)} 条新增资产**不属于测试素材**：")
        for aid, a in suspicious:
            print(f"     {aid} origin={a['origin']!r} kind={a['kind']} bytes={a['bytes']}")
            fails.append(f"同步把一个非测试资产推上了服务器：{aid} origin={a['origin']}")
    else:
        print(f"  ✔ 新增的 {len(new_ids)} 条全部命中测试信号（origin 前缀 / sha256 / PhotoKit 期望）")
    if gone_ids:
        warns.append(f"同步过程中有 {len(gone_ids)} 条原有资产消失（不应发生）：{gone_ids[:5]}")

    print()
    print("=" * 78)
    print("结论")
    print("=" * 78)
    for w in warns:
        print(f"  ! WARN  {w}")
    if fails:
        for f in fails:
            print(f"  ✘ FAIL  {f}")
        print(f"\n结果：FAIL（{len(fails)} 项）")
    else:
        print(f"  ✔ 同步结果与 PhotoKit 期望一致；count {before_stats['count']}→{after_stats['count']}"
              f"（{after_stats['count']-before_stats['count']:+d}）")
        print("\n结果：PASS")

    if args.out:
        Path(args.out).write_text(json.dumps({
            "before_stats": before_stats, "after_stats": after_stats,
            "new_ids": new_ids, "gone_ids": gone_ids,
            "expected": expected, "matched": matched_expected,
            "suspicious": [{"id": i, **a} for i, a in suspicious],
            "fails": fails, "warns": warns,
        }, ensure_ascii=False, indent=2))
        print(f"结果 → {args.out}")
    return 0 if not fails else 1


def cmd_assert_clean(args) -> int:
    st = stats()
    fails = []
    print(f"服务器 {BASE}")
    print(f"当前 stats：count={st['count']} gallery={st['gallery']} material={st['material']} "
          f"bytes={st['bytes']} trash={st['trash']}")
    if args.baseline_count is not None and st["count"] != args.baseline_count:
        fails.append(f"count={st['count']} 未回到基线 {args.baseline_count}")
    if args.baseline_bytes is not None and st["bytes"] != args.baseline_bytes:
        fails.append(f"bytes={st['bytes']} 未回到基线 {args.baseline_bytes}")
    code, page = http_json(f"/api/assets?limit=300&offset=0&q={PREFIX.rstrip('-')}")
    left = [it for it in (page or {}).get("items", [])
            if (it.get("origin") or "").lower().startswith(PREFIX.lower())]
    print(f"服务器上残留 origin 前缀 {PREFIX} 的资产：{len(left)} 条")
    for it in left:
        print(f"  {it['id']} origin={it['origin']} kind={it['kind']}")
    if left:
        fails.append(f"还有 {len(left)} 条 iostest-* 资产没清掉")
    if fails:
        for f in fails:
            print(f"  ✘ FAIL  {f}")
        return 1
    print("  ✔ 已回到基线，且没有残留测试资产")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("snapshot", help="拍一张「同步前」全量快照")
    s.add_argument("--out", required=True)
    s.set_defaults(func=cmd_snapshot)

    v = sub.add_parser("verify", help="带上同步前快照核对同步结果")
    v.add_argument("--before", required=True)
    v.add_argument("--udid", default=DEFAULT_UDID)
    v.add_argument("--expect-no-new", action="store_true", help="断言没有任何新增（幂等验证）")
    v.add_argument("--out", default="")
    v.set_defaults(func=cmd_verify)

    c = sub.add_parser("assert-clean", help="断言服务器回到基线、无残留测试资产")
    c.add_argument("--baseline-count", type=int, default=None)
    c.add_argument("--baseline-bytes", type=int, default=None)
    c.set_defaults(func=cmd_assert_clean)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
