r"""视频打标：Qwen3-VL-4B-Instruct（Q4_K_M GGUF）+ llama.cpp CUDA。

为什么是 llama.cpp 而不是 ONNX Runtime：
    1070 Ti 是 Pascal（SM 6.1），FP16 吞吐只有 FP32 的 1/64，所以"半精度推理"
    在这张卡上是负优化；能用的 4-bit 路线只有 weight-only 量化 + FP32 激活。
    ONNX Runtime 的 int4 矩阵乘（MatMulNBits）CUDA kernel 要求 SM 7.5+，
    在 Pascal 上会退回 CPU；vLLM 的 GPTQ/AWQ/Marlin kernel 更激进，起步就要
    SM 7.0/8.0。llama.cpp 的 CUDA 后端对 Pascal 的 K-quant 有完整实现
    （f16 权重 × f32 激活，走 FP32 FMA），是这张卡上唯一成熟的路。
    这也意味着不能跟人脸引擎共用一个 ONNX EP —— 两套运行时各占一点显存：
    人脸 ~0.65 GB + VLM ~3 GB，8 GB 够用，但不要同时开两个 worker。

调用方式：
    本模块自己起一个 llama-server 子进程（localhost 随机端口），
    用 OpenAI 兼容的 /v1/chat/completions 发图；退出时自动杀掉。

    uv run --group ml python -m ml.vlm --download        # 下模型（~3.5 GB）
    uv run --group ml python -m ml.vlm --images a.jpg    # 单图自检
"""
from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
MODELS_DIR = Path(os.environ.get("VAULT_MODELS_DIR", str(_REPO / "models")))
LLAMA_BIN = Path(os.environ.get("VLM_LLAMA_SERVER", "")) if os.environ.get(
    "VLM_LLAMA_SERVER") else None
MODEL_FILE = Path(os.environ.get(
    "VLM_MODEL", str(MODELS_DIR / "qwen3-vl-4b" / "Qwen3-VL-4B-Instruct-Q4_K_M.gguf")))
MMPROJ_FILE = Path(os.environ.get(
    "VLM_MMPROJ", str(MODELS_DIR / "qwen3-vl-4b" / "mmproj-F16.gguf")))

VIDEO_MODEL_ID = "qwen3-vl-4b-instruct-q4_k_m"
LLAMA_CPP_TAG = "b12018"          # 下载器/README 用同一个版本号

# 受控词表：VLM 只从这里面选，避免 4B 小模型自由发挥造出几百个同义标签。
# 词表偏"能用来找照片"的粗粒度概念；太细的（"挖掘机"）交给 OCR 文本和 caption。
TAG_VOCAB = [
    # 地点 / 场景
    "室内", "室外", "家中", "办公室", "教室", "餐厅", "商场", "街道", "公园",
    "海边", "沙滩", "山上", "森林", "草地", "湖边", "河边", "城市", "乡村",
    "车站", "机场", "地铁", "酒店", "景区", "雪地", "夜景", "日落", "日出",
    "烟花", "舞台", "医院", "寺庙", "游泳池",
    # 人物
    "有人", "多人", "孩子", "婴儿", "老人", "合影", "自拍", "背影", "人群",
    "表演者",
    # 活动
    "旅行", "聚会", "吃饭", "喝酒", "生日", "婚礼", "运动", "跑步", "打球",
    "游泳", "爬山", "骑车", "开车", "钓鱼", "购物", "拍照", "工作", "上课",
    "唱歌", "跳舞", "玩游戏", "散步", "露营", "滑雪", "划船", "看演出",
    "健身", "做饭",
    # 物体 / 题材
    "食物", "蛋糕", "水果", "饮料", "宠物", "猫", "狗", "鸟", "鱼", "汽车",
    "卡车", "公交车", "火车", "飞机", "船只", "建筑", "桥梁", "塔", "雕塑",
    "花", "树", "水", "火", "灯光", "屏幕", "手机", "相机", "乐器", "玩具",
    "球", "书", "画", "招牌", "文字", "机器", "气球", "礼物", "车内",
    # 拍摄风格 / 素材类型
    "黑白", "老胶片", "录像带", "航拍", "特写", "慢动作", "延时", "手持",
    "模糊", "屏幕录制", "动画", "转场",
]
TAG_SET = frozenset(TAG_VOCAB)

