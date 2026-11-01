# photo-vault iOS 同步 App · QA 验证报告

> 验证对象：`ios/`（SwiftUI + XcodeGen，bundle `com.photovault.ios`）
> 真实服务器：`http://bmc_desktop:7878`（生产库，19919 张用户真实资产 / 2.0 TB）
> 验证人：qa-verifier · 时间：2026-11-01 19:40 起（本机时区 Asia/Shanghai）
> 状态：**阶段 A 完成（服务端契约 + 素材播种 + 清理）；阶段 B 进行中**

---

## 0. 结论摘要

### 阶段 A：服务端契约（不依赖 App，全部 PASS）

| # | 验收项 | 结果 | 证据 |
|---|---|---|---|
| A1 | `POST /api/upload` 首次上传 → `status=imported` | **PASS** | `/api/stats` count +1；`asset.origin` 保留传入的 name |
| A2 | 同内容重传 → `status=duplicate`，且 `asset.id` 不变 | **PASS** | 两次响应 id 相同 |
| A3 | 同内容换文件名重传 → 仍 `duplicate`（内容寻址去重） | **PASS** | `iostest-curl-renamed.jpg` → 同 id |
| A4 | Live Photo：同一 batch，先静帧 `iostest-live.JPG` 后动态 `iostest-live.MOV` → 第二条 `asset.kind=live_photo` | **PASS** | 第二条响应 `kind=live_photo`，与静帧同一 id |
| A5 | 顺序反了（先 MOV 后 JPG）→ 变成独立 video + 独立 photo（与 README 一致） | **PASS** | 两条不同 id：`kind=video` / `kind=photo` |
| A6 | HEIC 上传 + 缩略图/预览 | **PASS** | 上传 imported；`/thumbs/*.webp` 200；`/preview/{id}` 返回 **JPEG**（服务端已转码） |
| A7 | PNG 上传 | **PASS** | imported，`kind=photo` |
| A8 | 缩略图是 WebP | **PASS** | `Content-Type: image/webp`，magic `RIFF....WEBP` |
| A9 | `/file/{id}` 原件字节与本地 sha256 一致 | **PASS** | 两侧 sha256 完全相同 |
| A10 | Live Photo 的实况视频 `/file/{id}?role=live_motion` 可取 | **PASS** | HTTP 200，字节数 = 上传的 MOV |
| A11 | 查询 `GET /api/assets?q=iostest` 能按文件名命中 | **PASS** | 返回 6 条（当轮上传的全部） |

### 阶段 A：素材播种（模拟器 PhotoKit）

| # | 验收项 | 结果 | 证据 |
|---|---|---|---|
| A12 | `simctl addmedia` 能把 JPEG/HEIC/PNG/MOV 灌进相册 | **PASS** | Photos.sqlite 只读副本核验到 9 条 `iostest-*` 资产 |
| A13 | 追加导入不覆盖原文件名（App 上传的 name 来源） | **PASS（关键发现）** | 展示名被改成 `IMG_0008…`，但 `PHAssetResource.originalFilename` / `ZORIGINALFILENAME` **保留** `iostest-a.jpg` |
| A14 | 模拟器里能播种**真 Live Photo** | **PASS（关键发现）** | `iostest-livep.HEIC` → `ZKINDSUBTYPE=2`、`playbackStyle=3`、带 `pairedVideo` 资源 |
| A15 | 仅"同 stem 的 JPG+MOV 一起 addmedia"能否配对 | **FAIL（预期）** | 被拆成两张独立资产 → 见 §2.2 三组对照实验 |
| A16 | WebP 在 iOS 26 能否原生解码 | **PASS** | 图库网格截图缩略图区域 `stddev 44~70`（非纯色块），见 §2.5 |

### 清理

