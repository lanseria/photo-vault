# Photo Vault · P0

本地优先的个人照片/视频资产管理服务。目标不是"又一个相册 App"，而是**一个能活 10 年、原件永不损坏、换任何软件都读得出来**的媒体库。

当前实现的是整条链路上**最危险、也最不依赖算力**的一段：**入库**；
AI 打标（RAM++）和人脸（InsightFace）已经接上，都在宿主机 worker 里跑。
数据模型、队列、内容寻址从一开始就为它们留好了位置。

在相册之外还有两块：**素材库**（同一张 assets 表的 `library` 轴，AI 结果复用，
用来手动整理可剪的素材）和**影视库**（电影/剧集/字幕，只登记路径，不搬家、不跑 AI）。

---

## 30 秒跑起来

```bash
cp .env.example .env
docker compose up -d db
docker compose run --rm app python -m vault.cli initdb
docker compose run --rm app python -m vault.cli ingest /host/source --mode link
docker compose up -d app
# 打开 http://localhost:7878
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

## 前端（Vue 3 + Vite + UnoCSS）

界面是独立的单页应用，代码在 `frontend/`，构建产物落在 `vault/static/`，由 FastAPI 直接托管
（`COPY vault ./vault` 时一起进镜像）。仓库里带了一份构建产物，所以只改后端时不用装 Node。

```bash
cd frontend
pnpm install
pnpm build          # 产出到 ../vault/static，刷新页面即可
pnpm dev            # 开发模式：5173，/api 与图片请求代理到 7878
```

`pnpm dev` 默认代理 `http://127.0.0.1:7878`（`.env` 里的 `APP_PORT`）；
换端口用 `VITE_BACKEND=http://127.0.0.1:9000 pnpm dev`。

### 路由

| 路径 | 内容 |
|---|---|
| `/gallery` · `/material` | 图库 / 素材库 |
| `/gallery/favorites` · `/material/favorites` | 喜欢（按库） |
| `/gallery/tags/:tag` · `/material/tags/:tag` | 标签（按库） |
| `/people` · `/people/:id` | 人物墙 / 某个人的照片 |
| `/bin` | 剪辑箱 |
| `/cinema` · `/cinema/:id` | 影视库 / 条目详情 |
| `/trash` | 最近删除 |

旧链接（`/?view=fav`、`/?view=tag&tag=…`、`/?view=person&person=<id>`、`/?view=cinema&title=<id>`）
由路由重定向到上面的新路径。

### 刷新为什么不会跳回顶部

1. **视图和筛选都在 URL 里**（`?q=` `?kind=` `?dur=` `?px=`），刷新即还原；
2. **滚动位置按「路径 + 查询」存 sessionStorage**，刷新、前进后退都会恢复；
3. **分页会补齐**：恢复滚动之前先按需往后拉几页 —— 否则页面还没那么高，`scrollTo` 会被浏览器夹到底；
4. **灯箱也是 URL**（`?photo=<id>`），刷新后还停在同一张；全屏浮层期间暂停记录滚动，
   避免 `overflow:hidden` 把位置归零后覆盖记忆。

### 顶部固定区

标题、标签页、筛选器包在一个 `position: sticky` 的顶栏里（`App.vue` 的 dock），
滚动时只有时间轴动；日期头条吸附在顶栏下沿（高度由 ResizeObserver 同步成 `--dock-h`）。
顶栏毛玻璃 + 半透明黑，滚起来之后才显示 1px 分隔线。滚动容器仍是整个文档，
所以滚动恢复、无限加载、浮层锁滚这些机制都不受影响。

### 分组与时间轴

- **分组粒度**：筛选条最右侧「分组 日 / 周 / 月 / 年」，状态在 URL（`?group=week` 等，
  默认按日不带参数）。刷新、分享链接都保持同一分组；不同分组各自记忆滚动位置。
  周 = 周一起算，标题显示日期范围（如 `2026年10月26日 – 11月1日`），跨年两端都带年份。
- **右侧时间轴**（`TimelineScrubber.vue`）：有两个以上分组时，窗口右缘出现刻度轨道，
  刻度位置与各分组在页面里的实际高度成比例（按日上千段时自动抽样到 ≤140 个刻度）。
  分组不多（按年 / 按月）时，刻度旁直接标出年份 / 月份，当前所在时间段高亮；
  按住拖动立即滚动到对应位置，同时弹出当前分组名（如「2026年9月」）；松手即停。
  仅照片流页面（图库 / 素材库 / 喜欢 / 标签 / 人物 / 回收站）有，设备、影视等页面没有。
