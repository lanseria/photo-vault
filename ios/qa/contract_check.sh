#!/usr/bin/env bash
# 阶段 A —— 不动 App，直接用 curl 验服务端「相册同步」契约。
#
# 只上传 ios/qa/seed/ 里 iostest-* 的测试素材，绝不碰服务器上任何其它资产。
#
#   bash ios/qa/contract_check.sh            # 跑全部检查，证据写 ios/qa/evidence/
#   bash ios/qa/contract_check.sh --cleanup  # 跑完顺手调用 cleanup（默认不删，留证据）
#
# 覆盖：upload(imported) / upload(duplicate) / Live Photo 配对(先静帧后动态) /
#       顺序反了会怎样 / 缩略图是否 WebP / preview 与 /file 的字节一致性 / stats 增量。
set -uo pipefail

BASE="${PV_BASE:-http://bmc_desktop:7878}"
SEED="$(cd "$(dirname "$0")/seed" && pwd)"
EVID="$(cd "$(dirname "$0")" && pwd)/evidence"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$EVID/phase-a-contract-$STAMP.log"
mkdir -p "$EVID"
: > "$LOG"

say() { printf '%s\n' "$*" | tee -a "$LOG"; }
run() { # run <描述> <命令...>：命令输出同时进日志和 stdout
  say "── $*" >/dev/null
  say "\$ $*"
  "$@" 2>&1 | tee -a "$LOG"
}
JSON() { python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin), ensure_ascii=False, indent=2))'; }
FIELD() { python3 -c 'import json,sys
try: d=json.load(sys.stdin)
except Exception: print(""); sys.exit(0)
for k in sys.argv[1].split("."):
    if isinstance(d,list): d=d[int(k)]
    elif isinstance(d,dict): d=d.get(k)
    else: d=None
print("" if d is None else d)' "$1"; }

# 上传一个文件：upload <文件> <batch> ；stdout 打响应 JSON，同时把 body 存到 $OUT
upload() {
  local f="$1" batch="$2" name
  name="$(basename "$f")"
  local url="$BASE/api/upload?name=$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))' "$name")&library=gallery&batch=$batch"
  say "\$ curl -X POST '$url' --data-binary @$name"
  curl -sS -m 120 -X POST "$url" \
       -H 'Content-Type: application/octet-stream' \
       --data-binary "@$f" -o "$EVID/.resp.json" -w 'HTTP:%{http_code} bytes:%{size_upload}\n' | tee -a "$LOG"
  cat "$EVID/.resp.json" | JSON | tee -a "$LOG"
  cp "$EVID/.resp.json" "$EVID/upload-$(basename "$f")-$batch.json"
}

say "===== photo-vault 服务端契约检查 $(date '+%F %T %z') ====="
say "BASE=$BASE"
say "SEED=$SEED"

# 每次跑用**内容不同**的 curl 素材：免得和上一轮没清干净的资产撞哈希，
# 把 imported 断言误判成 duplicate。这两张不导入模拟器，只给 curl 用。
python3 - "$EVID" <<'PY' | tee -a "$LOG"
import os, sys, subprocess
from PIL import Image, ImageDraw
out = sys.argv[1]

def make_jpg(path, label, seed):
    w, h = 720, 540
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            px[x, y] = ((seed + x * 7) % 256, (seed * 3 + y * 5) % 256,
                        (seed * 7 + x + y) % 256)
    d = ImageDraw.Draw(im)
    d.text((20, 20), f"{label} seed={seed}", fill=(255, 255, 255))
    im.save(path, "JPEG", quality=84)
    return path

def make_mov(jpg, path, content_id):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-loop", "1", "-i", jpg, "-t", "2", "-vf", "scale=320:-2",
                    "-r", "24", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-movflags", "use_metadata_tags",
                    "-metadata", f"com.apple.quicktime.content.identifier={content_id}",
                    path], check=True)
    return path

# 每组 fixture 都用**随机**内容：上一轮没清干净时也不会撞哈希，
# 否则 imported 会被判成 duplicate、"顺序反了"根本走不到配对逻辑。
for label in ("curl", "live", "rev"):
    seed = int.from_bytes(os.urandom(3), "big")
    jpg = make_jpg(os.path.join(out, f"iostest-{label}.JPG"), f"iostest {label}", seed)
    if label != "curl":
        make_mov(jpg, os.path.join(out, f"iostest-{label}.MOV"),
                 "-".join(f"{int.from_bytes(os.urandom(2), 'big'):04X}" for _ in range(4)))