# 模型偶尔会输出更口语/更细的说法，统一收敛到词表里的规范词
TAG_ALIASES = {
    "人物": "有人", "人们": "有人", "群众": "人群", "儿童": "孩子",
    "小朋友": "孩子", "宝宝": "婴儿", "老年人": "老人", "合照": "合影",
    "合影照": "合影", "全家福": "合影", "户外": "室外", "屋子里": "室内",
    "房间": "室内", "家里": "家中", "街": "街道", "马路": "街道",
    "海滩": "沙滩", "大海": "海边", "海": "海边", "山": "山上", "树林": "森林",
    "田野": "乡村", "农村": "乡村", "都市": "城市", "市中心": "城市",
    "火车站": "车站", "高铁站": "车站", "商场里": "商场", "超市": "商场",
    "夜里": "夜景", "夜晚": "夜景", "晚上": "夜景", "黄昏": "日落",
    "夕阳": "日落", "早晨": "日出", "礼花": "烟花", "演唱会": "看演出",
    "演出": "看演出", "音乐会": "看演出", "聚餐": "吃饭", "用餐": "吃饭",
    "美食": "食物", "菜品": "食物", "啤酒": "喝酒", "红酒": "喝酒",
    "健身运动": "健身", "锻炼": "健身", "徒步": "爬山", "登山": "爬山",
    "游泳馆": "游泳", "驾驶": "开车", "汽车驾驶": "开车", "购物中心": "商场",
    "商店": "商场", "跳广场舞": "跳舞", "广场舞": "跳舞", "电子游戏": "玩游戏",
    "宠物狗": "狗", "小狗": "狗", "猫咪": "猫", "小猫": "猫", "汽车站": "车站",
    "轿车": "汽车", "货车": "卡车", "船": "船只", "建筑物": "建筑",
    "楼房": "建筑", "桥": "桥梁", "鲜花": "花", "树木": "树", "火焰": "火",
    "火光": "火", "屏幕截图": "屏幕", "截屏": "屏幕", "手机屏幕": "手机",
    "文字说明": "文字", "字幕": "文字", "标识": "招牌", "店招": "招牌",
    "机械": "机器", "设备": "机器", "老电影": "老胶片", "胶片": "老胶片",
    "录像": "录像带", "VHS": "录像带", "无人机": "航拍", "无人机航拍": "航拍",
    "近景": "特写", "虚化": "模糊", "卡通": "动画", "动图": "动画",
    "车厢": "车内", "驾驶室": "车内", "汽车内部": "车内", "车里": "车内",
    "车内空间": "车内",
}

SYSTEM_PROMPT = (
    "你是家庭相册的视频标注员。用户会给出一段视频里抽出的关键帧（按时间顺序）。"
    "只描述画面里能确认的内容，不要猜测看不到的东西。"
)


def _prompt(ord_: int, total: int) -> str:
    vocab = "、".join(TAG_VOCAB)
    return (
        f"这是同一段视频的第 {ord_ + 1}/{total} 张关键帧。\n"
        f"只输出一个 JSON 对象，不要任何解释或 Markdown 代码块，格式：\n"
        f'{{"caption": "一句话描述，不超过 30 个汉字，写清谁/在哪/做什么", '
        f'"labels": ["从词表里选 0-6 个，按重要性排序"], '
        f'"ocr": "画面里清晰可读的文字，多条用; 分隔，没有就空字符串"}}\n'
        f"labels 只能从下面的词表里选，画面里没有的事物不要写：\n{vocab}"
    )


@dataclass
class FrameResult:
    labels: list[str] = field(default_factory=list)
    caption: str = ""
    ocr: str = ""
    raw: str = ""


@dataclass
class VideoResult:
    frames: list[FrameResult]
    labels: list[dict]              # [{"name": ..., "frames": n}]
    caption: str
    ocr: str


# ---------------------------------------------------------------- 解析 / 聚合

def _strip_json(text: str) -> str:
    """去掉 ```json 围栏和前后废话，截出最外层花括号。"""
    t = text.strip()
    if "```" in t:
        parts = t.split("```")
        t = next((p for p in parts if "{" in p), t)
        t = t.removeprefix("json").strip()
    lo, hi = t.find("{"), t.rfind("}")
    return t[lo:hi + 1] if lo >= 0 and hi > lo else ""