- **无限滚动兜底**：分组后页面可能只有一屏（按年时尤其明显），哨兵一加载完就落在
  `rootMargin` 里，`IntersectionObserver` 没有"进入"事件就不会再拉页。所以每页合并后
  都用 rAF 量一次哨兵位置，还在视口附近就继续拉，直到页面真的能滚；拖动时间轴导致
  页面变长时，拇指会重新贴住原来的比例。

筛选栏从左到右：类型 / 时长 / 清晰度 / **设备（下拉）** / **标签（下拉）** / 分组。
「标签」入口从导航栏挪到了这里，和「设备」并排；点开是标签列表（带数量），选一个进入
`/<库>/tags/<名字>`，在标签视图里再点它就回库。

图层顺序：`AppHeader` / `NavChips` / `FilterBar` 是外壳（吸附顶栏，z-40），
`RouterView` 是内容（时间轴轨道 z-30），灯箱、标签面板、上传面板、toast 是全局浮层。
资产的所有写操作集中在 `src/composables/useAssetActions.ts`，组件只负责「点了哪个按钮」。

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
$env:VAULT_ROOT='D:\photo\vault'
uv run --group ml python -m ml.worker --task tags
```

视频用入库时的海报帧打标。换打标模型 = 词表变化：跑迁移（`photo-vault initdb`）
会清空旧 `source='ai'` 标签与旧任务，再 `ml.enqueue` 全量重算。

---

## 人脸识别（InsightFace + ONNX Runtime CUDA）

检测用 **SCRFD 10G**（`det_10g.onnx`，640×640 输入、5 个关键点），识别用 **ArcFace R50**
（`w600k_r50.onnx`，112×112 输入、512 维向量）——就是 InsightFace 官方 buffalo_l 包里的
两张图，直接喂 ONNX Runtime，**完全不用 TensorRT**。

> **素材库不跑人脸**：`library='material'` 的资产不排 `faces` 任务、worker 见到会直接
> 收掉；把照片移进素材库时，已有的脸（`faces` 行 + `derived/faces` 裁剪图）一并清掉。
> 打标（RAM++）和视频摘要照常。所以「人物」页和人物检索里只有图库的人。

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

## 剪辑交付：搜索 → 剪辑箱 → 拖进剪映

相册整理完只是"存得好"；剪辑要的是"取得快"。这一段把素材送到剪辑软件门口。

### 两个硬边界（决定了架构）

1. **浏览器拖不出文件。** 从网页拖一个 `<img>`／`<a>` 到剪映，外部程序只能拿到
   `text/uri-list`，拿不到真实路径——Chromium 出于安全不会把本地路径交出去。
   所以"拖拽"这个动作必须有原生进程参与，纯 Web 做不到。
2. **容器摸不到桌面。** app 跑在 Docker 里（`/host` → `D:\photo`），进程在 Linux 命名空间，
   `explorer.exe`、剪贴板、OLE 拖拽全都调不到。

于是分工：**容器只负责数据**（查库、建硬链接、写清单），**宿主机桥负责桌面**
（`vault/bridge.py`，`127.0.0.1:8099`）。桥没起也能用，只是出稿后要自己复制路径。

### 用法（三步）

1. 顶栏筛选条：`🎬 视频` + `15–60s` + `≥1080p`；搜索框同时搜文件名、原目录、
   AI/VLM 标签、视频描述、OCR、人物名；
2. 「选择」→ 勾选 → 底部「剪辑箱」（灯箱里也有）。**可以跨多次搜索反复加**——
   箱子在数据库里，不是页面临时变量；
3. 切到「✂️ 剪辑箱」→「出稿」→ **资源管理器自动打开并停在出稿目录** → 全选拖进剪映。

> 剪映不吃"粘贴文件列表"，所以交付的终点就是资源管理器：`出稿` 把文件摆好、打开目录，
> 你只需要框选拖一次。想再次打开上次的目录，剪辑箱页有「打开目录」。

### 出稿做了什么

`<HOST_ROOT>\derived\cuts\<箱名>\` 下生成：

| 文件 | 说明 |
|---|---|
| `001_20200917_115712_IMG_5135.MP4` | **硬链接**指向 vault 原件：零额外空间、瞬时、是真文件（不是快捷方式） |
| `cutlist.csv` | 序号／时长／分辨率／标签，Excel 直接开（UTF-8 BOM） |

RAW+JPEG 的照片会出**两个文件**：`001_20200917_115712_DJI_0498.JPG` 和
`001_20200917_115712_DJI_0498.DNG` —— 序号、拍摄时间、原名都一样，只有扩展名不同，
剪映里仍是一组。`cutlist.csv` 里也是两行，RAW 那行分辨率为 `?x?`。

命名是设计过的：`序号_拍摄时间_原名.ext`。剪映素材库默认按文件名排序，
于是**排序 = 你在箱子里的顺序**，同一时段的素材天然挨着。重新出稿会重排并清掉旧文件
（`stale_removed`），所以出稿目录是"每次覆盖"的幂等产物——但**剪映草稿记的是绝对路径，
剪完前别删这个目录**。

跨卷（`EXDEV`）自动退回拷贝，所以 derived 和 vault 不在同一卷时也能用，只是会占空间。

### 宿主机桥

```bash
uv run photo-vault bridge              # 前台跑，127.0.0.1:8099
uv run photo-vault bridge --install    # 一次性：设为开机自启（HKCU\...\Run，pythonw 无黑窗）
uv run photo-vault bridge --uninstall  # 撤销
```

剪辑箱页右上角有「桥 · 在线 / 未启动」的状态灯。桥没起也不影响出稿，只是不自动开文件夹，
路径会自动复制到剪贴板。

| 接口 | 作用 |
|---|---|
| `POST /reveal {path}` | 目录 → 打开；文件 → 打开所在目录并选中 |
| `POST /clipboard {paths}` | 文件列表进剪贴板（CF_HDROP）。**剪映不支持粘贴文件，仅备用**（Premiere/达芬奇可以） |
| `POST /open {path}` | 用系统默认播放器打开影视文件（MKV/AVI 浏览器放不了） |
| `GET /health` | 前端探状态 |

只绑 127.0.0.1，且只接受 `127.0.0.1` / `localhost` 页面的跨域请求——否则任意网页都能
往你剪贴板里塞文件。

**不想要这个常驻进程？** 把 web 也跑在宿主机上就不需要桥：

```bash
$env:DATABASE_URL='postgresql://vault:vault@localhost:5433/vault'
$env:VAULT_ROOT='D:\photo\vault'; $env:DERIVED_ROOT='D:\photo\derived'
uv run photo-vault serve --port 7878
```

这种部署下 app 进程本身就在 Windows 上，出稿时后端直接 `ShellExecute` 开文件夹
（deliver 返回 `opened=true`，前端就不再去问桥）。代价是失去容器的隔离和自动重启。

### 还没做的

- **原生拖拽**：从网页直拖不行，要一个常驻浮窗（`IDataObject` + `IDropSource`）。
  体验最好，但那是 1–2 天的 COM 活；现在"开目录 + 框选拖"只多一下。
- **剪映草稿**：跳过导入直接成片，但 draft schema 跟剪映版本强绑定（你装的是 11.6），
  风险高于收益。
- **素材使用记录**：同一段素材被哪个片子用过、用了第几秒，还没记。
- **代理**：4K HEVC 多轨预览卡的话再做（1080p H.264 同 basename，剪完「替换」回原片）。

---

## 素材库：相册之外的第二个归属

素材库不是"另一个库"，而是同一张 `assets` 表上的 `library` 轴（`gallery` / `material`）。
标签、人脸、视频摘要、剪辑箱全部复用 —— **换归属不复制文件，也不重跑 AI**。

### 用法

1. 顶部切到「图库」，点右上角「选择」→ 勾选素材（有「全选」）→ 底部「素材库」；
2. 切到「素材库」，底部按钮变成「图库」，可以整批移回去；
3. 素材库里的「AI 识别」把选中项（没选中就是当前加载的全部）排进 `jobs`：
   RAM++ 打标 / 人脸 / 视频关键帧。已经排过、跑过、或正在跑的会被跳过，重复点不涨队列；
4. 手动整理靠已有的标签、喜欢、搜索和筛选轴（类型/时长/分辨率），不用学新东西。

也可以命令行直接导入素材库，不经过"先进相册再移动"：

```bash
uv run photo-vault ingest D:\素材 --mode copy --library material
```

### 设计取舍

| 选择 | 原因 |
|---|---|
| `assets.library` 单值列，不是多对多 | 一件素材要么是"回忆"、要么是"可用的素材"；两边都能看到会让"删的是哪份"变成歧义 |
| 不复制文件、不复制标签 | 素材库的价值是"筛出来"，不是"再存一份"；复制会让 2TB 变 4TB |
| 回收站是全局的 | 误删要能找回来，恢复后回到原来的库（`library` 列不变） |
| 收藏 / 标签视图跟着当前库走 | 在素材库里点 ❤️ 看到的是素材的收藏，标题会标「· 素材库」；人物是跨库实体视图，不分库 |

迁移（`photo-vault initdb`）给所有老数据补 `library='gallery'`，相册不会变空。

---

## 网页导入：拖拽即入库

日常新增的照片 / 视频不用再从盘里扫：打开 `http://localhost:7878`，点右上角
「导入」，把文件（或整个文件夹）拖进窗口，选「图库 / 素材库」，剩下的它自己做。

