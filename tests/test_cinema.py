"""
影视库纯逻辑回归测试：不连数据库、不跑 ffprobe，只锁文件名解析和字幕识别。

    uv run python tests/test_cinema.py

覆盖：
  · 电影名清理：发布组 / 分辨率 / 编码 / 年份 / 多段点号
  · 剧集识别：SxxEyy / 1x02 / 第01集 / Season 目录 + 纯数字 / EP 前缀 / 番剧
  · 不该误判成剧集的：带数字的电影名（1917 / 2012）
  · 集名提取与噪声过滤
  · 字幕语言：zh-Hans / cht / en / forced / 中英 / 无标记
  · SRT → WebVTT 时间轴
  · 真跑一次 ffmpeg 抽海报（没装就跳过）
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vault import cinema                                            # noqa: E402

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok  {name}")
    else:
        failures.append(f"{name}  {detail}")
        print(f"  FAIL {name}  {detail}")


def mkv(*parts: str) -> Path:
    return Path("/media") / Path(*parts)


# ---------------------------------------------------------------- 电影
print("\n[电影名]")
cases = [
    ("The.Matrix.1999.1080p.BluRay.x264-GROUP.mkv",
     "The Matrix", 1999),
    ("让子弹飞.2010.1080p.mkv", "让子弹飞", 2010),
    ("[Studio] Parasite (2019) [2160p] [HDR].mkv", "Parasite", 2019),
    ("Spider-Man.No.Way.Home.2021.WEB-DL.DDP5.1.H.264.mkv",
     "Spider-Man No Way Home", 2021),
    ("1917.2019.1080p.mkv", "1917", 2019),
    ("1917.mkv", "1917", None),
    ("2012.mkv", "2012", None),
    ("Blade.Runner.2049.2017.mkv", "Blade Runner 2049", 2017),
    ("interstellar.mkv", "interstellar", None),
    ("Movie Name (2019) [1080p] [国语中字].mkv", "Movie Name", 2019),
    ("Some.Movie.2020.1080p.WEB-DL.AAC5.1-HDCTV.mkv", "Some Movie", 2020),
]
for name, want_title, want_year in cases:
    got = cinema.parse_name(mkv("Movies", name))
    check(f"{name} → 「{want_title}」 {want_year}",
          got.kind == "movie" and got.title == want_title and got.year == want_year,
          f"{got.kind} 「{got.title}」 {got.year}")


# ---------------------------------------------------------------- 剧集
print("\n[剧集名]")
series = [
    ("Breaking.Bad.S01E02.720p.BluRay.mkv",
     "Breaking Bad", 1, 2, None),
    ("Breaking Bad - S01E02 - Cat's in the Bag.mkv",
     "Breaking Bad", 1, 2, "Cat's in the Bag"),
    ("Show.Name.1x02.HDTV.mkv", "Show Name", 1, 2, None),
    ("Show Name Season 1 Episode 2.mkv", "Show Name", 1, 2, None),
    ("进击的巨人 第01集.mp4", "进击的巨人", 1, 1, None),
]
for name, want_title, want_s, want_e, want_ep_title in series:
    got = cinema.parse_name(mkv("Shows", name))
    check(f"{name} → 「{want_title}」 S{want_s}E{want_e}",
          got.kind == "series" and got.title == want_title
          and got.season == want_s and got.episode == want_e
          and (got.episode_title or None) == want_ep_title,
          f"{got.kind} 「{got.title}」 S{got.season}E{got.episode} ep={got.episode_title!r}")

got = cinema.parse_name(mkv("Shows", "Breaking Bad", "Season 2", "03 - Bit by a Dead Bee.mkv"))
check("Season 目录 + 编号 → 剧集",
      got.kind == "series" and got.title == "Breaking Bad"
      and got.season == 2 and got.episode == 3,
      f"{got.kind} 「{got.title}」 S{got.season}E{got.episode}")

got = cinema.parse_name(mkv("Shows", "The Office", "S03", "E05.mkv"))
check("S03/E05 → 剧集", got.kind == "series" and got.title == "The Office"
      and got.season == 3 and got.episode == 5,
      f"{got.kind} 「{got.title}」 S{got.season}E{got.episode}")

got = cinema.parse_name(mkv("Anime", "[SubsPlease] Frieren - 05 [1080p].mkv"))
check("番剧 [组] Show - 05 → 剧集", got.kind == "series"
      and got.title == "Frieren" and got.episode == 5,
      f"{got.kind} 「{got.title}」 E{got.episode}")

got = cinema.parse_name(mkv("Shows", "Show.2020.S02E03.1080p.mkv"))
check("剧集年份进 year，不进片名", got.kind == "series" and got.title == "Show"
      and got.year == 2020 and got.season == 2, f"{got.title} {got.year} S{got.season}")

got = cinema.parse_name(mkv("Movies", "Show.S01E02.mkv"), forced="movie")
check("--kind movie 强制当电影", got.kind == "movie" and got.season == 0,
      f"{got.kind} S{got.season}E{got.episode}")

got = cinema.parse_name(mkv("Movies", "Baraka - 01.mkv"))
check("无季目录的『片名 - 01』不误判成剧集", got.kind == "movie",
      f"{got.kind} 「{got.title}」 E{got.episode}")

got = cinema.parse_name(mkv("Movies", "Spider-Man.2002.mkv"))
check("片名里的连字符不丢", got.title == "Spider-Man" and got.year == 2002,
      f"「{got.title}」 {got.year}")


# ---------------------------------------------------------------- 片名清理
print("\n[片名清理]")
check("发布组后缀被去掉",
      cinema.clean_title("Some.Movie.2020.1080p.WEB-DL.x264-HDCTV") == "Some Movie 2020",
      cinema.clean_title("Some.Movie.2020.1080p.WEB-DL.x264-HDCTV"))
check("方括号整段丢弃",
      cinema.clean_title("[Group] Movie [1080p]") == "Movie",
      cinema.clean_title("[Group] Movie [1080p]"))
check("中文语言标记去掉",
      cinema.clean_title("流浪地球2.2023.国语中字.1080p") == "流浪地球2 2023",
      cinema.clean_title("流浪地球2.2023.国语中字.1080p"))
check("片名里的 eng 不被当成英语（Avengers）",
      cinema.clean_title("Avengers.Endgame.2019") == "Avengers Endgame 2019",
      cinema.clean_title("Avengers.Endgame.2019"))
check("空格塌缩 + 首尾标点", cinema.clean_title("  .. Movie  .. ") == "Movie",
      cinema.clean_title("  .. Movie  .. "))
check("归并键忽略大小写和标点",
      cinema.title_key("The Matrix") == cinema.title_key("the.matrix"),
      f"{cinema.title_key('The Matrix')} / {cinema.title_key('the.matrix')}")
check("中文归并键保留汉字",
      cinema.title_key("流浪地球 2") == "流浪地球2", cinema.title_key("流浪地球 2"))


# ---------------------------------------------------------------- 季目录
print("\n[季目录]")
check("Season 1", cinema.season_from_dir("Season 1") == 1)
check("S02", cinema.season_from_dir("Show S02") == 2)
check("第二季", cinema.season_from_dir("第二季") == 2)
check("第十二季", cinema.season_from_dir("第十二季") == 12)
check("第二十季", cinema.season_from_dir("第二十季") == 20)
check("Specials → 0", cinema.season_from_dir("Specials") == 0)
check("普通目录不是季目录", cinema.season_from_dir("Breaking Bad") is None)
check("片名里的 S01 被去掉", cinema.clean_title("Show S02") == "Show",
      cinema.clean_title("Show S02"))
check("S1m0ne / Se7en 不被当成季标记",
      cinema.clean_title("S1m0ne") == "S1m0ne" and cinema.clean_title("Se7en") == "Se7en",
      f"{cinema.clean_title('S1m0ne')} / {cinema.clean_title('Se7en')}")


# ---------------------------------------------------------------- 字幕识别
print("\n[字幕]")
lang_cases = [
    ("Movie.zh-Hans.srt", "zh-CN", "简体中文"),
    ("Movie.chs.ass", "zh-CN", "简体中文"),
    ("Movie.cht.ass", "zh-TW", "繁體中文"),
    ("Movie.zh.srt", "zh-CN", "中文"),
    ("Movie.eng.srt", "en", "English"),
    ("Movie.jpn.srt", "ja", "日本語"),
    ("Movie.中英双字.srt", "zh-CN", "中英双语"),
    ("Movie.简体.srt", "zh-CN", "简体中文"),
]
for name, want_lang, want_label in lang_cases:
    suffix = Path(name).stem[len("Movie"):]
    lang, label, _ = cinema.subtitle_meta(suffix, name)
    check(f"{name} → {want_label}", lang == want_lang and label == want_label,
          f"{lang} {label}")

lang, label, forced = cinema.subtitle_meta(".zh-Hans.forced", "Movie.zh-Hans.forced.srt")
check("forced 标记", forced and lang == "zh-CN", f"{lang} {forced}")
lang, label, _ = cinema.subtitle_meta(".unknown", "Movie.unknown.srt")
check("无语言标记 → 字幕", lang is None and label == "字幕", f"{lang} {label}")

with tempfile.TemporaryDirectory() as td:
    d = Path(td) / "a"
    d.mkdir()
    (d / "Movie.1080p.BluRay.x264-GROUP.mkv").write_bytes(b"")
    (d / "Movie.srt").write_text("1", encoding="utf-8")
    (d / "Movie.zh-CN.srt").write_text("1", encoding="utf-8")
    (d / "Movie.en.srt").write_text("1", encoding="utf-8")
    (d / "Movie.2.srt").write_text("1", encoding="utf-8")          # 数字不是年份
    (d / "OtherMovie.srt").write_text("1", encoding="utf-8")
    subs = cinema.find_subtitles(d / "Movie.1080p.BluRay.x264-GROUP.mkv")
    names = sorted(s.path.name for s in subs)
    check("不带发布组标签的字幕也能匹配（Movie.zh-CN.srt）",
          names == ["Movie.en.srt", "Movie.srt", "Movie.zh-CN.srt"], str(names))
    check("zh-CN 识别成简体中文",
          any(s.lang == "zh-CN" and s.label == "简体中文" for s in subs),
          str([(s.lang, s.label) for s in subs]))
    check("有语言标记的排在无标记前面",
          subs[0].label != "字幕", str([s.label for s in subs]))

    d2 = Path(td) / "b"
    d2.mkdir()
    (d2 / "Movie.mkv").write_bytes(b"")
    (d2 / "Movie.2019.zh.srt").write_text("1", encoding="utf-8")
    subs2 = cinema.find_subtitles(d2 / "Movie.mkv")
    check("字幕多一个年份也认（Movie.srt vs Movie.2019.mkv）", len(subs2) == 1,
          str([s.path.name for s in subs2]))

check("跨集字幕不误配", not cinema._is_subtitle_for("Show.S01E01.1080p", "Show.S01E02.srt"))
check("标题里的数字不是年份就不配", not cinema._is_subtitle_for("Movie", "Movie.2.srt"))


# ---------------------------------------------------------------- SRT → VTT
print("\n[SRT → WebVTT]")
srt = "1\r\n00:00:01,000 --> 00:00:04,500\r\n你好\r\n\r\n2\r\n00:00:05,000 --> 00:00:06,000\r\n再见\r\n"
vtt = cinema.srt_to_vtt(srt)
check("带 WEBVTT 头", vtt.startswith("WEBVTT\n\n"))
check("逗号换成点", "00:00:01.000 --> 00:00:04.500" in vtt)
check("CRLF 归一", "\r" not in vtt)
check("ASS 定位标签被去掉", "{\\an8}" not in cinema.srt_to_vtt("1\n00:00:01,000 --> 00:00:02,000\n{\\an8}你好\n"))


# ---------------------------------------------------------------- ffmpeg 海报
print("\n[ffmpeg 海报]")
if not shutil.which("ffmpeg"):
    print("  skip 没装 ffmpeg")
else:
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "clip.mkv"
        subprocess.run([
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi",
            "-i", "testsrc2=size=320x180:rate=10:duration=6",
            "-pix_fmt", "yuv420p", str(src)], check=True, timeout=60)
        dst = Path(td) / "derived" / "media" / "x.jpg"
        ok = cinema.make_poster(src, dst, 6000)
        check("海报抽帧成功且非空", ok and dst.exists() and dst.stat().st_size > 1024,
              f"{ok} {dst}")
        if dst.exists():
            from PIL import Image
            with Image.open(dst) as im:
                check("海报是 600×900 竖版", im.size == (600, 900), str(im.size))


print()
if failures:
    print(f"{len(failures)} 项失败：")
    for f in failures:
        print("  -", f)
    raise SystemExit(1)
print("全部通过")