for f in sorted(os.listdir(out)):
    if f.startswith("iostest-"):
        print(f"fixture: {f} bytes={os.path.getsize(os.path.join(out, f))}")
PY

# ---------------------------------------------------------------- 0. 基线
say ""
say "== 0. 基线 =="
say "\$ curl $BASE/api/stats"
curl -sS -m 15 "$BASE/api/stats" -o "$EVID/.stats-before.json" -w 'HTTP:%{http_code}\n' | tee -a "$LOG"
python3 -c 'import json;d=json.load(open("'"$EVID"'/.stats-before.json"));print({k:d[k] for k in ("count","gallery","material","bytes","trash")})' | tee -a "$LOG"

BATCH_A="qa-curl-${STAMP}"
BATCH_LIVE="qa-live-${STAMP}"

# ---------------------------------------------------------------- 1. 首次上传
say ""
say "== 1. 上传 iostest-curl.jpg（batch=${BATCH_A}）→ 期望 status=imported =="
upload "$EVID/iostest-curl.jpg" "$BATCH_A"
ID1="$(cat "$EVID/.resp.json" | FIELD asset.id)"
STATUS1="$(cat "$EVID/.resp.json" | FIELD status)"
KIND1="$(cat "$EVID/.resp.json" | FIELD asset.kind)"
ORIGIN1="$(cat "$EVID/.resp.json" | FIELD asset.origin)"
SHA1="$(cat "$EVID/.resp.json" | FIELD asset.sha)"
say "→ status=$STATUS1 kind=$KIND1 origin=$ORIGIN1 id=$ID1"

# ---------------------------------------------------------------- 2. 重复上传
say ""
say "== 2. 同内容再传一次（同名同 batch）→ 期望 status=duplicate，asset.id 不变 =="
upload "$EVID/iostest-curl.jpg" "$BATCH_A"
ID1B="$(cat "$EVID/.resp.json" | FIELD asset.id)"
STATUS2="$(cat "$EVID/.resp.json" | FIELD status)"
say "→ status=$STATUS2 id=${ID1B}（首次 id=${ID1}）"

# 换个名字、同内容再传：内容寻址应该也判 duplicate
say ""
say "== 2b. 同内容但换文件名 iostest-curl-renamed.jpg → 内容哈希去重应仍为 duplicate =="
cp "$EVID/iostest-curl.jpg" "$EVID/iostest-curl-renamed.jpg"
upload "$EVID/iostest-curl-renamed.jpg" "$BATCH_A"
STATUS2B="$(cat "$EVID/.resp.json" | FIELD status)"
say "→ status=$STATUS2B"

# ---------------------------------------------------------------- 2c. HEIC / PNG
say ""
say "== 2c. HEIC / PNG 上传（App 从 PhotoKit 推的就是 HEIC）=="
BATCH_FMT="qa-fmt-${STAMP}"
upload "$SEED/iostest-d.heic" "$BATCH_FMT"
ID_HEIC="$(cat "$EVID/.resp.json" | FIELD asset.id)"
SHA_HEIC="$(cat "$EVID/.resp.json" | FIELD asset.sha)"
say "→ HEIC: status=$(cat "$EVID/.resp.json" | FIELD status) kind=$(cat "$EVID/.resp.json" | FIELD asset.kind) id=$ID_HEIC"
say "-- HEIC 的缩略图（WebP）/ 预览（HEIC 服务端要转 JPEG）--"
curl -sS -m 30 "$BASE/thumbs/$SHA_HEIC.webp" -o "$EVID/thumb-heic.webp" -D "$EVID/.h1" \
     -w 'thumbs HTTP:%{http_code} bytes:%{size_download}\n' | tee -a "$LOG"
grep -i '^content-type' "$EVID/.h1" | tee -a "$LOG"
curl -sS -m 60 "$BASE/preview/$ID_HEIC" -o "$EVID/preview-heic.bin" -D "$EVID/.h2" \
     -w 'preview HTTP:%{http_code} bytes:%{size_download}\n' | tee -a "$LOG"