- 收流直接写 landing（与 vault 同一文件系统），**边收边算 sha256**；重复内容按
  哈希当场跳过，新文件 `os.replace` 进 vault —— 全程只写一次盘，GB 级视频也不用
  先落容器临时盘再拷一遍。
- 和 `vault ingest` 是同一条流水线：EXIF / ffprobe、时区、Live Photo 配对、缩略图、
  AI 入队全部一致。上传的资产和扫盘进来的没有区别。
- 一次拖拽 = 一个 batch：同批的 `HEIC + MOV` 会配成一张 Live Photo；重传同一个
  文件返回「已在图库 / 素材库」，不产生副本。
- 导入完成自动排进 `jobs`（打标 / 人脸 / 视频），ML worker 在跑就会接着处理。
- 文件夹可以整个拖，会递归展开；`.xmp`、`.aae` 这类伴生文件自动忽略。
- 深链 `http://localhost:7878/#upload` 直接打开导入面板。

批量搬旧相册还是用 CLI 更快：上传受 Docker bind mount 限制，一次几千个文件不如
`vault ingest` 直接读盘。

---

## iOS App 接入（相册同步）

iOS 端只做"把 PhotoKit 里的照片/视频推过来"，服务端已经比网页导入需要的东西都齐了。
下面这些都是实测过的行为（HEIC + MOV 真上传过一遍）。