def strip_think(text: str) -> str:
    """Qwen3 系列的思考块（...）一律丢掉：打标只要结论。"""
    while " thinking" in text and "<｜end▁of▁thinking｜>" in text:
        lo = text.find(" thinking")
        hi = text.find("<｜end▁of▁thinking｜>", lo)
        if hi < 0:
            break
        text = text[:lo] + text[hi + len("<｜end▁of▁thinking｜>"):]
    return text.strip()


def parse_result(text: str) -> FrameResult:
    """模型输出 → FrameResult。JSON 坏了也不抛，尽量捞 caption 和标签。"""
    raw = strip_think(text or "")
    body = _strip_json(raw)
    data: dict = {}
    if body:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {}
    labels = normalize_labels(data.get("labels") or [])
    caption = str(data.get("caption") or "").strip()
    ocr = str(data.get("ocr") or "").strip()
    if not labels and not caption:
        # 兜底：没 JSON 就把整段文字当描述（至少别丢信息），标签从文字里捞词表词
        caption = raw.strip().replace("\n", " ")[:60]
        labels = [t for t in TAG_VOCAB if t in raw]
    return FrameResult(labels=labels, caption=caption, ocr=ocr, raw=raw)


def normalize_labels(labels) -> list[str]:
    """别名收敛 → 去重 → 只留词表内的 → 保持原顺序。"""
    out: list[str] = []
    for item in labels:
        name = str(item).strip().strip("#").strip()
        if not name:
            continue
        name = TAG_ALIASES.get(name, name)
        if name in TAG_SET and name not in out:
            out.append(name)
    return out


# 互斥标签：同一段视频里两个都出现说明模型在瞎猜（比如游戏画面里既有"室内"又有"室外"）。
# 只留票数高的那个；平票就都不留 —— 宁可少一个标签，也不给库留矛盾。
EXCLUSIVE_PAIRS = [("室内", "室外")]
# 蕴含标签：更具体的有票时，泛化的跟着出现只是噪音。
# 车内视角既不算"室内"也不算"室外"——行车记录仪画面里两个都不该留。
IMPLIED = {"屏幕录制": {"屏幕"}, "车内": {"室内"}}


def aggregate(frames: list[FrameResult], min_share: float = 0.5) -> tuple[list[dict], str, str]:
    """帧级结果 → 视频级标签。票数 ≥ ceil(n*min_share) 的留下；一个都没有就取前二。

    票数存进库：以后想调阈值（比如改成 2/4 才留）不用重跑模型。
    """
    counts: dict[str, int] = {}
    order: dict[str, int] = {}
    for i, fr in enumerate(frames):
        for name in fr.labels:
            counts[name] = counts.get(name, 0) + 1
            order.setdefault(name, i)
    if not counts:
        labels: list[dict] = []
    else:
        need = max(1, int(round(len(frames) * min_share + 0.4999)))
        keep = {n for n, c in counts.items() if c >= need}
        if not keep:
            keep = {n for n, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:2]}
        for a, b in EXCLUSIVE_PAIRS:
            if a in keep and b in keep:
                if counts[a] > counts[b]:
                    keep.discard(b)
                elif counts[b] > counts[a]:
                    keep.discard(a)
                else:
                    keep -= {a, b}
        for specific, generic in IMPLIED.items():
            if specific in keep:
                keep -= generic
        labels = [{"name": n, "frames": counts[n]}
                  for n in sorted(keep, key=lambda n: (-counts[n], order[n]))]
    # 视频级描述：选信息量最大的那帧（标签最多），并列取中间
    best = ""
    if frames:
        score = [(len(f.labels) + (1 if f.caption else 0), -abs(i - len(frames) / 2))
                 for i, f in enumerate(frames)]
        idx = max(range(len(frames)), key=lambda i: score[i])
        best = frames[idx].caption
        if not best:
            best = next((f.caption for f in frames if f.caption), "")
    # OCR 跨帧合并：同一行字不同帧可能多几个空格，按"去掉空格"当键去重
    seen: dict[str, str] = {}
    for f in frames:
        for s in f.ocr.split(";"):
            s = " ".join(s.split())
            if s:
                seen.setdefault(s.replace(" ", "").lower(), s)
    ocr = "; ".join(seen.values())
    return labels, best, ocr


# ---------------------------------------------------------------- llama-server

