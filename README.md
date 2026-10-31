# Photo Vault · P0

本地优先的个人照片/视频资产管理服务。目标不是"又一个相册 App"，而是**一个能活 10 年、原件永不损坏、换任何软件都读得出来**的媒体库。

当前实现的是整条链路上**最危险、也最不依赖算力**的一段：**入库**；
AI 打标（RAM++）和人脸（InsightFace）已经接上，都在宿主机 worker 里跑。
数据模型、队列、内容寻址从一开始就为它们留好了位置。

---

## 30 秒跑起来

```bash
cp .env.example .env
docker compose up -d db
docker compose run --rm app python -m vault.cli initdb
docker compose run --rm app python -m vault.cli ingest /host/source --mode link
docker compose up -d app
# 打开 http://localhost:8080
```

自检：

```bash
docker compose run --rm app python -m vault.cli doctor
docker compose run --rm app python -m vault.cli reconcile   # 反向对账
docker compose run --rm app python tests/test_time.py
```

---

## 本地开发（uv）

依赖用 [uv](https://docs.astral.sh/uv/) 管理：`pyproject.toml` 声明依赖，`uv.lock` 锁定精确版本，Python 固定 3.12（与 Docker 镜像一致）。

```bash
uv sync                              # 创建 .venv，按 uv.lock 精确安装
uv run photo-vault doctor            # console script，等价于 python -m vault.cli
uv run python tests/test_time.py     # 时区回归测试（10 个用例）
```

常用操作：

| 场景 | 命令 |
|---|---|
| 加依赖 | `uv add <pkg>`（自动更新 uv.lock 和 .venv） |
| 删依赖 | `uv remove <pkg>` |
| 升级全部依赖 | `uv lock --upgrade && uv sync` |
| 只按锁文件装，不解析 | `uv sync --frozen` |

Docker 构建同样走 uv：`uv sync --frozen --no-dev`，保证容器里的版本与 `uv.lock` 完全一致。

---

## 把现有相册"快捷"整理进来（家庭机场景）

关键判断：**已有 2TB 数据在机器上，不要搬。** 拷贝 2TB 要双倍空间 + 双倍时间，
而硬链接（`--mode link`）只是给同一个 inode 多起一个名字：**零额外空间、耗时约等于读一遍**。

### 前置：只挂一个父目录

Docker 里**每个 bind mount 是独立设备**，素材和 vault 分开挂载时 `link()` 会返回 `EXDEV`，
只能退回拷贝。所以要让素材和 vault 处在同一个挂载点里：

```
D:\photo\                 ← HOST_ROOT，只需要挂这一个
    ├── vault\            ← 新建，空目录，本系统写入（内容寻址）
    ├── derived\          ← 新建，缩略图/代理，随时可删可重建
    └── source\           ← 你现有的相册，原样不动
```

`.env`：

```ini
HOST_ROOT=D:\photo
```

> `HOST_ROOT` 不能指向 `Z:\` 这类 SMB 网络盘——既不支持硬链接，Docker 也挂不进去。

### 四步走

```bash
# ① 体检（只读，不改任何文件）。先看清有多少东西、多少重复、时间可不可信
docker compose run --rm app python -m vault.cli audit /host/source
#    → 报告在 host/derived/audit/report.md

# ② 干跑（只扫描、算哈希、探测元数据；不落盘、不写库）
docker compose run --rm app python -m vault.cli ingest /host/source --mode link --dry-run

# ③ 正式入库
docker compose run --rm app python -m vault.cli ingest /host/source --mode link

# ④ 起服务，在浏览器里看
docker compose up -d app
```

再跑一次 `ingest` 是安全且廉价的——**幂等**，已入库的全部跳过（实测 108 文件/s）。

### link 模式到底做了什么

| | copy | link |
|---|---|---|
| 额外磁盘占用 | 双份 | **0**（实测 `du -sh vault` = 0） |
| 耗时 | 读 + 写 | **只读一遍** |
| 源文件被删 | 库里还有 | **库里还有**（同一 inode 仍被引用） |
| 源文件被原地改写 | 不影响库 | ⚠️ **会同时改到库**（同一 inode） |

所以：**入库之后不要再对 `source\` 里的文件做原地编辑**（比如批量写 EXIF）。
重命名、移动、删除都没问题；只是别原地改内容。介意这一点就用 `--mode copy`。

### "整理好"之后怎么收拾旧目录

入库完成后 `source\` 那套乱目录就不再重要了——所有原件在 `vault\` 里都有硬链接。
等你确认浏览正常、备份到位，就可以逐步删掉 `source\` 里的重复目录来释放空间，
**库不受影响**（已实测：删掉源文件后 vault 副本 md5 不变）。

---

## AI 标签（ML worker，RAM++）

打标只走一条路径：**RAM++ ONNX INT8**（4585 类开放词表，逐标签 sigmoid + 官方逐标签阈值），
输出**中文标签**写入 `asset_tags(source='ai')`，在 Web 界面按标签精确筛选（狗 / 日落 / 食物 / 室内）。

标签表（4585 行官方英文表 + 中文对照表 + 逐标签阈值）已内置于 `ml/assets/ram/`，
与 ONNX 权重的输出顺序一一对应。纯 ONNX Runtime 推理，**不依赖 torch/transformers**，
CPU 约 700ms/张，峰值内存 ~1.8GB。

### 模型权重（手动下载，放 `models/` 下）

| 文件 | 大小 | 放到 | 直链 |
|---|---|---|---|
| RAM++ INT8 | 873 MB | `models/ram_plus_int8.onnx` | <https://hf-mirror.com/anakhiu/ram-plus-onnx-int8/resolve/main/ram_plus_int8.onnx> |

校验（sha256）：`44836da6724dcbdd7446632599c190426af96bd47d4457d47d876b019e10f8bd`。
可选环境变量：`RAM_THRESHOLD_CAP=0.65`（压回高阈值提召回，官方阈值=1.0 的 10 个标签
仍保持关闭）、`RAM_MAX_TAGS=32`（单图标签数上限）、`RAM_PROVIDERS`（ONNX 执行后端，默认 CPU）。

worker 跑在宿主机（onnxruntime-gpu / opencv 在 `ml` 依赖组，不进 app 镜像），只连 DB 拉活 + 读 vault 文件，可跨机部署：

```bash
# ① 待处理资产入队（jobs 表）
uv run --group ml python -m ml.enqueue

# ② 启动 worker：消费队列打标（--task tags 只打标，--task faces 只跑人脸）
$env:DATABASE_URL='postgresql://vault:vault@localhost:5433/vault'
$env:VAULT_ROOT='E:\photo\vault'
uv run --group ml python -m ml.worker --task tags
```

视频用入库时的海报帧打标。换打标模型 = 词表变化：跑迁移（`photo-vault initdb`）
会清空旧 `source='ai'` 标签与旧任务，再 `ml.enqueue` 全量重算。

---

## 人脸识别（InsightFace + ONNX Runtime CUDA）

检测用 **SCRFD 10G**（`det_10g.onnx`，640×640 输入、5 个关键点），识别用 **ArcFace R50**
（`w600k_r50.onnx`，112×112 输入、512 维向量）——就是 InsightFace 官方 buffalo_l 包里的
两张图，直接喂 ONNX Runtime，**完全不用 TensorRT**。

> 为什么不 `pip install insightface`：它硬依赖 CPU 版 onnxruntime，和 onnxruntime-gpu 同装会
> 往同一个 `onnxruntime/` 目录写两份文件，Python 版本和 CUDA DLL 随机对不上。本项目只需要
> 两张 ONNX 图，于是照官方实现逐行对齐了前/后处理（letterbox、anchor 解码、Umeyama 5 点对齐、
> ArcFace 归一化）。实测同一张图上与本项目的输出：**检测框 0.00px 偏差、向量余弦 0.99999+**。

### Pascal 上的两个坑（1070 Ti 实测）

| 项 | 结论 |
|---|---|
| 显存 | 两个模型常驻约 0.65 GB（实测增量，含 CUDA context），8 GB 绰绰有余；检测 20–30 ms/张 |
| CUDA | **必须 CUDA 12**：CUDA 13 已放弃 Pascal（SM 6.1），装 13 会静默掉回 CPU |
| onnxruntime-gpu | **>=1.24.4,<1.27**：1.23 及以下缺 RAM++ INT8 图要的 `ConvInteger`（加载即 `NOT_IMPLEMENTED`）；1.27 起官方转 CUDA 13。`ml` 依赖组里已锁好 |
| TensorRT | **不要用**：TRT 10 要求 SM 7.5+，在 1070 Ti 上只会退回 CPU/CUDA 重编译，白折腾 |

> 这两条边界都是实测撞出来的，不是抄文档：1.22 / 1.23 加载 RAM++ 直接报
> `Could not find an implementation for ConvInteger`，1.27 / 1.28 的 CUDA EP
> 初始化失败（提示需要 CUDA 13），只有 1.24.4–1.26 同时满足。

自检：`uv run --group ml python -m ml.faces --images 某张合影.jpg`，打印的 providers 必须
含 `CUDAExecutionProvider`；只有 `CPUExecutionProvider` 说明 CUDA/cuDNN 没在 PATH 上。
纯逻辑（命名 / 并查集 / 对齐 / NMS）的回归测试不读模型、不连库：

```bash
uv run --group ml python tests/test_faces.py
```

### 模型权重（手动下载，放 `models/` 下）

| 文件 | 大小 | 放到 |
|---|---|---|
| `det_10g.onnx` | 16 MB | `models/insightface/buffalo_l/` |
| `w600k_r50.onnx` | 166 MB | `models/insightface/buffalo_l/` |

一条命令搞定（下载 buffalo_l.zip 275 MB、校验 sha256、只留上面两个文件，其余 143 MB 直接丢掉）：

```bash
uv run --group ml python -m ml.faces --download
```

### 三步跑完

```bash
# ① 入队：kind='faces'（--kind tags 是 RAM++ 打标，默认两者都排）
uv run --group ml python -m ml.enqueue --kind faces

# ② 提取：检测 → 5 点对齐 → 512 维向量 → faces 表 + derived/faces/ 头像
uv run --group ml python -m ml.worker --task faces

# ③ 聚类：匿名 Person A/B/C
uv run --group ml python -m ml.cluster
```

`ml.worker --task` 可切 `tags` / `faces` / `all`（默认）；`--once` 清空队列后退出，适合计划任务。
视频用海报帧（`faces.source='poster'`），HEIC 直接解原件。

### 聚类规则：匿名 + 手动命名

1. **认领**：未归类的人脸先跟已有人物比最近邻，相似度 ≥ 0.45 就挂过去——每天新导入的照片
   不必重算全库；
2. **聚类**：剩下的按余弦距离 ≤ 0.45（≈相似度 0.55）建邻域图，取连通分量；
3. **过滤**：成员少于 3 张脸的簇不建人物（一两张脸的簇基本是海报帧、路人、误检）——
   宁可少建，也不要 UI 里冒出几千个"只有一张照片的人"；
4. **命名**：新人物按簇大小降序，取下一个没被占用的字母：Person A…Z、AA…；
   封面取簇内人脸面积最大的一张。

命名与合并在网页「人物」里做：点进人物 → 右上角「重命名」；聚类把一个人拆成两个时，
进其中一个点「合并」再选另一个。觉得拆得太碎/并得太狠就调阈值：

```bash
python -m ml.cluster --eps 0.40 --min-faces 2    # 更松：少拆散，但更容易混
python -m ml.cluster --min-faces 5              # 更严：只收"常出现的人"
```

误检（比如把海报上的表情包当成一个人）就点进那个人物 →「合并」→ 底部
「取消这个人物」，它的脸会回到未归类。

### 换模型 / 重算

```sql
delete from faces; delete from persons;           -- 或只删要重算的那部分
delete from jobs where kind = 'faces';
```

再跑一遍 ①②③。当前模型代次记在 `ml_meta.face_model`（`buffalo_l:det_10g+w600k_r50`），
排障时先核对这个值。

---

## 视频打标（关键帧 + Qwen3-VL 4B）

视频没有"一张图"可打，但也没必要打满全片：成本随帧数线性涨，而一段几分钟的短视频
往往只有两三个镜头。流程是 **抽 3–5 张差异最大的帧 → 逐帧问 4B VLM → 按票数聚合成视频级标签**。

### 关键帧：均匀候选 + 最远点采样

`ml/keyframes.py`：

1. 按时间均匀取 K 个候选点（跳过首尾 0.3s 的淡入淡出），每个点用 `ffmpeg -ss` 单独 seek 取帧
   —— 4 GB 的家庭录像整片解码要几分钟，seek 取 12 帧只要 1–2 秒；
2. 每帧缩成 32×32 灰度指纹（去均值、单位化），余弦距离当"画面差异"；
3. 黑场/纯色帧直接丢（均值 < 10 且标准差 < 3）；
4. **最远点采样**挑彼此差异最大的 N 帧，差异不够就少给几张 —— 静止镜头重复喂图没有意义。

落盘成长边 768 的 JPEG：`derived/keyframes/<sha[:2]>/<sha[2:4]>/<sha>/f00.jpg`，
库里的 `video_frames` 只存相对路径 + 时间点，几秒就能重建。

> PEEK 那类"预算约束下的关键帧选择"没有上：个人相册这个量级（1500 段视频）用
> 均匀候选 + 差异筛选已经够用，多一个模型就多一份版本矩阵要维护。等视频到几万段再说。

### 为什么是 llama.cpp，不是 ONNX Runtime

| 路线 | 在 1070 Ti（Pascal / SM 6.1）上的结果 |
|---|---|
| ONNX Runtime int4（`MatMulNBits`） | CUDA kernel 要求 **SM 7.5+**，Pascal 上静默退回 CPU |
| vLLM（GPTQ / AWQ / Marlin） | 要求 **SM 7.0 / 8.0+**，1070 Ti 直接不支持 |
| PyTorch + bitsandbytes 4bit | 能跑，但 ViT 走 FP16 —— GP104 的 FP16 是 FP32 的 **1/64**，比 fp32 还慢 |
| **llama.cpp CUDA（GGUF K-quant）** | ✅ f16 权重 × f32 激活，走 FP32 FMA，这才是 Pascal 上唯一成熟的路 |

所以视频打标是**第二套运行时**（llama.cpp 子进程 + GGUF），不能跟人脸共用 ONNX EP。
显存是分开算的：VLM 峰值 ~4.6 GB（含 0.8 GB 视觉塔）+ 桌面 1.6 GB，人脸引擎那 0.65 GB
还能塞下，但**别同时开两个 worker**。

### 模型权重

| 文件 | 大小 | 放到 |
|---|---|---|
| `Qwen3-VL-4B-Instruct-Q4_K_M.gguf` | 2.4 GB | `models/qwen3-vl-4b/` |
| `mmproj-F16.gguf`（视觉塔） | 800 MB | `models/qwen3-vl-4b/` |
| llama.cpp `b12018` win-cuda-12.4 | 267 MB | `models/llama.cpp/bin/` |

一条命令（HF 直连不通会自动改道 hf-mirror；llama.cpp 从 GitHub Releases 下）：

```bash
uv run --group ml python -m ml.vlm --download
```

自检（打印每张图的描述/标签/OCR 和耗时）：

```bash
uv run --group ml python -m ml.vlm --images derived/keyframes/…/f00.jpg --json
```

### 三步跑完

```bash
# ① 入队：只排视频（1527 段，已打过标的会跳过）
uv run --group ml python -m ml.enqueue --kind video

# ② 抽帧 + 推理：跑完队列退出加 --once；--max 60 是试跑
uv run --group ml python -m ml.worker --task video
```

③ 网页里看：视频灯箱下方出现「AI 描述」、关键帧缩略图（点一下跳到对应时间）和画面文字。
`--task all` 会三类任务一起接（缺模型时自动跳过 video，不会把任务跑成 dead）。

### 标签怎么收敛

4B 模型自由发挥会造出几百个同义标签，所以：

- **受控词表** 120 个（地点/人物/活动/物体/拍摄风格，见 `ml/vlm.py` 的 `TAG_VOCAB`），
  词表外的直接丢；模型说"人物/儿童/合照"这类别称，先过 96 条的 `TAG_ALIASES` 收敛成"有人/孩子/合影"；
- **票数聚合**：在 ≥50% 的关键帧里出现的标签才算视频级（4 帧 → 至少 2 票）；
  一票都没有（比如 4 帧各说各的）就取票数前二，避免视频一个标签都没有；
- **互斥词**：同一段视频既"室内"又"室外"说明模型在猜，只留票高的；
  平票就都不留 —— 宁可少一个标签，也不给库留矛盾；
- **蕴含词**：有"屏幕录制"就不留"屏幕"；
- 视频描述取"标签最多"的那一帧（并列取中间帧），OCR 跨帧按去空格比较去重。

聚合结果同时写进 `video_summaries.labels`（带票数）和 `asset_tags(source='vlm')`，
所以标签筛选、标签云、搜索都直接复用现有通路。**调聚合规则不用重跑模型**：

```bash
uv run --group ml python -m ml.vlm --reaggregate            # 几秒钟
uv run --group ml python -m ml.vlm --reaggregate --min-share 0.75
```

### 实测（1070 Ti 8G）

| 项 | 数字 |
|---|---|
| 模型加载 | 3 s（页缓存命中）/ 35 s（冷启动，含 CUDA 上下文） |
| 单帧 | ~2.0 s（prompt eval 600 tok/s + 生成 42 tok/s） |
| 一段视频（4 帧） | 9–14 s（含抽帧） |
| 显存峰值 | 6.2 GB（含桌面 1.6 GB，模型+KV+视觉塔 4.6 GB） |
| 全量 1524 段 | 约 3–4 小时，可随时中断 —— 队列在库里，重新起 worker 接着跑 |

---

## 已实现

| 能力 | 状态 | 说明 |
|---|---|---|
| 内容寻址存储 | ✅ | `vault/ab/cd/<sha256>.<ext>`，原件只增不改 |
| 入库前体检 | ✅ | `audit`：规模、格式、年份、重复、时间可信度、风险文件，输出 md + json |
| 硬链接入库 | ✅ | `--mode link`：同卷零额外空间，实测 `du` = 0 |
| 拷贝入库 | ✅ | `--mode copy`：独立副本，最安全 |
| 原子入库 | ✅ | 落 landing → 算哈希 → `os.replace` → 事务落库 |
| 幂等 / 断点重入 | ✅ | 幂等键 `(sha256, role)` |
| 精确去重 | ✅ | 跨设备/跨卡只留一份，记录所有来源 |
| EXIF 提取 | ✅ | 机型、镜头、ISO、光圈、快门、焦距、GPS、朝向 |
| 时间三件套 | ✅ | `taken_at_local` + `tz_offset_min` + `taken_at_utc` |
| 时区推断 | ✅ | EXIF Offset → GPS 反算（吸附 15 分钟）→ 默认时区 → mtime |
| HEIC/HEIF | ✅ | iPhone 主力格式 |
| 视频 | ✅ | ffprobe 元数据、creation_time、封面抽帧 |
| 视频打标 | ✅ | 关键帧（均匀候选 + 最远点采样）→ Qwen3-VL-4B 逐帧标注 → 票数聚合 |
| 按原目录浏览 | ✅ | `origin_rel` + `/api/folders`，Web 上直接筛"原来放在哪个文件夹" |
| 反向对账 | ✅ | `reconcile`：磁盘有、库里没有的文件重新登记（顺带校验哈希） |
| 环境体检 | ✅ | `doctor`：磁盘、原子性、DB、ffmpeg、未登记文件 |
| Live Photo 配对 | ⚠️ | 同目录同文件名配对，未用 ContentIdentifier |
| 近似去重 | ⚠️ | pHash 已入库，聚类未做 |
| RAW | ❌ | CR3/NEF/ARW/DNG 只能落盘，读不出元数据 |
| 人脸检测 / 识别 | ✅ | SCRFD 10G + ArcFace R50（InsightFace ONNX，CUDA），512 维向量 |
| 人物聚类 | ✅ | 匿名 Person A/B/C，网页里手动命名 / 合并（`ml.cluster`） |
| VLM 视频描述 / OCR | ✅ | Qwen3-VL-4B-Instruct Q4_K_M（llama.cpp CUDA），4 帧/段，~2 s/帧 |

---

## 目录结构

```
photo-vault/
├── pyproject.toml            # 项目元数据 + 依赖声明（uv）
├── uv.lock                   # 锁定文件：本地与 Docker 装同一套版本
├── .python-version           # 固定 Python 3.12（与镜像一致）
├── docker-compose.yml        # db(pgvector/pg16) + app，单挂载点
├── Dockerfile                # 基于 uv sync --frozen 安装依赖
├── db/
│   ├── 001_init.sql
│   ├── 002_provenance.sql  # storage_mode / origin_path / origin_rel
│   ├── 003_engagement.sql  # favorite / trash / tags
│   ├── 004_semantic.sql
│   ├── 005_ram_tags.sql    # RAM++ 单轨 + ml_meta
│   ├── 006_faces.sql       # persons / faces / 512 维 HNSW
│   └── 007_video.sql       # video_frames / video_summaries
├── vault/
│   ├── config.py
│   ├── db.py
│   ├── hashutil.py         # 流式 sha256 / pHash / 内容寻址
│   ├── meta.py             # EXIF / ffprobe / 时区判定 / 缩略图
│   ├── audit.py            # 入库前体检
│   ├── ingest.py           # 入库状态机（copy / link）
│   ├── cli.py              # initdb | audit | ingest | stats | doctor | reconcile | serve
│   └── web.py              # FastAPI + 单页 UI（图库 / 人物 / 标签）
├── ml/
│   ├── faces.py            # SCRFD 检测 + ArcFace 识别 + 模型下载
│   ├── cluster.py          # 人脸聚类 → 匿名人物
│   ├── keyframes.py        # 视频抽帧（seek 取帧 + 最远点采样）
│   ├── vlm.py              # Qwen3-VL（llama.cpp 子进程）+ 标签聚合
│   ├── pgvector.py         # vector 文本格式读写
│   ├── model.py            # RAM++ 打标
│   ├── worker.py           # jobs 消费（tags / faces / video）
│   ├── enqueue.py          # 入队
│   └── assets/ram/         # 4585 类标签表 + 阈值 + 中文对照
├── scripts/make_fixtures.py
└── tests/
    ├── test_time.py        # 时区回归测试（10 个用例）
    ├── test_faces.py       # 命名 / 并查集 / 对齐 / NMS（无模型、无库）
    └── test_video.py       # 抽帧 / 聚合 / 输出解析（无模型、无库）
```

---

## 几个关键设计（踩过坑才写得出来）

**1. 原件不可变。** 进 vault 后永不移动、不改名、不写回。整理全是数据库视图。

**2. landing 必须与 vault 同文件系统。** 否则 `os.replace` 退化成跨盘拷贝，失去原子性，`doctor` 会检查。

**3. 时间是三件套，不是一个字段。** EXIF 只有墙上时间、没有时区，相机时钟还经常是错的。
排序与聚类只看 `taken_at_utc`，展示才用 local。每条记录都带 `tz_source`，让 UI 能标"这个时间不可信"。

**4. 幂等键是内容哈希，不是文件名。** `DJI_0001.MP4` 每张卡都会重名。

**5. 单挂载点不是洁癖，是硬性要求。** 跨 bind mount 的硬链接必然失败（EXDEV）。

---

## 家庭机算力：GTX 1070 Ti 8G 能干什么

| 任务 | 可行性 | 说明 |
|---|---|---|
| 人脸检测/聚类（InsightFace） | ✅ 很轻松 | 8G 绰绰有余 |
| 物体检测（YOLO / Grounding DINO） | ✅ 轻松 | |
| 图像打标（RAM++ ONNX） | ✅ 很轻松 | CPU ~700ms/张，无 GPU 依赖 |
| OCR（PaddleOCR） | ✅ 可用 | |
| 语音转写（Whisper） | ✅ 可用 | |
| **VLM 视频打标（Qwen3-VL-4B Q4）** | ✅ **可用** | 4.6 GB 显存（含视觉塔），~2 s/帧 |

1070 Ti 是 Pascal：没有 Tensor Core，FP16 只有 1/64 速率，所以文本/多模态模型都得走
**4-bit 权重量化 + FP32 激活**（llama.cpp 的 K-quant 正是这么算的）。实测 Qwen3-VL-4B Q4_K_M：
prompt 600 tok/s、生成 42 tok/s、单帧 2 秒 —— 比早先"3B 级 30–60 秒/张"的估计快一个量级，
因为真正的瓶颈一直是**运行时有没有 Pascal 的 kernel**，不是算力不够。

8B 的 4-bit 权重 5 GB 起步，加上 KV 和视觉塔会顶到 8 GB 边界，且多出来的算力在 Pascal 上
大部分被显存带宽吃掉，不建议。


**架构上的应对**：ML worker 必须能**跨机部署**——只从 `jobs` 表拉活、回写结果。
今天跑在家庭机 1070 Ti 上，将来换卡或换机器，库和 API 一行不改。

---

## 已知边界

- **RAW 未支持**：需要 exiftool 或 libraw。你现在没相机，优先级低。
- **Live Photo 配对**靠文件名，改名会失效。
- **连拍归栈**、**近似去重聚类**、**时空事件聚类**都还没做。
- **多用户**：表里没有 user 轴。
- **备份**：本服务不做备份。vault 必须挂到有快照 + 异地副本的位置。
- **视频打标只看画面**：没有音轨（ASR）、没有人脸身份合并（视频里的人脸来自海报帧），
  关键帧也覆盖不到"一闪而过"的镜头。要按说话内容找视频，得再加 Whisper。
- **关键帧是镜头级抽样**：一段 30 分钟的长录像只取 4 帧，适合"这段视频大概是什么"，
  不适合"精确找到第 17 分 32 秒的那句话"。