**地址**：`http://<电脑局域网 IP>:7878`（本机是 `192.168.31.115`；ZeroTier 地址
`10.144.14.190` 可从外网连）。容器监听 `0.0.0.0`，Windows 防火墙只需放行 Docker
Desktop 的后台规则（已允许 专用/公用）。

**跨域**：已开 CORS（默认 `*`，`CORS_ORIGINS=a,b` 可收紧）。原生 App 用 URLSession
本来就不受 CORS 限制；这条是给 WKWebView / 浏览器调试页用的。预检 `OPTIONS` 返回
200，`Range` 响应也带 `Access-Control-Expose-Headers: Content-Length, Content-Range,
Accept-Ranges`。

**ATS**：iOS 默认禁止明文 HTTP。连局域网 IP 时要在 Info.plist 里加
`NSAppTransportSecurity → NSAllowsLocalNetworking = true`（或对具体域名开例外）；
只连 `localhost` 才不需要。真要跨公网，建议反代加 HTTPS，别开
`NSAllowsArbitraryLoads`。

**鉴权**：服务本身没有登录（家庭内网定位）。要放到公网必须自己套一层反代 + Basic
Auth 或 VPN，否则任何能连到端口的人都能读删照片。

### 同步 Live Photo

PhotoKit 的 Live Photo 是一个 `PHAsset` 两个资源：`.photo`（HEIC/JPG 静帧）和
`.pairedVideo`（MOV）。按下面两条规则推，服务端会合成**一条** `live_photo` 资产：

1. 两个文件用**同一个 `batch`**（建议每张照片一个 UUID），文件名用同一个 stem：
   `IMG_0001.HEIC` + `IMG_0001.MOV`（扩展名不区分大小写）；
2. **先传静帧，再传动态**。静帧建资产并记住 `(batch, stem)`；动态到达时挂成
   `role='live_motion'`，资产 kind 变成 `live_photo`。顺序反了会变成一条独立视频。
   服务端重启或超过 6 小时会丢掉这个映射，重传静帧即可（内容重复会直接返回
   `duplicate`，不会产生副本）。