def find_server() -> Path:
    cands = [LLAMA_BIN] if LLAMA_BIN else []
    cands += [
        MODELS_DIR / "llama.cpp" / "bin" / "llama-server.exe",
        MODELS_DIR / "llama.cpp" / "bin" / "llama-server",
    ]
    for c in cands:
        if c and c.exists():
            return c
    raise FileNotFoundError(
        "找不到 llama-server（llama.cpp 的 CUDA 版）。找过:\n"
        + "\n".join(f"  {c}" for c in cands if c)
        + "\n  运行 `python -m ml.vlm --download` 或见 README「视频打标」")


def ready() -> tuple[bool, str]:
    """worker 启动时先问一句：llama.cpp + 两个 GGUF 都在吗。"""
    try:
        find_server()
    except FileNotFoundError as exc:
        return False, str(exc).split("\n")[0]
    for path, label in ((MODEL_FILE, "VLM 权重"), (MMPROJ_FILE, "视觉塔")):
        if not path.exists():
            return False, f"缺少{label} {path}（python -m ml.vlm --download）"
    return True, ""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LlamaServer:
    """托管 llama-server 子进程。start() 等到 /health 通过再返回。"""

    def __init__(self, ctx: int = 6144, ngl: int = 99, log: Path | None = None,
                 extra: list[str] | None = None, ready_timeout: float = 240):
        self.port = _free_port()
        self.ctx = ctx
        self.ngl = ngl
        self.ready_timeout = ready_timeout
        self.log_path = log or (MODELS_DIR / "llama.cpp" / "server.log")
        self.extra = extra or []
        self.proc: subprocess.Popen | None = None
        self._log = None

    def start(self) -> "LlamaServer":
        exe = find_server()
        if not MODEL_FILE.exists():
            raise FileNotFoundError(f"找不到 VLM 权重 {MODEL_FILE}（python -m ml.vlm --download）")
        if not MMPROJ_FILE.exists():
            raise FileNotFoundError(f"找不到视觉塔 {MMPROJ_FILE}（python -m ml.vlm --download）")
        cmd = [
            str(exe), "-m", str(MODEL_FILE), "--mmproj", str(MMPROJ_FILE),
            "-ngl", str(self.ngl), "-c", str(self.ctx),
            "--host", "127.0.0.1", "--port", str(self.port),
            "-np", "1", "--no-warmup", "--jinja",
        ] + self.extra
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(self.log_path, "wb")
        env = dict(os.environ)
        # llama.cpp 的 CUDA 运行库在 bin 目录里，DLL 只在 exe 同目录/系统 PATH 里找
        env["PATH"] = str(exe.parent) + os.pathsep + env.get("PATH", "")
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(cmd, cwd=str(exe.parent), stdout=self._log,
                                     stderr=subprocess.STDOUT, env=env,
                                     creationflags=flags)
        t0 = time.time()
        while time.time() - t0 < self.ready_timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server 启动即退出（{self.proc.returncode}），"
                                   f"日志见 {self.log_path}")
            if self._healthy():
                return self
            time.sleep(0.5)
        self.stop()
        raise RuntimeError(f"llama-server {self.ready_timeout:.0f}s 内没就绪，"
                           f"日志见 {self.log_path}")

    def _healthy(self) -> bool:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/health", timeout=2) as r:
                return r.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._log:
            self._log.close()
            self._log = None
        self.proc = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