| # | 验收项 | 结果 |
|---|---|---|
| A17 | 清理脚本能定位所有 `iostest-*` 资产并 trash→delete | **PASS**（12 条测试资产分两轮全部删除） |
| A18 | 清理后 `/api/stats` 回到基线 | **PASS**：`count 19925→19919`、`gallery 15961→15955`、`bytes 2159267166010→2159266870148` |
| A19 | 服务器无残留 `iostest-*` 资产 | **PASS**（`q=iostest` 返回 0） |
| A20 | 清理后无**其它**副作用 | **注意**：`device_count 34→35` —— 测试素材带 `EXIF Model=iPhone 17 Pro`，入库时建了一行设备字典，服务端**没有删除设备的接口**。详见 §5.3 |

### 阶段 B：端到端同步
（待填：构建/安装、单元测试、首轮同步、幂等、暂停/继续、失败重试、错误路径、图库页）

---

## 1. 环境

| 项 | 值 |
|---|---|
| 模拟器 | iPhone 17 Pro，`E96F2777-7A6B-4018-B7E5-BD3937CD17A8`，iOS 26.2（Booted） |
| App | `ios/`，XcodeGen；本次 QA 独立 derivedDataPath `ios/.build/qa` |
| 服务器 | `http://bmc_desktop:7878`（局域网另一台机器，Docker）；`.local` 域名 `bmc_desktop.local:7878` 也可用 |
| 工具 | `xcodebuild`、`xcrun simctl`、`ffmpeg 9.0.1`、`python3 3.14 + Pillow 12.1`、`sqlite3`、`curl`、`swiftc 6.4.2` |
| 服务器基线 | `count=19919 / gallery=15955 / material=3964 / bytes=2159266870148 / trash=0` |

### 1.1 环境坑（后来者必读）

1. **`simctl privacy grant photos` 对「完全访问」无效**
   `xcrun simctl privacy <udid> grant photos com.photovault.ios` 返回 0，但 App 里仍显示
   「需要访问相册权限」，`PHPhotoLibrary.authorizationStatus` 依旧是 `.notDetermined`。
   必须点 App 内的「允许访问相册」→ 系统弹窗「**允许完全访问**」。
   ```bash
   xcrun simctl terminate <udid> com.photovault.ios
   xcrun simctl launch <udid> com.photovault.ios
   # 然后 UI 点：允许访问相册 → 允许完全访问
   ```
   弹窗里会显示库里资产数（本次：「14 Photos, 2 Videos」），可以顺手当一次 PhotoKit 自检。

2. **`simctl addmedia` 会改「展示名」，但不改原始名**
   照片进相册后 `ZFILENAME` 变成 `IMG_0008.JPG` 之类；原始文件名在
   `ZADDITIONALASSETATTRIBUTES.ZORIGINALFILENAME`（对应 `PHAssetResource.originalFilename`）。
   ⇒ 只用 `ZFILENAME` 找 `iostest-*` 会一条都找不到；App 上传 name 也应取 `originalFilename`。

3. **Python `urllib` 会走 macOS 系统代理**
   这台机器系统代理是 `127.0.0.1:7897`，`urllib` 读系统配置后把 `http://bmc_desktop:7878`
   也发给代理，结果稳定 **502**；`curl` 不走系统代理所以正常。
   脚本里用 `urllib.request.build_opener(ProxyHandler({}))` 绕开（见 `cleanup_test_assets.py`）。
   症状极具误导性：curl 200 / python 502。

4. **模拟器 Photos 数据库可以只读核验**
   `~/Library/Developer/CoreSimulator/Devices/<udid>/data/Media/PhotoData/Photos.sqlite`
   连同 `-wal`/`-shm` 复制到临时目录再 `mode=ro` 打开，可以独立验证 PhotoKit 里到底有什么，
   不必依赖 App 的自述。关键字段：
   - `ZASSET.ZKIND`：0=照片 1=视频
   - `ZASSET.ZKINDSUBTYPE`：Live Photo 为 2
   - `ZASSET.ZPLAYBACKSTYLE`：1=静图 3=Live Photo 4=视频
   - `ZASSET.ZMEDIAGROUPUUID`：Live Photo 的 media group（= content identifier）
   - `ZINTERNALRESOURCE.ZRESOURCETYPE`：**0=静帧 1=视频本体 3=pairedVideo**（≠ `PHAssetResourceType` 枚举，别混用）