grep -i '^content-type' "$EVID/.h2" | tee -a "$LOG"
python3 - "$EVID/preview-heic.bin" <<'PY' | tee -a "$LOG"
import sys
b = open(sys.argv[1], 'rb').read()
print(f"preview magic={b[:4]!r} → " + ("JPEG ✔（HEIC 已转码）" if b[:2] == b'\xff\xd8' else "不是 JPEG ✘"))
PY
upload "$SEED/iostest-e.png" "$BATCH_FMT"
ID_PNG="$(cat "$EVID/.resp.json" | FIELD asset.id)"
say "→ PNG: status=$(cat "$EVID/.resp.json" | FIELD status) kind=$(cat "$EVID/.resp.json" | FIELD asset.kind) id=$ID_PNG"

# ---------------------------------------------------------------- 3. Live Photo：先静帧后动态
say ""
say "== 3. Live Photo（batch=${BATCH_LIVE}）：先 iostest-live.JPG 再 iostest-live.MOV → 期望第二条 kind=live_photo =="
upload "$EVID/iostest-live.JPG" "$BATCH_LIVE"
ID_STILL="$(cat "$EVID/.resp.json" | FIELD asset.id)"
KIND_STILL="$(cat "$EVID/.resp.json" | FIELD asset.kind)"
say "→ 静帧: status=$(cat "$EVID/.resp.json" | FIELD status) kind=$KIND_STILL id=$ID_STILL"

upload "$EVID/iostest-live.MOV" "$BATCH_LIVE"
ID_MOTION="$(cat "$EVID/.resp.json" | FIELD asset.id)"
KIND_MOTION="$(cat "$EVID/.resp.json" | FIELD asset.kind)"
ORIGIN_MOTION="$(cat "$EVID/.resp.json" | FIELD asset.origin)"
say "→ 动态: status=$(cat "$EVID/.resp.json" | FIELD status) kind=$KIND_MOTION origin=$ORIGIN_MOTION id=$ID_MOTION"

# 合成后应该只剩一条资产（动态被挂成 live_motion 角色）
say ""
say "-- 3b. 合成结果：GET /api/assets/{静帧 id} --"
curl -sS -m 15 "$BASE/api/assets/$ID_STILL" -o "$EVID/.live.json" -w 'HTTP:%{http_code}\n' | tee -a "$LOG"
cat "$EVID/.live.json" | JSON | tee -a "$LOG"
KIND_LIVE="$(cat "$EVID/.live.json" | FIELD kind)"

# ---------------------------------------------------------------- 4. 顺序反了
say ""
say "== 4. 顺序反了：先 MOV 再同一 stem 的 JPG → README 说会变成独立视频 =="
BATCH_REV="qa-rev-${STAMP}"
upload "$EVID/iostest-rev.MOV" "$BATCH_REV"
REV_MOV_ID="$(cat "$EVID/.resp.json" | FIELD asset.id)"
REV_MOV_KIND="$(cat "$EVID/.resp.json" | FIELD asset.kind)"
upload "$EVID/iostest-rev.JPG" "$BATCH_REV"
REV_JPG_ID="$(cat "$EVID/.resp.json" | FIELD asset.id)"
REV_JPG_KIND="$(cat "$EVID/.resp.json" | FIELD asset.kind)"
say "→ 先传 MOV: kind=$REV_MOV_KIND id=${REV_MOV_ID}；后传 JPG: kind=$REV_JPG_KIND id=$REV_JPG_ID"

# ---------------------------------------------------------------- 5. 缩略图 / 预览 / 原件
say ""
say "== 5. 缩略图 / 预览 / 原件 =="
say "-- 5a. GET /thumbs/{sha}.webp --"
curl -sS -m 30 "$BASE/thumbs/$SHA1.webp" -o "$EVID/thumb-$SHA1.webp" \
     -D "$EVID/.thumb.hdr" -w 'HTTP:%{http_code} bytes:%{size_download}\n' | tee -a "$LOG"
grep -i '^content-type' "$EVID/.thumb.hdr" | tee -a "$LOG"
python3 - "$EVID/thumb-$SHA1.webp" <<'PY' | tee -a "$LOG"
import sys
p = sys.argv[1]
b = open(p, 'rb').read()
print(f"magic={b[:4]!r}/{b[8:12]!r} bytes={len(b)} → " + ("WebP ✔" if b[:4]==b'RIFF' and b[8:12]==b'WEBP' else "不是 WebP ✘"))
PY
say "-- 5b. GET /preview/{id} --"
curl -sS -m 60 "$BASE/preview/$ID1" -o "$EVID/preview-$ID1.jpg" -D "$EVID/.prev.hdr" \
     -w 'HTTP:%{http_code} bytes:%{size_download}\n' | tee -a "$LOG"
