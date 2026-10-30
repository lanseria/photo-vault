# Photo Vault · P0

本地优先的个人照片/视频资产管理服务。目标不是"又一个相册 App"，而是**一个能活 10 年、原件永不损坏、换任何软件都读得出来**的媒体库。

当前实现的是整条链路上**最危险、也最不依赖算力**的一段：**入库**。
AI 识别、人脸、语义检索都还没接——但数据模型、队列、内容寻址都已经为它们留好位置。

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
| 按原目录浏览 | ✅ | `origin_rel` + `/api/folders`，Web 上直接筛"原来放在哪个文件夹" |
| 反向对账 | ✅ | `reconcile`：磁盘有、库里没有的文件重新登记（顺带校验哈希） |
| 环境体检 | ✅ | `doctor`：磁盘、原子性、DB、ffmpeg、未登记文件 |
| Live Photo 配对 | ⚠️ | 同目录同文件名配对，未用 ContentIdentifier |
| 近似去重 | ⚠️ | pHash 已入库，聚类未做 |
| RAW | ❌ | CR3/NEF/ARW/DNG 只能落盘，读不出元数据 |
| 人脸 / 向量 / VLM | ❌ | 数据模型与 `jobs` 表已预留 |

---

## 目录结构

```
photo-vault/
├── docker-compose.yml      # db(pgvector/pg16) + app，单挂载点
├── Dockerfile
├── db/
│   ├── 001_init.sql
│   └── 002_provenance.sql  # storage_mode / origin_path / origin_rel
├── vault/
│   ├── config.py
│   ├── db.py
│   ├── hashutil.py         # 流式 sha256 / pHash / 内容寻址
│   ├── meta.py             # EXIF / ffprobe / 时区判定 / 缩略图
│   ├── audit.py            # 入库前体检
│   ├── ingest.py           # 入库状态机（copy / link）
│   ├── cli.py              # initdb | audit | ingest | stats | doctor | reconcile | serve
│   └── web.py              # FastAPI + 单页 UI
├── scripts/make_fixtures.py
└── tests/test_time.py      # 时区回归测试（10 个用例）
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
| 图像向量（SigLIP / Chinese-CLIP） | ✅ 快 | 每秒几十张 |
| OCR（PaddleOCR） | ✅ 可用 | |
| 语音转写（Whisper） | ✅ 可用 | |
| **VLM 图文描述** | ⚠️ **只能跑 3B 级** | 8G 显存跑 7B 很勉强 |

1070 Ti 是 Pascal 架构：没有 Tensor Core，FP16 只有 1/64 速率，**必须走 INT4/INT8 量化**。
现实选择是 `Qwen2.5-VL-3B` 或 `MiniCPM-V` 这一档，约 **30–60 秒/张**。

按 5 万张照片算 ≈ **1–3 周连续跑**（夜间批处理完全可以接受）。7B 会慢 3–4 倍，不建议。

**架构上的应对**：ML worker 必须能**跨机部署**——只从 `jobs` 表拉活、回写结果。
今天跑在家庭机 1070 Ti 上，将来换卡或换机器，库和 API 一行不改。

---

## 已知边界

- **RAW 未支持**：需要 exiftool 或 libraw。你现在没相机，优先级低。
- **Live Photo 配对**靠文件名，改名会失效。
- **连拍归栈**、**近似去重聚类**、**时空事件聚类**都还没做。
- **多用户**：表里没有 user 轴。
- **备份**：本服务不做备份。vault 必须挂到有快照 + 异地副本的位置。