5. **Xcode 首次 `swiftc -O` 编译要几十秒**，别用 `| head` 截管道（SIGPIPE 会让编译失败但看起来像成功）。

---

## 2. 阶段 A：素材播种与服务端契约

### 2.1 播种脚本

`ios/tools/seed_simulator_media.py` —— 生成素材 → `simctl addmedia` → 用 Photos.sqlite 只读副本核验。

```bash
python3 ios/tools/seed_simulator_media.py                 # 生成 + 导入 + 核验
python3 ios/tools/seed_simulator_media.py --verify-only    # 只核验相册
python3 ios/tools/seed_simulator_media.py --manifest-only  # 不重新生成，按现有文件刷 manifest
```

产物（`ios/qa/seed/`，全部 `iostest-` 前缀，合计 ~150 KB）：

| 文件 | 字节 | 说明 |
|---|---|---|
| `iostest-a.jpg` / `-b.jpg` / `-c.jpg` | 35986 / 25749 / 18924 | 1024×768 / 800×600 / 640×480，带 `EXIF Make=Apple Model=iPhone 17 Pro` |
| `iostest-d.heic` | 10845 | 由 `-c.jpg` 经 `sips` 转出（iOS 原生格式） |
| `iostest-e.png` | 7166 | 512×512，无 EXIF（应识别为「未识别设备」） |
| `iostest-v.mov` | 9156 | 2s 320×240 H.264 + AAC |
| `iostest-live.JPG` + `iostest-live.MOV` | 34238 + 6686 | **仅同名**的伪配对（对照实验用） |
| `iostest-livep.HEIC` + `iostest-livep.MOV` | 19427 + 7845 | **真 Live Photo**（带 content id + still-image-time 轨） |

导入后 PhotoKit 实际状态（`--verify-only` 输出，节选）：

```
Z_PK=8   iostest-c.jpg      展示名=IMG_0008.JPG  kind=0 subtype=0 playback=1/0 640x480
Z_PK=10  iostest-a.jpg      展示名=IMG_0010.JPG  kind=0 subtype=0 playback=1/0 1024x768
Z_PK=13  iostest-e.png      展示名=IMG_0013.PNG  kind=0 subtype=0 playback=1/0 512x512
Z_PK=14  iostest-v.mov      展示名=IMG_0014.MOV  kind=1 subtype=0 playback=4/0 320x240  VIDEO
Z_PK=16  iostest-livep.HEIC 展示名=IMG_0016.HEIC kind=0 subtype=2 playback=3/0 1024x768  LIVE pairedVideo
        resources: photo(19427B) + pairedVideo(7845B, avc1)
→ 共 9 条；照片 7，视频 2；Live Photo 1 条
```

### 2.2 Live Photo 播种：三组对照实验（关键）

| 实验 | 静帧 | 动态 | 结果 |
|---|---|---|---|
| ① 仅同名 + JPEG 里写 XMP `apple:ContentIdentifier` + MOV 里写 mdta 文件级 tag | `iostest-live.JPG` | `iostest-live.MOV` | **拆成两张独立资产**（Z_PK 12 照片 / 15 视频） |
| ② 只同名（不做任何 metadata） | 同上 | 同上 | 同上，拆开 |
| ③ **MakerApple `17` + QuickTime content identifier + `still-image-time` timed metadata 轨** | `iostest-livep.HEIC` | `iostest-livep.MOV` | **成功合成一条 Live Photo**：`ZKINDSUBTYPE=2`、`playback=3`、`mediaGroupUUID = content id`、资源含 `pairedVideo` |