grep -i '^content-type' "$EVID/.prev.hdr" | tee -a "$LOG"
say "-- 5c. GET /file/{id}（原件字节应与本地 sha256 一致）--"
curl -sS -m 60 "$BASE/file/$ID1" -o "$EVID/file-$ID1.bin" -w 'HTTP:%{http_code} bytes:%{size_download}\n' | tee -a "$LOG"
python3 - "$EVID/file-$ID1.bin" "$EVID/iostest-curl.jpg" <<'PY' | tee -a "$LOG"
import hashlib, sys
a = hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest()
b = hashlib.sha256(open(sys.argv[2],'rb').read()).hexdigest()
print(f"server_file_sha={a}\nlocal_source_sha={b}\n{'一致 ✔' if a==b else '不一致 ✘'}")
PY
say "-- 5d. Live Photo 的实况视频 /file/{id}?role=live_motion --"
curl -sS -m 60 "$BASE/file/$ID_STILL?role=live_motion" -o "$EVID/file-$ID_STILL-live_motion.mov" \
     -w 'HTTP:%{http_code} bytes:%{size_download}\n' | tee -a "$LOG"

# ---------------------------------------------------------------- 6. 查询 & 统计
say ""
say "== 6. 查询可见性 =="
say "-- 6a. GET /api/assets?limit=300&q=iostest --"
curl -sS -m 30 "$BASE/api/assets?limit=300&offset=0&q=iostest" -o "$EVID/.q.json" -w 'HTTP:%{http_code}\n' >/dev/null
python3 - "$EVID/.q.json" <<'PY' | tee -a "$LOG"
import json, sys
d = json.load(open(sys.argv[1]))
print(f"total={d['total']}")
for it in d['items']:
    print(f"  id={it['id']} origin={it['origin']:<28} kind={it['kind']:<11} "
          f"bytes={it['bytes']:<8} has_thumb={it['has_thumb']} sha={it['sha'][:12]}")
PY
say ""
say "== 7. 统计增量 =="
curl -sS -m 15 "$BASE/api/stats" -o "$EVID/.stats-after.json" -w 'HTTP:%{http_code}\n' >/dev/null
python3 - "$EVID/.stats-before.json" "$EVID/.stats-after.json" <<'PY' | tee -a "$LOG"
import json, sys
b = json.load(open(sys.argv[1])); a = json.load(open(sys.argv[2]))
print(f"{'字段':<10}{'前':>14}{'后':>14}{'差':>8}")
for k in ("count","gallery","material","bytes","trash"):
    print(f"{k:<10}{b[k]:>14}{a[k]:>14}{a[k]-b[k]:>+8}")
PY

say ""
say "== 8. 本次上传产生的 iostest 资产 id（供清理核对）=="
for pair in "curl:$ID1" "heic:$ID_HEIC" "png:$ID_PNG" "live_still:$ID_STILL" \
            "rev_mov:$REV_MOV_ID" "rev_jpg:$REV_JPG_ID"; do
  say "  ${pair%%:*}=${pair##*:}"
done
say ""
say "结论速览：status1=${STATUS1}（期望 imported）status2=${STATUS2}（期望 duplicate）"
say "          live_photo: 先静帧后动态 → kind=${KIND_LIVE}（期望 live_photo）"
say "          顺序反了 → mov_kind=$REV_MOV_KIND / jpg_kind=$REV_JPG_KIND"
say "          日志：$LOG"

if [ "${1:-}" = "--cleanup" ]; then
  say ""
  say "== 9. 清理本次上传的测试资产 =="
  python3 "$(dirname "$0")/../tools/cleanup_test_assets.py" --yes --ids \
    "$ID1,$ID_HEIC,$ID_PNG,$ID_STILL,$REV_MOV_ID,$REV_JPG_ID" 2>&1 | tee -a "$LOG"
fi

rm -f "$EVID/.resp.json" "$EVID/.q.json" "$EVID/.live.json" "$EVID/.stat?*.json" "$EVID/.thumb.hdr" "$EVID/.prev.hdr"
echo
echo "完整日志：$LOG"