```http
POST /api/upload?name=IMG_0001.HEIC&library=gallery&batch=<uuid>   body = 文件字节
POST /api/upload?name=IMG_0001.MOV&library=gallery&batch=<uuid>    body = 文件字节
→ {"status":"imported","asset":{...kind:"live_photo"...},"queued":N}
```

响应里第二条的 `asset` 就是合成后的资产。重传返回 `"status":"duplicate"`。

### 同步需要的接口

| 用途 | 接口 |
|---|---|
| 推文件 | `POST /api/upload?name=&library=&batch=`，body 是原始字节（`URLSession.uploadTask(with:fromFile:)`） |
| 列照片 | `GET /api/assets?limit=100&offset=0&library=gallery`（`kind=video,live_photo` 可筛） |
| 单张详情 | `GET /api/assets/{id}` |
| 缩略图 | `GET /thumbs/{sha}.webp`（512px） |
| 预览图 | `GET /preview/{id}`（HEIC/RAW 会转 JPEG，≤2048px） |
| 原件 | `GET /file/{id}`（原始字节，支持 `Range`） |
| 实况视频 | `GET /file/{id}?role=live_motion`（支持 `Range`，`AVPlayer` 直接播） |
| 删除 | `POST /api/assets/{id}/trash` → `DELETE /api/assets/{id}`（进回收站再彻底删） |

分页是 `offset` 游标，按拍摄时间倒序；`total` 在响应里。上传是流式写盘，没有文件
大小上限，GB 级视频也能推（用文件 URL 上传，别把整个文件读进内存）。

---

## 拍摄设备：识别、命名、手工归类

`devices` 是设备字典：入库时从 EXIF 取 `Make` / `Model`，同一个机型只建一行。

**展示名**按优先级取：用户起的名字（`devices.label`）→ 归一化型号 → EXIF 原始型号；
三者都没有就是「未识别设备」。

**为什么会有未识别**：截图、微信 / QQ 保存的图、剪辑软件导出的视频，EXIF 里根本没有机型。
这些资产的 `device_id` 为空，网页上是这样体现的：

- 照片列表右上角有一个橙色 **?** 角标（悬停显示「未识别拍摄设备」）；
- 「设备」页顶部显示未识别的总数；
- 打开照片后，点标题下面的设备名，可以从**已有设备**里挑一台归过去，或者清回未识别；
- **素材库不显示这个标记**：素材库是剪辑素材，不按拍摄设备提示归类；那里的灯箱也只留
  中性的「📷 设备」入口，不再用警告色写「未识别设备」；
- 「导入」面板里，这一批如果有没认出设备的，底部会出现
  「N 项没识别出拍摄设备 → 选择设备」，选一次整批归过去，不用一张张改；
- 列表里也能批量改：点右上角「选择」→ 勾选照片 → 底部「设备」→ 选一台设备
  （或「未识别设备」清除原归类）。几十张截图一次归位，比逐张点开快得多。

**改名**：导航里的「📷 设备」→ 每行「重命名」。名字是展示用的标签，留空即恢复默认；
EXIF 原值仍保留在下面一行，方便对照是哪台机器。改完列表和灯箱会立刻跟着变
（列表接口返回的是 `label > canonical > raw` 算好的展示名）。

**新建设备**：「设备」页「＋ 新建设备」，填个名字、选类型（手机 / 相机 / 无人机 /
运动相机 / 其他）。用于 EXIF 认不出来、但你又确实想分类的机器，比如「大疆 Action 2」。
建出来是一台空设备，之后在照片详情或导入面板里把照片归过来。导入 / 灯箱的设备选择器
底部也有「新建设备…」，建完直接选中，少一步跳转。

**合并**：同一种机器常因为 EXIF 写法不同被建成两行（`iPhone 12 Pro Max` 和
`Apple iPhone 12 Pro Max`、`iPhone13,4` 和 `iPhone 12 Pro Max`）。在源设备那行点「合并」，
选目标设备即可：照片全部改挂过去，源设备删除。

> 合并只改 `assets.device_id`，照片的 EXIF 原值一个字都不动。所以以后再次导入
> 同型号的机器时，`ingest` 会按 EXIF 把源设备重新建出来 —— 想彻底归并，要靠改名
> 让展示名一致，或者接受它再出现一次。这是「不改原数据」的代价，也是有意的取舍。

这套逻辑只加了一列（`011_devices.sql` 的 `devices.label`），老数据不用迁移：
`label` 为空时展示行为与之前完全一致。