**结论**：模拟器**可以**播种真 Live Photo，但「同名 JPG+MOV」不够，三样缺一不可：

1. 静帧写 `kCGImagePropertyMakerAppleDictionary["17"] = content-id`（**必须是 MakerApple**；只写 XMP 不行，实验 ① 已证伪）；
2. 动态写 QuickTime `com.apple.quicktime.content.identifier` = **同一个** id；
3. 动态里还要有一条 `com.apple.quicktime.still-image-time` 的 **timed metadata 轨**（ffmpeg 写不了，必须 AVFoundation 的 `AVAssetWriterInputMetadataAdaptor`）。

生成器：`ios/qa/tools/make_live_photo.swift`（编译产物 `ios/qa/.build/make_live_photo`）

```bash
swiftc -O -o ios/qa/.build/make_live_photo ios/qa/tools/make_live_photo.swift
cd ios/qa/seed && ../.build/make_live_photo iostest-a.jpg iostest-v.mov . iostest-livep
xcrun simctl addmedia <udid> iostest-livep.HEIC iostest-livep.MOV   # 两个文件必须一起传
```

⇒ 这条对 sync-core 很重要：**模拟器端可以端到端验证 Live Photo 同步**（`.photo` + `.pairedVideo` 两个资源）。

### 2.3 服务端契约实测（curl 独立于 App）

脚本：`ios/qa/contract_check.sh`（可重跑；每轮用不同内容的随机素材，避免和上一轮撞哈希）
证据：`ios/qa/evidence/phase-a-contract-20261101-195542.log`

```
== 1. 上传 iostest-curl.jpg ==
→ status=imported kind=photo origin=iostest-curl.jpg id=3f7ba2b8-…
== 2. 同内容再传 ==
→ status=duplicate id=3f7ba2b8-…（与首次相同）
== 2b. 同内容换名 iostest-curl-renamed.jpg ==
→ status=duplicate（内容寻址去重，跨文件名）
== 2c. HEIC / PNG ==
→ HEIC: status=imported kind=photo id=bc8bd8ab-…
   thumbs HTTP:200 content-type: image/webp
   preview HTTP:200 content-type: image/jpeg  magic=b'\xff\xd8\xff\xe0' → JPEG ✔（HEIC 已转码）
→ PNG: status=imported kind=photo id=8c234c9e-…
== 3. Live Photo（先静帧后动态，同 batch）==
→ 静帧: status=imported kind=photo id=40239f03-…
→ 动态: status=imported kind=live_photo origin=iostest-live.JPG id=40239f03-…   ← 同一 id，kind 变成 live_photo
== 4. 顺序反了（先 MOV 后 JPG）==
→ 先传 MOV: kind=video id=36fa5edb-…；后传 JPG: kind=photo id=223ce81e-…      ← 两条独立资产
== 5. 缩略图 / 预览 / 原件 ==
-- 5a. thumbs → HTTP:200 bytes:12534 content-type: image/webp magic=RIFF/WEBP ✔
-- 5b. preview → HTTP:200 content-type: image/jpeg
-- 5c. /file 原件 → server sha == 本地 sha ✔
-- 5d. /file?role=live_motion → HTTP:200 bytes:17873 ✔（= 上传的 MOV 大小）
== 7. 统计增量 ==
字段          前              后            差
count       19919          19925           +6
gallery     15955          15961           +6
material     3964           3964           +0
bytes   2159266870148  2159267166010    +295862
trash           0              0           +0
```

两条值得记的服务端行为：

- **`bytes` 增量 = 静帧大小之和，不含 Live Photo 的动态视频**（`live_motion` 不计入 `/api/stats.bytes`）。
  App 的「已上传字节」是实际传输字节（含 MOV），两者会有偏差 —— 属**已知偏差（统计口径）**，非 bug。