class Vlm:
    """VLM 门面：懒启动服务，一次视频一次调用链。"""

    def __init__(self, server: LlamaServer | None = None, ctx: int = 6144):
        self._server = server
        self._own = server is None
        self.ctx = ctx
        self.max_tokens = int(os.environ.get("VLM_MAX_TOKENS", "200"))

    @property
    def alive(self) -> bool:
        s = self._server
        return bool(s and s.proc and s.proc.poll() is None)

    def start(self) -> "Vlm":
        if not self.alive:
            self.stop()
            self._server = LlamaServer(ctx=self.ctx).start()
        return self

    def stop(self) -> None:
        if self._own and self._server is not None:
            self._server.stop()
            self._server = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def _post(self, path: str, body: dict, timeout: float = 300) -> dict:
        assert self._server is not None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._server.port}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def describe_image(self, image: Path, total: int = 1, ord_: int = 0) -> FrameResult:
        b64 = base64.b64encode(image.read_bytes()).decode()
        base = {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    {"type": "text", "text": _prompt(ord_, total)},
                ]},
            ],
            "max_tokens": self.max_tokens,
            "temperature": 0.1,
            "top_p": 0.8,
            "top_k": 20,
            "repeat_penalty": 1.05,
            "cache_prompt": False,
        }
        optional = [
            {"response_format": {"type": "json_object"}},
            {"chat_template_kwargs": {"enable_thinking": False}},
        ]
        # 不同 llama.cpp 版本对 response_format / chat_template_kwargs 的支持不一致：
        # 400 就丢掉一个可选项重试，全丢完还 400 才真报错。
        resp = None
        for i in range(len(optional) + 1):
            body = dict(base, **{k: v for d in optional[:len(optional) - i] for k, v in d.items()})
            try:
                resp = self._post("/v1/chat/completions", body)
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:300]
                if exc.code != 400 or i == len(optional):
                    raise RuntimeError(f"VLM 请求失败 {exc.code}: {detail}") from exc
        assert resp is not None
        msg = resp["choices"][0]["message"]
        text = msg.get("content") or msg.get("reasoning_content") or ""
        return parse_result(text)

    def tag_frames(self, images: list[Path]) -> VideoResult:
        """逐帧问一遍，再按票数聚合。帧数少（≤5）时这样比"一次喂多图"更准：
        4B 小模型一次看 4 张图容易漏掉单张里的细节。"""
        assert self.start() is not None
        results: list[FrameResult] = []
        for i, img in enumerate(images):
            results.append(self.describe_image(img, total=len(images), ord_=i))
        labels, caption, ocr = aggregate(results)
        return VideoResult(frames=results, labels=labels, caption=caption, ocr=ocr)


# ---------------------------------------------------------------- 下载 / 自检

def download(force: bool = False) -> int:
    """llama.cpp CUDA 运行时 + Qwen3-VL GGUF。HF 直连不通时自动改道 hf-mirror。"""
    repo = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_CPP_TAG}"
    jobs = [
        (f"{repo}/llama-{LLAMA_CPP_TAG}-bin-win-cuda-12.4-x64.zip",
         MODELS_DIR / "llama.cpp" / "llama.zip"),
        (f"{repo}/cudart-llama-bin-win-cuda-12.4-x64.zip",
         MODELS_DIR / "llama.cpp" / "cudart.zip"),
    ]
    hf = "unsloth/Qwen3-VL-4B-Instruct-GGUF/resolve/main"
    for name in ("Qwen3-VL-4B-Instruct-Q4_K_M.gguf", "mmproj-F16.gguf"):
        jobs.append((f"https://huggingface.co/{hf}/{name}", MODELS_DIR / "qwen3-vl-4b" / name))
    for url, dst in jobs:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and dst.stat().st_size > 1 << 20 and not force:
            print(f"跳过（已存在）{dst.name}")
            continue
        print(f"下载 {dst.name} …", flush=True)
        ok = False
        for attempt_url in ([url] if "github" in url
                            else [url, url.replace("https://huggingface.co",
                                                   "https://hf-mirror.com")]):
            try:
                _fetch(attempt_url, dst)
                ok = True
                break
            except Exception as exc:  # noqa: BLE001 — 换源重试
                print(f"  失败 {type(exc).__name__}: {exc}", flush=True)
        if not ok:
            print(f"  ✗ 放弃 {dst.name}")
            return 1
        print(f"  ✓ {dst.name} {dst.stat().st_size/1e6:.0f} MB", flush=True)
    # 解压 llama.cpp 自带 + cudart 两个包
    bin_dir = MODELS_DIR / "llama.cpp" / "bin"
    if (bin_dir / "llama-server.exe").exists() and (bin_dir / "ggml-cuda.dll").exists():
        print(f"跳过解压（{bin_dir} 已就绪）")
    else:
        for zname in ("llama.zip", "cudart.zip"):
            zp = MODELS_DIR / "llama.cpp" / zname
            if not zp.exists():
                continue
            try:
                with zipfile.ZipFile(zp) as zf:
                    zf.extractall(bin_dir)
            except PermissionError as exc:
                print(f"解压 {zname} 失败：{exc}\n"
                      f"  大概是 worker 正占着 llama-server / DLL，停掉 worker 再试一次")
                return 1
            print(f"解压 {zname} → {bin_dir}")
    print("完成。验证：uv run --group ml python -m ml.vlm --images <某张图.jpg>")
    return 0


