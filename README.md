# 网球视频分析（本地后端 + 调试工作台）

本地运行：视频上传 → 人体/球/球拍检测 → 击球候选 → 证据帧与元数据 → 专家复核。
不联网调用模型；所有推理在本机完成。

## 环境要求

| 需要 | 版本 | 说明 |
|---|---|---|
| Python | 3.11+ | 后端用 `uv` 管理依赖（`local-backend/uv.lock`） |
| Node.js | 20+ | 只跑调试工作台时需要 |
| FFmpeg / ffprobe | 任意近期版本 | 媒体探测与标准化 |
| **Git LFS** | 3.x | **必须**：模型权重用 Git LFS 存储，没装时克隆下来只是指针文件，后端会拒绝启动检测 |

一次装好：

```sh
brew install git-lfs      # macOS；其他系统见 https://git-lfs.com
git lfs install           # 每个用户装一次即可
git lfs pull              # 拉取实际权重文件；没拉取时大小只有几百字节
```

没装 LFS 时 `git clone` 仍然成功，但 `models/detector/rf-detr-nano.pth` 里是一段
指针文本而不是权重。后端检测到这种情况会返回 `BALL_WEIGHTS_MISSING` 并提示
`git lfs pull`，不会静默失败，也不会自己联网下载。

## 模型权重

全部随仓库发布（Git LFS），位于 `models/`：

| 文件 | 大小 | 用途 |
|---|---|---|
| `detector/rf-detr-nano.pth` | 366 MB | 人体检测；球与球拍的 COCO 检测（近景） |
| `ball/wasb_tennis_best.pth.tar` | 6 MB | 专用网球检测（远景/中景） |
| `pose/pose_landmarker_full.task` | 9 MB | 姿态关键点 |

校验值见 `models/README.md`；下载后可用 `shasum -a 256 <文件>` 自行核对。

## 启动

```sh
# 后端（NDJSON over stdio）
uv run --frozen --offline --project local-backend tennis-backend --stdio

# 调试工作台
cd workbuddy-plugin/bridge && npm run phase1-console
```

## 当前范围与限制

- 契约 2.0.0 已冻结，包含球轨迹、击球候选、取证与存储预估接口；后端完成 `cli.py` 接线前，这些新方法仍不可用于真实联调。
- 击球时点只输出"候选区间"，不输出精确到单帧的击球时点、球速或拍头速度。
- 球被拍面遮挡、或被身体挡住时为漏检；没有框不代表画面里没有球。
- 视频类型（近景/远景）由专家判断，后端兜底规则只在 4 个视频上测过，仅供参考。