- **`GET /api/assets?q=` 匹配的是文件路径/扩展名**（`files.src_path`、`origin_rel`），所以按文件名搜是可靠的；
  但没有按 sha256 搜索的能力，清理脚本因此需要全量翻页兜底。

### 2.4 视频 `/preview` 返回 500（服务端缺陷，独立复现）

用服务器上真实视频资产（`origin=DJI_0070.MP4`）只读复现：

```
GET /preview/5ecaf7e8-…            → HTTP 500  body: "Internal Server Error"
GET /thumbs/a13711383813….webp     → HTTP 200  12286B  content-type: image/webp  magic RIFF/WEBP（VP8）
```

⇒ 视频的**缩略图正常、预览接口 500**。UI 必须对视频回退缩略图（`RemoteImageStore` 已实现回退），
否则列表/详情会出现空白。服务端侧建议：`/preview` 对视频返回首帧 JPEG 或干净的 404。

### 2.5 WebP 缩略图结论（问题：iOS 原生能不能解）

**结论：iOS 26.2 原生解码 WebP 正常，不需要退回 `/preview`。**

- 服务端 `/thumbs/{sha}.webp`：`Content-Type: image/webp`，magic `RIFF....WEBP`，VP8 编码，512px 级别、约 12 KB；
- 模拟器里的客观核验（`ios/qa/screenshots/01-library-webp-grid.png`，对 4 个缩略图单元做像素统计）：

| 单元 | mean(RGB) | stddev(RGB) | 判定 |
|---|---|---|---|
| row1col1 | (109, 98, 87) | (70, 70, 66) | 有图像内容 ✔ |
| row1col2 | (113, 101, 88) | (68, 66, 62) | 有图像内容 ✔ |
| row2col1 | (115, 105, 93) | (57, 56, 54) | 有图像内容 ✔ |
| row3col1 | (95, 71, 54) | (67, 54, 44) | 有图像内容 ✔ |

（解码失败/黑块的话 stddev 会接近 0、均值接近 (0,0,0) 或纯灰。）

- 落地建议：保留 App 现有的「WebP 优先、失败自动回退 `/preview` 并记日志」策略即可，
  这样在更老系统或未来服务端换编码时不会白屏。

### 2.6 清理脚本与阶段 A 清理

`ios/tools/cleanup_test_assets.py` —— 三重定位 + 默认 dry-run + 逐条打印：

```bash
python3 ios/tools/cleanup_test_assets.py                 # dry-run，先看会删什么
python3 ios/tools/cleanup_test_assets.py --yes --full-scan --out json
python3 ios/tools/cleanup_test_assets.py --check-baseline 19919,2159266870148
```

定位优先级（任一命中即视为测试资产，否则**绝不动**）：

1. App 账本 `serverAssetID`（`<data container>/Library/Application Support/SyncLedger.json`，字段 `serverAssetID/name/state/bytes/updatedAt/modificationDate`）；
2. `origin` 以 `iostest-` 开头；
3. **sha256 命中**已知测试素材（seed manifest + 模拟器相册实物文件 + 账本）——兜住「App 用 IMG_0012.JPG 这种被改过的名字上传」的情况；
   `--full-scan` 会全量翻页（15,955 条 ≈ 45 s）逐条比对 sha。

**sha256 作身份是可靠的**（实测）：10 个种子文件经 `simctl addmedia` 落进 DCIM 后，
内容**逐字节未变**，manifest 里的 sha256 与模拟器相册实物文件完全一致：

| seed 文件 | 相册里的文件名 | sha 一致 |
|---|---|---|
| iostest-a.jpg | IMG_0010.JPG | ✔ |
| iostest-b.jpg | IMG_0009.JPG | ✔ |
| iostest-c.jpg | IMG_0008.jpg | ✔ |
| iostest-d.heic | IMG_0011.HEIC | ✔ |
| iostest-e.png | IMG_0013.png | ✔ |
| iostest-live.JPG / .MOV | IMG_0012.JPG / IMG_0015.MOV | ✔ |
| iostest-livep.HEIC / .MOV | IMG_0016.HEIC / IMG_0016.MOV | ✔ |
| iostest-v.mov | IMG_0014.mov | ✔ |