def _fetch(url: str, dst: Path) -> None:
    tmp = dst.with_suffix(dst.suffix + ".part")
    with urllib.request.urlopen(urllib.request.Request(
            url, headers={"User-Agent": "photo-vault/0.1"}), timeout=60) as r, \
            open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    tmp.replace(dst)


def reaggregate(dsn: str, min_share: float = 0.5, dry_run: bool = False) -> int:
    """只重算聚合，不跑模型：从 video_frames 的帧级结果重出摘要和 vlm 标签。

    调聚合规则（票数门槛、互斥词）时用这个，几秒钟搞定，不用重看几千段视频。
    """
    import psycopg

    tag_sql = """
    with t as (
        insert into tags (name) values (%s)
        on conflict (name) do update set name = excluded.name
        returning id
    )
    insert into asset_tags (asset_id, tag_id, source)
    select %s::uuid, t.id, 'vlm' from t
    on conflict (asset_id, tag_id) do nothing
    """
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "select asset_id, caption, labels, ocr from video_frames "
            "order by asset_id, ord").fetchall()
        per: dict[str, list[FrameResult]] = {}
        t_ms: dict[str, int] = {}
        for asset_id, caption, labels, ocr in rows:
            per.setdefault(str(asset_id), []).append(
                FrameResult(labels=list(labels or []), caption=caption or "",
                            ocr=ocr or ""))
        for i, r in enumerate(conn.execute(
                "select asset_id, ms from video_summaries").fetchall()):
            t_ms[str(r[0])] = r[1] or 0
        n_tags = 0
        for asset_id, frames in per.items():
            labels, caption, ocr = aggregate(frames, min_share)
            n_tags += len(labels)
            if dry_run:
                continue
            conn.execute(
                "update video_summaries set n_frames=%s, caption=%s, labels=%s::jsonb, "
                "ocr=%s, updated_at=now(), ms=%s where asset_id=%s::uuid",
                (len(frames), caption, json.dumps(labels, ensure_ascii=False),
                 ocr, t_ms.get(asset_id), asset_id))
            conn.execute("delete from asset_tags where asset_id=%s::uuid "
                         "and source='vlm'", (asset_id,))
            for lab in labels:
                conn.execute(tag_sql, (lab["name"], asset_id))
        if not dry_run:
            conn.commit()
    print(f"{'[dry-run] ' if dry_run else ''}重算 {len(per)} 个视频，"
          f"平均 {n_tags / max(len(per), 1):.1f} 个标签")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m ml.vlm", description="视频打标 VLM")
    ap.add_argument("--download", action="store_true", help="下载 llama.cpp + Qwen3-VL 权重")
    ap.add_argument("--images", nargs="+", help="对若干张图跑一遍标注（自检）")
    ap.add_argument("--json", action="store_true", help="打印原始 JSON")
    ap.add_argument("--force", action="store_true", help="重新下载")
    ap.add_argument("--reaggregate", action="store_true",
                    help="按现有帧结果重算视频级标签（不跑模型）")
    ap.add_argument("--min-share", type=float, default=0.5,
                    help="标签要在多少比例的帧里出现（默认 0.5）")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dsn", default=os.environ.get(
        "DATABASE_URL", "postgresql://vault:vault@localhost:5433/vault"))
    args = ap.parse_args(argv)

    if args.download:
        return download(force=args.force)
    if args.reaggregate:
        return reaggregate(args.dsn, args.min_share, args.dry_run)
    if not args.images:
        ap.print_help()
        return 2

    imgs = [Path(p) for p in args.images]
    missing = [str(p) for p in imgs if not p.exists()]
    if missing:
        print(f"找不到：{missing}")
        return 1
    t0 = time.time()
    with Vlm() as vlm:
        print(f"服务就绪 {time.time()-t0:.1f}s  port={vlm._server.port}")
        for img in imgs:
            t1 = time.time()
            fr = vlm.describe_image(img)
            print(f"[{img.name}] {time.time()-t1:.2f}s")
            if args.json:
                print(fr.raw)
            print(f"  描述 {fr.caption}")
            print(f"  标签 {fr.labels}")
            print(f"  OCR  {fr.ocr}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