**按设备筛选**：筛选条上有「设备 ▾」下拉，列出全部设备（带照片数，多者在前）和
「未识别设备」，状态在 URL（`?device=<id>`，`?device=unknown`，可与其他筛选叠加），
刷新、分享都保持。设备页每行和未识别横幅上都有「看照片」直达对应筛选结果。

---

## RAW+JPEG：一张照片，两个文件

相机同时写 JPG + RAW（.DNG/.CR2/.NEF/.ARW…）时，**库里只有一条资产**：

- `files` 里是两行：JPEG 是 `role='original'`（缩略图、预览、宽高都以它为准），
  RAW 是 `role='raw'`；
- 列表里只出现一张，左下角标 **RAW+JPEG**；灯箱里同样只显示 JPEG，下面注明
  `RAW+JPEG · 24 MB`；
- **出稿时两个文件一起给**（同序号、同拍摄时间、同原名，扩展名不同），剪辑软件拿到的
  仍是一对 RAW+JPEG；搜索 / 筛选 / 计数都按"一张"算。

**配对规则**（`vault/ingest.py`）：同库、同目录、同文件名（忽略大小写）、拍摄时间相差
≤ 5 分钟。两个文件先后导入也会自动配对：RAW 先到时先建一条，JPEG 到达后合并成一条
（保留 JPEG 那条的 id）；反过来也一样。

历史数据用 `photo-vault pair` 补：老版本把一对存成了两条资产，这条命令把它们合并
（先加 `--dry-run` 看清单，确认后再去掉执行）。合并只改库记录：标签、喜欢、
设备、剪辑箱都会并到保留的那条上，vault 里的物理文件一个不动。

> 没有 JPEG 的纯 RAW 文件仍然是独立资产（`kind='raw'` 的图片），不在配对范围内。

---

## 影视库：只保存，不识别

电影 / 电视剧 / 字幕的登记与播放。和相册的取舍正好相反：
**文件留在原地，库里只存路径和元数据**，不搬进 vault、不算哈希、不跑任何模型。

### 为什么不能照搬相册

- 一部电影 10–50 GB，一部剧几十到上百 GB；`--mode link` 只对同卷有效，
  放到另一块盘就只能拷贝 —— 那等于把用户的硬盘复制一遍；
- 影视没有拍摄时间、没有 EXIF，不需要人脸/RAM++，塞进 `assets` 会污染相册的
  时间轴、统计、AI 队列和剪辑搜索；
- 播放、季集结构、字幕是另一套形状，单独三张表比往 `assets.payload` 里硬塞干净。

### 首次配置

影视目录通过**第二个挂载点**进容器，和 vault 无关，可以放在另一块盘：

```ini
# .env
MEDIA_HOST_ROOT=E:\Movies     # 你的电影/剧集目录；容器里看到的是 /media
```

```bash
docker compose up -d --build --force-recreate app   # 挂载点变了，必须重建容器
```

目录里可以混放电影和剧集，命名不用整理：

| 文件名 / 目录 | 识别结果 |
|---|---|
| `The.Matrix.1999.1080p.BluRay.x264-GROUP.mkv` | 电影《The Matrix》1999 |
| `让子弹飞.2010.1080p.mkv` | 电影《让子弹飞》2010 |
| `Breaking.Bad.S01E02.720p.mkv` | 剧集《Breaking Bad》S01E02 |
| `Breaking Bad - S01E02 - Cat's in the Bag.mkv` | 同上，带集名 |
| `Show/Season 2/03 - Bit by a Dead Bee.mkv` | 剧集《Show》S02E03 |
| `Anime/[SubsPlease] Frieren - 05 [1080p].mkv` | 剧集《Frieren》E05 |
| `进击的巨人 第01集.mp4` | 剧集《进击的巨人》E01 |

识别是**保守的**：没有季集证据的文件一律当电影（"Baraka - 01" 不会被拆成剧集）。
认错了就在网页里删掉条目重新导入，或命令行加 `--kind movie|series` 强制。

### 导入

```bash
# 先干跑：只打印解析结果，不写库
docker compose exec app python -m vault.cli cinema-import "E:\Movies" --dry-run

# 正式导入（可反复跑：同一文件是更新，不是重复）
docker compose exec app python -m vault.cli cinema-import "E:\Movies"

# 看结果
docker compose exec app python -m vault.cli cinema-list
```

也可以直接在网页「影视 → ＋ 添加影视」填路径，进度会实时显示（后台线程跑，不怕大库超时）。

### 字幕