⇒ 即使 App 上传时用的是被改过的名字，服务器上的 `sha` 也必然等于这些值，
清理脚本的 sha256 兜底路径因此是确定可用的（阶段 A 已用它交叉命中过一条资产）。

阶段 A 共产生 12 条测试资产，分两轮全部删除（`trash` 200 + `delete` 200）：

```
轮次 1（cleanup-20261101-195454.json）：count 19925→19919
  bf050c7c-… iostest-rev.MOV      trash=200 delete=200
  de429cd0-… iostest-rev.JPG      trash=200 delete=200
  044b9a5e-… iostest-live.JPG     trash=200 delete=200
  273a9aca-… iostest-curl.jpg     trash=200 delete=200
  a3a99959-… iostest-curl.jpg     trash=200 delete=200
  b008b6e3-… iostest-live.JPG     trash=200 delete=200
轮次 2（cleanup-phaseA-final.json）：count 19925→19919
  36fa5edb-… iostest-rev.MOV      trash=200 delete=200
  223ce81e-… iostest-rev.JPG      trash=200 delete=200
  40239f03-… iostest-live.JPG     trash=200 delete=200
  8c234c9e-… iostest-e.png        trash=200 delete=200
  3f7ba2b8-… iostest-curl.jpg     trash=200 delete=200
  bc8bd8ab-… iostest-d.heic       trash=200 delete=200
```

清理后：`count=19919, gallery=15955, material=3964, bytes=2159266870148, trash=0` —— **与基线完全一致**，`q=iostest` 0 条。

---

## 3. 阶段 B：端到端同步

（待填）

### 3.1 构建与安装
### 3.2 单元测试
### 3.3 首轮同步（含起始日期过滤）
### 3.4 幂等验证（第二次同步）
### 3.5 暂停 / 继续 / 失败重试
### 3.6 图库页 / 详情页
### 3.7 错误路径（错误地址）
### 3.8 清理与基线复核

---

## 4. 阶段 B 检查清单（执行顺序）

1. `cd ios && ./tools/regen.sh && xcodebuild … -derivedDataPath .build/qa build`，`simctl install/launch`；
2. 权限：点「允许访问相册 → 允许完全访问」（见 §1.1 坑 1）；
3. **同步前先设起始日期 = 2026-11-01 00:00**（设置 → 同步选项 → 只同步指定日期之后）。
   ⚠️ 该开关打开时默认值是 `Date() - 1 年`（2025-11-01），会把系统样例照 `IMG_0007`（2026-10-27）也传上去，**必须手动改成 2026-11-01**；
4. `python3 ios/tools/e2e_check.py snapshot --out ios/qa/evidence/e2e-before.json`（同步前快照）；
5. 开始同步 → 等完成 → 截图；
6. `python3 ios/tools/e2e_check.py verify --before ios/qa/evidence/e2e-before.json`
   —— 逐条核对 9 条期望资产 + **安全断言：新增资产必须 100% 命中测试信号**；
7. 再跑一次同步 → `--expect-no-new` 断言幂等；
8. 暂停/继续、失败重试（`http://127.0.0.1:1` → 改回 → 重新「测试」保存 effectiveBase → 重试）；
9. 清理：账本 id + origin 前缀 + `--full-scan` sha 三重兜底 → `assert-clean --baseline-count 19919`。

---

## 5. 发现问题 / 风险

