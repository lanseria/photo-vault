"""影视库：按目录登记电影 / 剧集 / 字幕。

设计边界（和相册正好相反）：
  · **不搬文件、不算哈希、不跑 AI。** 一部电影 10–50 GB，硬链接只能同卷，
    拷贝等于把用户的硬盘复制一遍。影视文件留在原地，库里只记路径和元数据。
  · 路径存 (root_key, rel_path)，不存绝对路径：换挂载点只改 env（MEDIA_ROOT /
    MEDIA_HOST_ROOT），不用改库。
  · 识别靠文件名 + 目录结构（S01E02 / 第01集 / Season 1 / 01.mkv），
    元数据靠 ffprobe，海报靠 ffmpeg 抽一帧。全部是确定性规则，没有模型。
  · 字幕：扫描同目录 sidecar，记语言/格式；播放时 SRT 转 WebVTT。
    完整的字幕下载/匹配以后再做，表结构不用变。

单独一个模块（不塞进 ingest.py）：相册的入库是"内容不可变 + 幂等哈希"，
影视是"路径登记 + 可重扫更新"，两者的事务边界和失败语义都不一样。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from .config import Config

VIDEO_EXT = {
    ".mkv", ".mp4", ".m4v", ".mov", ".avi", ".wmv", ".flv", ".webm",
    ".ts", ".m2ts", ".mpg", ".mpeg", ".rmvb", ".rm", ".3gp", ".ogv",
}
SUB_EXT = {".srt", ".ass", ".ssa", ".vtt", ".sub"}

# 发布名里的噪声词：分辨率 / 片源 / 编码 / 音轨 / 版本 / 字幕组
# 注意：只收"几乎不可能出现在片名里"的词。web / complete / extended 这类会误伤
# （"Web of Lies"、"The Extended Family"），宁可不删也不要把片名啃掉。
_RELEASE_RE = re.compile(
    r"""(?ix)(?<![a-z0-9])(
        2160p|1080p|1080i|720p|576p|480p|4k|uhd|fhd|hdrip|hdtv|hdvd
      | blu-?ray|bdrip|brrip|bdremux|remux|web-?dl|webrip
      | dvdrip|dvd|xvid|divx
      | x264|x265|h\.?264|h\.?265|hevc|avc|av1|vp9
      | 10bit|8bit|hi10p
      | hdr10\+?|hdr|dolby\.?vision
      | dts(?:-?hd)?|truehd|atmos|eac3|ac3|aac(?:\d(?:\.\d)?)?|flac|opus
      | ddp?\d\.\d|dd\+?\d\.\d|dd[257]
      | proper|repack|internal|remastered|dubbed|subbed|amzn|dsnp|hmax|atvp|nf
      | yts|yify|rarbg|torrent
    )(?![a-z0-9])"""
)

# 语种 / 字幕类型标记（出现在标题里是噪声；真正的字幕语言另走 subtitle_meta）。
# ASCII 标记必须有词边界，否则 "Avengers" 里的 "eng" 会被当成英语删掉。
_LANG_CN_RE = re.compile(
    r"中英双字|中英|国语|粤语|普通话|日语|韩语|英语|法语|德语"
    r"|中字|简繁|简体|繁体|双语|内嵌|外挂|无字|国配|原声"
)
_LANG_ASCII_RE = re.compile(
    r"(?ix)(?<![a-z0-9])(?:dual[\s._-]?audio|chs|cht|eng|jpn|kor)(?![a-z0-9])"
)
# DTS.5.1 / AAC.7.1 删掉音轨名后残下的 "5.1"
_CHANNELS_RE = re.compile(r"(?<![\d.])[1-9]\.1(?![\d.])")

_YEAR_RE = re.compile(r"(?<!\d)(19[0-9]{2}|20[0-2][0-9])(?!\d)")
_PAREN_YEAR_RE = re.compile(r"[\(\[]((?:19|20)[0-9]{2})[\)\]]")

# 集标记：S01E02 / 1x02 / Season 1 Episode 2 / 第01集
_SE_RE = re.compile(r"(?<![a-z0-9])s(\d{1,2})[\s._-]*e(\d{1,3})(?![0-9])", re.I)
_NXNN_RE = re.compile(r"(?<![a-z0-9])(\d{1,2})x(\d{2,3})(?![0-9])", re.I)
_SEASON_EP_RE = re.compile(
    r"season[\s._-]*(\d{1,2})[\s._-]*(?:episode|ep)[\s._-]*(\d{1,3})", re.I)
_CN_EP_RE = re.compile(r"第\s*(\d{1,4})\s*[集话話期]")

_SEASON_DIR_RE = re.compile(
    r"(?i)(?:^|[\s._\-])(?:season|s)\s*0*(\d{1,2})(?:$|[\s._\-])")
_CN_SEASON_RE = re.compile(r"第\s*([0-9一二三四五六七八九十]+)\s*季")
_SPECIAL_DIRS = {"specials", "special", "特别篇", "sp", "ova"}

_BARE_EP_RE = re.compile(r"^(\d{1,4})(?:v\d+)?(?:[\s._-]+(.*))?$")
_EP_PREFIX_RE = re.compile(r"^ep?[\s._-]*(\d{1,4})(?:v\d+)?(?=$|[\s._-])", re.I)
_EP_ANY_RE = re.compile(r"(?<![a-z0-9])ep[\s._-]*(\d{1,4})(?![0-9])", re.I)
_ANIME_RE = re.compile(
    r"^\[[^\]]+\]\s*(?P<title>.+?)\s*[-–—]\s*(?P<ep>\d{1,4})(?:v\d+)?(?:\s|\[|\(|\.|$)",
    re.I)
_DASH_EP_RE = re.compile(r"^(.+?)\s+[-–—]\s+(\d{1,4})(?:v\d+)?(?:\s|\[|\(|\.|$)")

_MULTI_SPACE = re.compile(r"\s+")
_EDGE_JUNK = " -–—–·•.,:;|/\\+&~"


# ---------------------------------------------------------------- 文本工具

def title_key(title: str) -> str:
    """归并键：去大小写 / 空格 / 标点，保留所有语言的字母数字。"""
    s = unicodedata.normalize("NFKC", title or "").lower()
    return "".join(ch for ch in s if ch.isalnum()) or "untitled"


def _collapse(text: str) -> str:
    return _MULTI_SPACE.sub(" ", text).strip(_EDGE_JUNK + " ")


def clean_title(text: str) -> str:
    """去掉发布名里的噪声，留下人能读的片名。"""
    s = (text or "").replace("\u3000", " ")
    s = re.sub(r"[\[\{][^\]\}]*[\]\}]", " ", s)       # [Group] / {edition}
    s = _RELEASE_RE.sub(" ", s)                       # 1080p / BluRay / x264
    s = _LANG_CN_RE.sub(" ", s)                       # 国语 / 中字 / 简繁
    s = _LANG_ASCII_RE.sub(" ", s)                    # chs / eng / dual audio
    s = _CHANNELS_RE.sub(" ", s)                      # 5.1 / 7.1
    s = re.sub(r"[\(\)]", " ", s)                     # (2019) 里的括号本身
    s = s.replace("_", " ").replace(".", " ")
    s = re.sub(r"(?i)\bseason\s*\d{1,2}\b", " ", s)
    s = re.sub(r"第\s*[0-9一二三四五六七八九十]+\s*季", " ", s)
    s = re.sub(r"(?i)(?<![a-z0-9])s\d{1,2}(?![a-z0-9])", " ", s)   # 文件夹名里的 S01
    s = re.sub(r"\s+-{1,2}[A-Za-z0-9@.]{2,18}\s*$", " ", s)   # 末尾 -GROUP
    return _collapse(s)


def _split_year(text: str, fallback_year: int | None = None
                ) -> tuple[str, int | None]:
    """从片名里拿出年份并从标题里删掉。开头就是年份的不当成年份（1917 / 2012）。"""
    raw = text or ""
    year: int | None = None
    m = None
    for m in _PAREN_YEAR_RE.finditer(raw):
        pass
    if m:
        year = int(m.group(1))
        raw = raw[:m.start()] + " " + raw[m.end():]
    else:
        matches = [mm for mm in _YEAR_RE.finditer(raw) if mm.start() > 0]
        if matches:
            mm = matches[-1]
            year = int(mm.group(1))
            raw = raw[:mm.start()] + " " + raw[mm.end():]
    return _collapse(clean_title(raw)), (year if year is not None else fallback_year)


def _cn_number(s: str) -> int | None:
    if s.isdigit():
        return int(s)
    digits = "零一二三四五六七八九"
    if s == "十":
        return 10
    if "十" in s:
        a, _, b = s.partition("十")
        tens = 1 if a == "" else (digits.index(a) if a in digits and len(a) == 1 else None)
        ones = 0 if b == "" else (digits.index(b) if b in digits and len(b) == 1 else None)
        if tens is None or ones is None:
            return None
        return tens * 10 + ones
    if len(s) == 1 and s in digits:
        return digits.index(s)
    return None


def season_from_dir(name: str) -> int | None:
    """目录名是不是季目录：Season 1 / S02 / 第二季 / Specials。"""
    low = (name or "").strip().lower()
    if low in _SPECIAL_DIRS:
        return 0
    m = _SEASON_DIR_RE.search(name or "")
    if m:
        return int(m.group(1))
    m = _CN_SEASON_RE.search(name or "")
    if m:
        return _cn_number(m.group(1))
    return None


# ---------------------------------------------------------------- 文件名解析

@dataclass
class ParsedName:
    kind: str                      # movie | series
    title: str
    year: int | None
    season: int | None
    episode: int | None
    episode_title: str | None
    series_root: Path | None       # 剧集根目录（剧集才有），用于「重新扫描」
    confidence: str = "pattern"    # pattern | folder | fallback —— 排障用


def _episode_marker(stem: str) -> tuple[int | None, int, int, int] | None:
    """返回 (season|None, episode, start, end)。"""
    for rx in (_SE_RE, _NXNN_RE, _SEASON_EP_RE):
        m = rx.search(stem)
        if m:
            return int(m.group(1)), int(m.group(2)), m.start(), m.end()
    m = _CN_EP_RE.search(stem)
    if m:
        return None, int(m.group(1)), m.start(), m.end()
    return None


def _series_from_marker(path: Path, stem: str, m: tuple[int | None, int, int, int],
                        season_dir: int | None, folder_title: str,
                        folder_year: int | None) -> ParsedName:
    season, episode, start, end = m
    title, year = _split_year(stem[:start], fallback_year=folder_year)
    if not title:
        title, year = folder_title, folder_year
    ep_title = clean_title(stem[end:])
    # 集名只保留"像人话"的部分：只剩一个发布组/年份/纯数字就不要
    if not ep_title or _YEAR_RE.fullmatch(ep_title) or len(ep_title) < 2:
        ep_title = None
    return ParsedName(
        kind="series", title=title or folder_title or "未命名",
        year=year,
        season=season if season is not None else (season_dir if season_dir is not None else 1),
        episode=episode,
        episode_title=ep_title,
        series_root=path.parent.parent if season_dir is not None else path.parent,
        confidence="pattern",
    )


def _series_from_folder(path: Path, stem: str, episode: int,
                        episode_title: str | None, season_dir: int | None,
                        folder_title: str, folder_year: int | None,
                        confidence: str = "folder") -> ParsedName:
    return ParsedName(
        kind="series", title=folder_title or clean_title(stem) or "未命名",
        year=folder_year,
        season=season_dir if season_dir is not None else 1,
        episode=episode,
        episode_title=episode_title,
        series_root=path.parent.parent if season_dir is not None else path.parent,
        confidence=confidence,
    )


def parse_name(path: Path, forced: str = "auto") -> ParsedName:
    """把一个视频文件解析成 电影 / 剧集 的（片名, 年份, 季, 集, 集名）。

    forced: auto（默认）| movie | series。auto 只在有明确证据时才认剧集：
      · S01E02 / 1x02 / Season 1 Episode 2 / 第01集
      · 季目录里的纯数字文件名（Show/Season 2/03.mkv）
      · 番剧式 "[Group] Show - 05"
    宁可把剧集认成电影（用户能手动改），也不要把电影拆成几百集。
    """
    stem = path.stem
    parent = path.parent
    season_dir = season_from_dir(parent.name)
    show_dir = parent.parent if season_dir is not None else parent
    folder_title, folder_year = _split_year(show_dir.name)

    if forced == "movie":
        title, year = _split_year(stem, fallback_year=folder_year)
        if not title:
            title, year = folder_title, folder_year
        return ParsedName("movie", title or "未命名", year,
                          0, 0, None, None, "forced")

    marker = _episode_marker(stem)
    if marker:
        return _series_from_marker(path, stem, marker, season_dir,
                                   folder_title, folder_year)

    # 番剧：[Group] Show - 05
    m = _ANIME_RE.search(stem)
    if m and m.group("title").strip():
        return ParsedName(
            kind="series", title=clean_title(m.group("title")),
            year=folder_year,
            season=season_dir if season_dir is not None else 1,
            episode=int(m.group("ep")),
            episode_title=None,
            series_root=path.parent.parent if season_dir is not None else path.parent,
            confidence="anime",
        )

    in_series_dir = season_dir is not None or forced == "series"

    # 季目录里的 03.mkv / 03 - 集名.mkv
    if in_series_dir:
        m = _BARE_EP_RE.match(stem)
        if m:
            ep_title = clean_title(m.group(2) or "")
            return _series_from_folder(
                path, stem, int(m.group(1)), ep_title or None, season_dir,
                folder_title, folder_year)

    # EP01 / E01 这种前缀（季目录里也接受出现在中间的 ep）
    m = _EP_PREFIX_RE.match(stem) or (_EP_ANY_RE.search(stem) if in_series_dir else None)
    if m:
        rest_start = m.end()
        return _series_from_folder(
            path, stem, int(m.group(1)), clean_title(stem[rest_start:]) or None,
            season_dir, folder_title, folder_year, confidence="ep-prefix")

    # Show - 05（只在季目录 / 明确 series 时才认，避免 "Baraka - 01" 被拆）
    if in_series_dir:
        m = _DASH_EP_RE.match(stem)
        if m:
            return _series_from_folder(
                path, stem, int(m.group(2)), None, season_dir,
                folder_title, folder_year, confidence="dash")

    title, year = _split_year(stem, fallback_year=folder_year)
    if not title:
        title, year = folder_title, folder_year
    return ParsedName("movie", title or "未命名", year, 0, 0, None, None, "fallback")


# ---------------------------------------------------------------- 字幕

@dataclass
class SubtitleFile:
    path: Path
    lang: str | None
    label: str
    forced: bool


_SUB_LANG_RULES: list[tuple[tuple[str, ...], str, str]] = [
    (("zh-hant", "cht", "hant", "big5", "繁", "tc"), "zh-TW", "繁體中文"),
    (("zh-hans", "chs", "hans", "简", "sc", "gb", "cn"), "zh-CN", "简体中文"),
    (("zh", "chi", "chn", "zho", "中文", "中字", "chinese"), "zh-CN", "中文"),
    (("en", "eng", "english", "英语", "英文"), "en", "English"),
    (("ja", "jpn", "jp", "日语", "日文", "japanese"), "ja", "日本語"),
    (("ko", "kor", "kr", "韩语", "韩文", "korean"), "ko", "한국어"),
    (("fr", "fre", "fra", "法语"), "fr", "Français"),
    (("de", "ger", "deu", "德语"), "de", "Deutsch"),
    (("es", "spa", "西班牙语"), "es", "Español"),
    (("ru", "rus", "俄语"), "ru", "Русский"),
    (("it", "ita", "意大利语"), "it", "Italiano"),
    (("pt", "por", "葡萄牙语"), "pt", "Português"),
    (("th", "tha", "泰语"), "th", "ไทย"),
    (("vi", "vie", "越南语"), "vi", "Tiếng Việt"),
]


def subtitle_meta(suffix: str, filename: str) -> tuple[str | None, str, bool]:
    """从字幕文件名里推断语言：Movie.zh-Hans.forced.srt → ('zh-CN','简体中文',True)。"""
    s = (suffix or "").lower()
    tokens = set(re.findall(r"[a-z0-9]+", s))
    forced = "forced" in tokens or "强制" in s
    if "中英" in s or {"zh", "en"} <= tokens or {"chs", "eng"} <= tokens:
        return "zh-CN", "中英双语", forced
    for keys, lang, label in _SUB_LANG_RULES:
        if any((k in tokens) if k.isascii() else (k in s) for k in keys):
            return lang, label, forced
    return None, "字幕", forced


def subtitle_meta_from_name(stem: str) -> tuple[str | None, str, bool]:
    """只看文件名末尾几段判断语言。标题里的 "English Patient" 不会被误判成英文字幕。"""
    parts = [x for x in re.split(r"[.\s_\-\[\]()]+", (stem or "").lower()) if x]
    return subtitle_meta(".".join(parts[-3:]), stem or "")


# 字幕文件名里的语言/类型标记。汉字标记在 _LANG_CN_RE 里，这里只收 ASCII token。
_SUB_TOKEN_RE = re.compile(
    r"""(?ix)(?<![a-z0-9])(?:
        zh(?:[-_.]?(?:hans|hant|cn|tw|sg))?
      | chs|cht|eng|english|en|jpn|japanese|ja|kor|korean|ko
      | forced|sdh|cc|default
    )(?![a-z0-9])"""
)


def _core_key(stem: str) -> str:
    """去掉发布信息、语言标记后的"片名骨架"，用来跨发布组匹配字幕。"""
    s = _SUB_TOKEN_RE.sub(" ", stem or "")
    s = _RELEASE_RE.sub(" ", s)
    s = _LANG_CN_RE.sub(" ", s)
    s = re.sub(r"[._]+", " ", s)
    # 去掉末尾发行组：Movie.1080p.x264-GROUP 里的 -GROUP（前面得是空白，
    # 否则 Spider-Man 的 -Man 会被当成组名）
    s = re.sub(r"\s+-{1,2}[A-Za-z0-9@.]{2,18}\s*$", " ", s)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", s.lower())


def _is_subtitle_for(video_stem: str, name: str) -> bool:
    """同目录字幕：优先同名（含语言后缀），再退回"骨架"匹配。

    真实字幕经常不带发布组标签：`Movie.zh-CN.srt` 旁边是
    `Movie.1080p.BluRay.x264-GROUP.mkv`。只按前缀比会漏掉这种最常见的命名。
    骨架相等就认；一方比另一方多一个年份（Movie.srt vs Movie.2019.mkv）也认。
    """
    v = video_stem.lower()
    n = Path(name).stem.lower()
    if n.startswith(v):
        rest = n[len(v):].strip(" ._-([")
        if not rest:
            return True
        # 只认 "语言/forced" 后缀，避免 Movie.mkv 把 Movie.2.srt 这种也吃进来
        lang, _label, forced = subtitle_meta(rest, name)
        if lang is not None or forced:
            return True
    vk, nk = _core_key(video_stem), _core_key(Path(name).stem)
    if not vk or not nk:
        return False
    if vk == nk:
        return True
    shorter, longer = (vk, nk) if len(vk) <= len(nk) else (nk, vk)
    if len(shorter) < 4 or not longer.startswith(shorter):
        return False
    extra = longer[len(shorter):]
    return bool(re.fullmatch(r"(?:19|20)\d{2}", extra))


def find_subtitles(video: Path) -> list[SubtitleFile]:
    """找视频旁边的字幕。只认同名前缀 + 可选语言后缀，不猜别的目录。"""
    out: list[SubtitleFile] = []
    seen: set[Path] = set()
    dirs = [video.parent]
    for name in ("Subs", "subs", "Sub", "sub", "字幕"):
        d = video.parent / name
        if d.is_dir():
            dirs.append(d)
    for d in dirs:
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if not p.is_file() or p.suffix.lower() not in SUB_EXT:
                continue
            if p in seen or not _is_subtitle_for(video.stem, p.name):
                continue
            seen.add(p)
            lang, label, forced = subtitle_meta_from_name(p.stem)
            out.append(SubtitleFile(p, lang, label, forced))
    out.sort(key=lambda s: (s.forced, s.label == "字幕", s.label))
    return out


def read_text_auto(path: Path) -> str:
    """字幕编码兜底：UTF-8(BOM) → GB18030 → Latin-1。中文 SRT 八成是这两种。"""
    data = path.read_bytes()
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", "ignore")


def srt_to_vtt(text: str) -> str:
    """SRT → WebVTT。浏览器只认 VTT；这一步只在播放时做，不改原文件。"""
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\{\\[^}]*\}", "", text)                       # ASS 定位标签
    text = re.sub(r"(\d{1,2}:\d{2}:\d{2}),(\d{1,3})", r"\1.\2", text)
    text = text.strip()
    return "WEBVTT\n\n" + (text + "\n" if text else "")


# ---------------------------------------------------------------- 探测 / 海报

def probe(path: Path) -> dict[str, Any]:
    """ffprobe → 时长 / 分辨率 / 编码 / 码率。失败返回空 dict，不阻断登记。"""
    if not shutil.which("ffprobe"):
        return {}
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, timeout=90, check=False)
        if out.returncode != 0:
            return {}
        info = json.loads(out.stdout or b"{}")
    except Exception:
        return {}
    fmt = info.get("format") or {}
    video = next((s for s in info.get("streams") or []
                  if s.get("codec_type") == "video"), None) or {}
    meta: dict[str, Any] = {
        "width": video.get("width"),
        "height": video.get("height"),
        "codec": video.get("codec_name"),
        "bit_rate": int(fmt["bit_rate"]) if str(fmt.get("bit_rate") or "").isdigit() else None,
    }
    try:
        if fmt.get("duration"):
            meta["duration_ms"] = int(float(fmt["duration"]) * 1000)
    except (TypeError, ValueError):
        pass
    return meta


def make_poster(src: Path, dst: Path, duration_ms: int | None = None) -> bool:
    """抽一帧做 2:3 竖版封面：整帧居中 + 四周模糊填充，不把画面裁成一条缝。

    优先 15% 处（避开黑场片头），失败退回第一帧；模糊合成失败再退回简单缩放。
    """
    if not shutil.which("ffmpeg"):
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    seek = 5.0
    if duration_ms and duration_ms > 30_000:
        seek = min(duration_ms / 1000 * 0.15, 300.0)
    filters = [
        "split[a][b];"
        "[a]scale=600:900:force_original_aspect_ratio=increase,crop=600:900,gblur=sigma=18[bg];"
        "[b]scale=600:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2",
        "scale=600:-2",
    ]
    for vf in filters:
        for ss in (f"{seek:.1f}", "0"):
            try:
                r = subprocess.run(
                    ["ffmpeg", "-v", "error", "-y", "-ss", ss, "-i", str(src),
                     "-frames:v", "1", "-vf", vf, str(dst)],
                    capture_output=True, timeout=60, check=False)
                if r.returncode == 0 and dst.exists() and dst.stat().st_size > 1024:
                    return True
            except Exception:
                continue
    return False


# ---------------------------------------------------------------- 扫描

def _looks_like_sample(path: Path) -> bool:
    s = path.stem.lower()
    return bool(re.search(r"(?<![a-z])sample(?![a-z])", s)) or s.endswith("-sample")


def walk_videos(root: Path) -> Iterator[Path]:
    """遍历影视文件。跳过隐藏目录、Sample 目录和 sample 文件。"""
    root = root.resolve()
    if root.is_file():
        if root.suffix.lower() in VIDEO_EXT and not _looks_like_sample(root):
            yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith((".", "@", "#", "$")) and d.lower() not in
            {"sample", "samples", "sample-data", "__macosx", "system volume information"}
        )
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            p = Path(dirpath) / name
            if p.suffix.lower() in VIDEO_EXT and not _looks_like_sample(p):
                yield p


def resolve_input(cfg: Config, raw: str) -> Path:
    """用户输入的宿主机路径 → 本进程能读的路径。

    支持三种写法：
      1. 宿主机影视根： E:\\Movies\\Show.S01E01.mkv  → MEDIA_ROOT/Show.S01E01.mkv
      2. 宿主机 HOST_ROOT：E:\\photo\\source\\x.mp4   → /host/source/x.mp4
      3. 本进程路径：       /media/… 或 E:\\photo\\…
    找不到就报一个能照着做的错误。
    """
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        raise FileNotFoundError("请填写影视所在的宿主机路径")

    win = raw.replace("/", "\\").rstrip("\\")
    for host_root, container_root in (
        (cfg.media_host_root, cfg.media_root),
        (cfg.host_path_root, cfg.vault_root.parent),
    ):
        hr = (host_root or "").rstrip("\\/")
        if hr and (win.lower() == hr.lower() or win.lower().startswith(hr.lower() + "\\")):
            rel = win[len(hr):].lstrip("\\")
            p = container_root.joinpath(*rel.split("\\")) if rel else container_root
            if p.exists():
                return p.resolve()
            raise FileNotFoundError(
                f"{raw} 映射到容器内 {p}，但那边没有这个路径——确认影视目录已经挂载")

    p = Path(raw).expanduser()
    if p.exists():
        return p.resolve()
    alt = (cfg.media_root / raw).expanduser()
    if alt.exists():
        return alt.resolve()
    raise FileNotFoundError(
        f"读不到 {raw}。容器里能看到的影视根是 MEDIA_ROOT={cfg.media_root}"
        + (f"（宿主机 {cfg.media_host_root}）" if cfg.media_host_root else "")
        + "；把目录挂上或填一个容器内路径（如 /media/电影）再试")


# ---------------------------------------------------------------- 入库

@dataclass
class ImportStats:
    scanned: int = 0
    imported: int = 0          # 新增的片/集
    updated: int = 0
    failed: int = 0
    titles: int = 0            # 涉及的影视条目（去重）
    subtitles: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned, "imported": self.imported,
            "updated": self.updated, "failed": self.failed,
            "titles": self.titles, "subtitles": self.subtitles,
        }


ProgressFn = Callable[[dict[str, Any]], None]


def _relative_for(cfg: Config, p: Path) -> tuple[str, str]:
    return cfg.classify(p)


def _upsert_title(conn, parsed: ParsedName, source_rel: str | None,
                  stats: ImportStats) -> dict[str, Any]:
    key = title_key(parsed.title)
    rows = conn.execute(
        """select id, year, poster_rel, source_rel
             from cinema_titles where kind = %s and title_key = %s
            order by coalesce(year, 0)""",
        (parsed.kind, key),
    ).fetchall()
    chosen = None
    if parsed.year is not None:
        chosen = next((r for r in rows if r["year"] == parsed.year), None)
    if chosen is None:
        chosen = next((r for r in rows if r["year"] is None), None)
    if chosen is None:
        if rows:
            # 同名不同年：可能是翻拍，宁可新建
            pass
        row = conn.execute(
            """insert into cinema_titles (kind, title, title_key, year, source_rel)
               values (%s, %s, %s, %s, %s) returning id, year, poster_rel, source_rel""",
            (parsed.kind, parsed.title, key, parsed.year, source_rel),
        ).fetchone()
        stats.titles += 1
        return row
    if parsed.year is not None and chosen["year"] is None:
        conn.execute("update cinema_titles set year=%s, updated_at=now() where id=%s",
                     (parsed.year, chosen["id"]))
        chosen = dict(chosen, year=parsed.year)
    if source_rel and (not chosen["source_rel"] or parsed.kind == "movie"):
        # 电影跟着文件走（换目录/改名后重新扫描要指向新路径）；剧集保留第一个根目录
        conn.execute("update cinema_titles set source_rel=%s where id=%s",
                     (source_rel, chosen["id"]))
        chosen = dict(chosen, source_rel=source_rel)
    return chosen


def _upsert_episode(conn, title_id, parsed: ParsedName, root_key: str,
                    rel_path: str, source_path: str,
                    meta: dict[str, Any], size: int | None) -> tuple[str, bool]:
    season = parsed.season if parsed.kind == "series" else 0
    episode = parsed.episode if parsed.kind == "series" else 0
    season = season if season is not None else 1
    episode = episode if episode is not None else 0

    existing = conn.execute(
        "select id from cinema_episodes where root_key=%s and rel_path=%s",
        (root_key, rel_path)).fetchone()
    if existing:
        conn.execute(
            """update cinema_episodes
                  set title_id=%s, season=%s, episode=%s, name=%s,
                      source_path=%s, bytes=%s, duration_ms=%s, width=%s, height=%s,
                      codec=%s, bit_rate=%s, updated_at=now()
                where id=%s""",
            (title_id, season, episode, parsed.episode_title, source_path, size,
             meta.get("duration_ms"), meta.get("width"), meta.get("height"),
             meta.get("codec"), meta.get("bit_rate"), existing["id"]),
        )
        return str(existing["id"]), False

    row = conn.execute(
        """insert into cinema_episodes
             (title_id, season, episode, name, root_key, rel_path, source_path,
              bytes, duration_ms, width, height, codec, bit_rate)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           on conflict (title_id, season, episode) do update
              set name = coalesce(excluded.name, cinema_episodes.name),
                  root_key = excluded.root_key, rel_path = excluded.rel_path,
                  source_path = excluded.source_path, bytes = excluded.bytes,
                  duration_ms = excluded.duration_ms, width = excluded.width,
                  height = excluded.height, codec = excluded.codec,
                  bit_rate = excluded.bit_rate, updated_at = now()
           returning id, (xmax = 0) as inserted""",
        (title_id, season, episode, parsed.episode_title, root_key, rel_path,
         source_path, size, meta.get("duration_ms"), meta.get("width"),
         meta.get("height"), meta.get("codec"), meta.get("bit_rate")),
    ).fetchone()
    return str(row["id"]), bool(row["inserted"])


def _replace_subtitles(conn, cfg: Config, episode_id: str, video: Path,
                       stats: ImportStats) -> None:
    conn.execute("delete from cinema_subtitles where episode_id=%s", (episode_id,))
    for sub in find_subtitles(video):
        try:
            rk, rel = cfg.classify(sub.path)
        except ValueError:
            continue
        conn.execute(
            """insert into cinema_subtitles
                 (episode_id, root_key, rel_path, format, lang, label, forced)
               values (%s,%s,%s,%s,%s,%s,%s)
               on conflict (episode_id, rel_path) do nothing""",
            (episode_id, rk, rel, sub.path.suffix.lower().lstrip("."),
             sub.lang, sub.label, sub.forced),
        )
        stats.subtitles += 1


def _maybe_poster(conn, cfg: Config, title: dict[str, Any], video: Path,
                  duration_ms: int | None) -> None:
    """每个影视条目只生成一张海报（第一个成功抽到帧的片/集）。"""
    if title.get("poster_rel"):
        existing = cfg.derived_root / title["poster_rel"]
        if existing.exists():
            return
    dst = cfg.derived_root / "media" / f"{title['id']}.jpg"
    if make_poster(video, dst, duration_ms):
        rel = dst.relative_to(cfg.derived_root).as_posix()
        conn.execute("update cinema_titles set poster_rel=%s, updated_at=now() where id=%s",
                     (rel, title["id"]))
        title["poster_rel"] = rel


def run_import(
    conn,
    cfg: Config,
    root: Path,
    *,
    forced: str = "auto",
    posters: bool = True,
    progress: ProgressFn | None = None,
) -> ImportStats:
    """登记 root（文件或目录）下的影视。每登记一个文件提交一次，坏文件不拖累整批。"""
    cfg.ensure_dirs()
    stats = ImportStats()
    root = root.resolve()
    files = list(walk_videos(root))
    total = len(files)
    if progress:
        progress({"total": total, "done": 0, "current": "", **stats.as_dict()})

    for i, path in enumerate(files, 1):
        stats.scanned += 1
        try:
            parsed = parse_name(path, forced=forced)
            root_key, rel_path = _relative_for(cfg, path)
            meta = probe(path)
            size = path.stat().st_size if path.exists() else None

            source_rel = None
            if parsed.kind == "movie":
                # 电影重新扫描只针对这一个文件；剧集针对整部剧的根目录
                source_rel = rel_path
            else:
                try:
                    _, show_rel = cfg.classify(parsed.series_root or path.parent)
                    source_rel = None if show_rel == "." else show_rel
                except ValueError:
                    source_rel = None

            with conn.transaction():
                title = _upsert_title(conn, parsed, source_rel, stats)
                epid, inserted = _upsert_episode(
                    conn, title["id"], parsed, root_key, rel_path,
                    str(path), meta, size)
                _replace_subtitles(conn, cfg, epid, path, stats)
                if posters:
                    _maybe_poster(conn, cfg, title, path, meta.get("duration_ms"))
                conn.execute("update cinema_titles set updated_at=now() where id=%s",
                             (title["id"],))
            if inserted:
                stats.imported += 1
            else:
                stats.updated += 1
        except Exception as exc:                       # noqa: BLE001 —— 单文件失败要能继续
            conn.rollback()
            stats.failed += 1
            if len(stats.errors) < 200:
                stats.errors.append({"path": str(path),
                                     "error": f"{type(exc).__name__}: {exc}"})
        if progress:
            progress({"total": total, "done": i, "current": str(path),
                      **stats.as_dict()})
    return stats