导入时自动识别同目录的 sidecar：`Movie.srt` / `Movie.zh-CN.srt` / `Movie.chs.ass`，
语言从后缀推断（简体/繁体/中英/English/日本語…）。播放时：

- SRT / VTT / ASS / SSA → 按需转成 WebVTT 给 `<track>`，**不改原文件**；
- ASS 只保留对白文本，样式和特效会丢；要完整字幕用系统播放器（见下）。

完整的字幕下载/匹配（OpenSubtitles 之类）还没做，表结构已经留好。

### 播放

| 场景 | 怎么走 |
|---|---|
| MP4 / WebM（浏览器能解） | 灯箱内直接播，支持拖动进度（Range 206） |
| MKV / AVI / TS | 浏览器基本放不了 → 播放器右上角「系统播放器」：宿主模式下后端直接 `ShellExecute`，容器模式走 `photo-vault bridge` 的 `/open` |
| 字幕 | 网页播放用 `<track>`；系统播放器会自动加载同目录字幕 |

影视条目的片名 / 年份 / 简介可以在网页里改；「删除」只删登记记录，**不删文件**。

### 边界

- 不做文件重命名/移动/删除，删条目后文件原样还在；
- 没有 TMDB 刮削：片名和年份来自文件名，海报是抽帧（15% 处 + 模糊填充成 2:3）；
- 同一"季集槽位"有两个版本（1080p + 4K）时后扫到的覆盖先扫到的；
- 不查重、不算哈希 —— 这是为了几十 GB 的片子不读一遍；
- 字幕识别只看同目录 / `Subs/`，按"去掉发布标签后的片名骨架"匹配（`Movie.zh-CN.srt` 能配
  `Movie.1080p.BluRay.mkv`），不猜别的目录；

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
| 素材库 | ✅ | `assets.library`（gallery/material）：移进移出、AI 结果复用、收藏/标签/人物跟随当前库 |
| 素材库 AI 入队 | ✅ | 选中 →「AI 识别」：按需排 RAM++ / 视频关键帧，已排过的跳过；**素材库不排人脸**（移入时清掉已有的脸） |
| 网页导入 | ✅ | 拖拽 / 选文件直传：边收边算 sha256、重复跳过、`os.replace` 入库（只写一次盘）；图库 / 素材库可选 |
| 拍摄设备 | ✅ | EXIF 机型 → `devices`；未识别的手工归类（列表批量 / 导入整批 / 灯箱单张）；可新建自定义设备、改名、合并；列表可按设备（含未识别）筛选 |
| RAW+JPEG | ✅ | 同目录同名的 RAW+JPEG 合成一条资产（图表标 RAW+JPEG、灯箱只显示一张），出稿时两个文件一起给；`pair` 补历史数据 |
| 影视库 | ✅ | `cinema_titles` / `cinema_episodes` / `cinema_subtitles`：只登记路径，不搬文件、不跑 AI |
| 影视命名识别 | ✅ | SxxEyy / 1x02 / 第01集 / Season 目录 / 番剧式；电影名去发布组噪声 |
| 影视字幕 | ✅ | 同目录 sidecar 自动识别（srt/ass/ssa/vtt），播放时转 WebVTT |
| 影视播放 | ✅ | MP4/WebM 直接播（Range）；MKV/AVI 交给系统播放器（宿主模式或桌面桥） |
| Web 前端 | ✅ | Vue 3 + Vite + UnoCSS + vue-router：URL 即状态，刷新保持视图与滚动位置 |
| 外部客户端 / iOS | ✅ | CORS（`CORS_ORIGINS` 可收紧）；`/api/upload` 流式收文件，同批 HEIC+MOV 自动配成 Live Photo；`/file/{id}?role=live_motion` 支持 Range |
| 剪辑搜索轴 | ✅ | 类型／时长／最小短边；`q` 同时搜标签、视频描述、OCR、人物名 |
| 剪辑箱 | ✅ | `bins`/`bin_items`，跨多次搜索累积、可排序，顺序即出稿编号 |
| 出稿到剪辑软件 | ✅ | 硬链接物化成 `derived/cuts/<箱名>/` + `cutlist.csv`，命名带拍摄时间 |
| 宿主机桥 | ✅ | `photo-vault bridge`：资源管理器定位 + 状态灯 + `--install` 开机自启 |
| 反向对账 | ✅ | `reconcile`：磁盘有、库里没有的文件重新登记（顺带校验哈希） |
| 环境体检 | ✅ | `doctor`：磁盘、原子性、DB、ffmpeg、未登记文件 |
| Live Photo 配对 | ⚠️ | 同目录同文件名、或同一次上传的 batch 内配对，未用 ContentIdentifier |
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
├── docker-compose.yml        # db(pgvector/pg16) + app：/host（相册）+ /media（影视）
├── Dockerfile                # 基于 uv sync --frozen 安装依赖
├── db/
│   ├── 001_init.sql
│   ├── 002_provenance.sql  # storage_mode / origin_path / origin_rel
│   ├── 003_engagement.sql  # favorite / trash / tags
│   ├── 004_semantic.sql
│   ├── 005_ram_tags.sql    # RAM++ 单轨 + ml_meta
│   ├── 006_faces.sql       # persons / faces / 512 维 HNSW
│   ├── 007_video.sql       # video_frames / video_summaries
│   ├── 008_clip.sql        # bins / bin_items + trigram 搜索索引
│   ├── 009_library.sql     # assets.library：相册 gallery / 素材库 material
│   ├── 010_cinema.sql      # 影视条目 / 剧集 / 字幕（只存路径，不搬文件）
│   └── 011_devices.sql     # devices.label：用户起的设备名（"我的手机"）
├── vault/
│   ├── config.py           # 环境配置 + 宿主机/容器路径映射
│   ├── db.py
│   ├── hashutil.py         # 流式 sha256 / pHash / 内容寻址
│   ├── meta.py             # EXIF / ffprobe / 时区判定 / 缩略图
│   ├── audit.py            # 入库前体检
│   ├── ingest.py           # 入库状态机（copy / link）
│   ├── cinema.py           # 影视：命名解析 / ffprobe / 字幕识别 / 导入（不跑 AI）
│   ├── cli.py              # initdb | audit | ingest | stats | doctor | reconcile | serve | bridge | cinema-import | cinema-list
│   ├── bridge.py           # 宿主机桥：explorer 定位 / 剪贴板 / 系统播放器（Windows）
│   └── web.py              # FastAPI API + 静态托管（SPA 兜底到 vault/static/index.html）
├── vault/static/           # 前端构建产物（cd frontend && pnpm build 生成，随仓库提交）
├── frontend/               # Vue 3 + Vite + UnoCSS 单页应用
│   ├── src/router/         # 路由表 + 旧深链重定向
│   ├── src/stores/         # Pinia：feed / selection / bin / upload / cinema / people / tags / toast
│   ├── src/composables/    # 滚动位置恢复、资产写操作、灯箱 query
│   ├── src/components/     # 顶栏、导航 chips、网格卡片、灯箱、标签面板、上传面板、播放器
│   └── src/views/          # FeedView / PeopleView / BinView / DevicesView / CinemaView / CinemaTitleView
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
├── scripts/
│   ├── make_fixtures.py
│   └── clip_probe.ps1      # 验硬链接 / 剪贴板 CF_HDROP 是否被剪辑软件接受
└── tests/
    ├── test_time.py        # 时区回归测试（10 个用例）
    ├── test_faces.py       # 命名 / 并查集 / 对齐 / NMS（无模型、无库）
    ├── test_video.py       # 抽帧 / 聚合 / 输出解析（无模型、无库）
    └── test_cinema.py      # 影视命名解析 / 字幕语言 / SRT→VTT / 海报（无库）
```

---

## 几个关键设计（踩过坑才写得出来）

**1. 原件不可变。** 进 vault 后永不移动、不改名、不写回。整理全是数据库视图。

**2. landing 必须与 vault 同文件系统。** 否则 `os.replace` 退化成跨盘拷贝，失去原子性，`doctor` 会检查。

**3. 时间是三件套，不是一个字段。** EXIF 只有墙上时间、没有时区，相机时钟还经常是错的。
排序与聚类只看 `taken_at_utc`，展示才用 local。每条记录都带 `tz_source`，让 UI 能标"这个时间不可信"。

**4. 幂等键是内容哈希，不是文件名。** `DJI_0001.MP4` 每张卡都会重名。

**5. 相册只有一个挂载点，影视库另算。** 相册的素材和 vault 必须同一个 bind mount（`/host`），
跨挂载点的硬链接必然失败（EXDEV）；影视库（`/media`）是独立挂载且不做硬链接，所以放在另一块盘
完全没问题——两条路径的边界不同，不要混着套。

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