### 5.1 服务端：视频 `/preview/{id}` 返回 500（P1，服务端）
- 复现：任意 `kind=video` 资产 `GET /preview/{id}` → HTTP 500 `Internal Server Error`。
- 影响：UI 若直接依赖 `/preview` 显示视频会空白；当前 App 已回退缩略图（需在阶段 B 目测确认）。
- 建议：视频返回首帧 JPEG（服务端已有 thumb WebP，可复用），或返回 404 让客户端走回退。
- 责任：服务端（非 App）。**不建议**在本轮修，因服务器是用户生产库。

### 5.2 服务端：Live Photo 的 `bytes` 不含动态视频（P3，口径问题）
- `/api/stats.bytes` 与 `asset.bytes` 只统计 `files.role in ('original','raw')`，`live_motion` 不计入。
- App 的「已上传字节」是实际传输量，两者会有差值（本次实测单条差 6686~18732 B）。
- 建议：报告层面的已知偏差，不算 bug。

### 5.3 环境副作用：测试素材的 EXIF 建了一行设备字典（已发生，需知会）
- 阶段 A 的 curl 素材带 `EXIF Make=Apple / Model=iPhone 17 Pro`，入库时建了设备行，
  删除资产后该设备行**保留**（服务端**没有删除设备的接口**，只有 merge/rename/assign）。
- 现状：`device_count 34 → 35`（该设备 0 张资产，不在 `/api/stats.devices` 列表里，UI 上基本不可见）。
- 建议：后续 QA 素材**不要伪造机型 EXIF**（用无 EXIF 的 PNG/JPEG）；如要彻底清掉，只能由服务器维护者用 SQL 处理，本轮不动生产库。

### 5.4 模拟器：`addmedia` 改名（P2，已规避）
- 展示名 `IMG_00xx`、原始名保留；App 必须用 `PHAssetResource.originalFilename`。
  `SyncPlanner/PhotoLibrary` 已按此实现（代码注释也写明了这个坑），阶段 B 会用服务器侧 `origin` 实证。

### 5.5 权限：`simctl privacy grant photos` 不生效（P3，环境坑）
- 见 §1.1 坑 1；建议把「点弹窗授权」写进团队环境搭建说明。

---

## 6. 复现命令速查

```bash
cd ios

# 素材：生成 + 导入 + 核验
python3 tools/seed_simulator_media.py
python3 tools/seed_simulator_media.py --verify-only

# Live Photo 生成器
swiftc -O -o qa/.build/make_live_photo qa/tools/make_live_photo.swift

# 服务端契约（不碰 App）
bash qa/contract_check.sh

# 清理（默认 dry-run；--yes 才删）
python3 tools/cleanup_test_assets.py
python3 tools/cleanup_test_assets.py --yes --full-scan
python3 tools/cleanup_test_assets.py --check-baseline 19919,2159266870148

# 端到端核对
python3 tools/e2e_check.py snapshot --out qa/evidence/e2e-before.json
python3 tools/e2e_check.py verify --before qa/evidence/e2e-before.json
python3 tools/e2e_check.py verify --before qa/evidence/e2e-after1.json --expect-no-new
python3 tools/e2e_check.py assert-clean --baseline-count 19919 --baseline-bytes 2159266870148
```

## 7. 产物清单

| 路径 | 说明 |
|---|---|
| `ios/qa/REPORT.md` | 本报告 |
| `ios/qa/contract_check.sh` | 服务端契约检查脚本（阶段 A） |
| `ios/qa/tools/make_live_photo.swift` | 真 Live Photo 生成器（ImageIO + AVFoundation） |
| `ios/qa/seed/` | 素材 + `manifest.json`（sha256 清单） |
| `ios/qa/evidence/` | 契约日志、清理 JSON、端到端快照与核对结果 |
| `ios/qa/screenshots/` | 截图（含 WebP 缩略图区域裁剪） |
| `ios/tools/seed_simulator_media.py` | 播种脚本 |
| `ios/tools/cleanup_test_assets.py` | 清理脚本（三重定位 + dry-run） |
| `ios/tools/e2e_check.py` | 端到端核对（前后快照差集 + 安全断言） |
