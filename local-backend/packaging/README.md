# 离线安装包

`build_bundle.py` 为一个平台组装"开箱即离线"的后端包：独立 Python 3.11.12、`uv.lock`
锁定的全部依赖、后端代码、`contracts/`、`fixtures/`、`models/` 三份权重、启动脚本。

```sh
cd local-backend
uv run --frozen --offline python packaging/build_bundle.py --target macos-arm64   --out /tmp/bundles
uv run --frozen --offline python packaging/build_bundle.py --target windows-x64   --out /tmp/bundles
```

组装需要联网（下载 Python 运行时和依赖的该平台版本）；产物运行不需要。

## 不在包里的东西

- **FFmpeg / ffprobe**：后端有 4 处用 `libx264`（GPL）。包不分发 FFmpeg；首次使用时
  征得用户同意后在用户机器上安装（`brew install ffmpeg` / `winget install Gyan.FFmpeg`，
  Windows 命令未验证）。后端按名字在 PATH 里找，缺失时健康检查的 `ffmpeg_available` 等 4 项失败。
- 调试工作台与 Node/Bridge：另行打包。

## 启动

```sh
TENNIS_DATA_ROOT=/path/to/data ./tennis-backend --stdio        # macOS
set TENNIS_DATA_ROOT=C:\path\to\data & tennis-backend.cmd --stdio   # Windows
```

`TENNIS_DATA_ROOT` 必须由调用方设置，否则后端拒绝启动（用户选定的数据根）。

## 验证状态（2026-11-16）

| 平台 | 组装 | 运行验证 |
|---|---|---|
| macOS arm64 | 已做，约 1.94 GB | 已做：复制到另一路径、空环境（`env -i`）、macOS 沙盒禁止全部网络下，`--version`、`runtime.status`、COCO 检测、WASB 追踪均通过 |
| Windows x64 | 已做，约 1.83 GB；静态检查：`python.exe` 为 PE32+ x86-64，352 个 `.pyd`、68 个 `.dll`，`torch/lib/*.dll` 在，启动脚本 CRLF | **未验证**：没有 Windows 机器，从未运行过 |

## 已知事项

- 有一个包没有预编译 wheel（`filterpy`，纯 Python），从源码构建；脚本会检查其中没有原生二进制，
  否则拒绝出包（避免把本机的二进制带进另一个平台）。
- `sounddevice` 的通用 wheel 同时带 macOS `.dylib` 与 Windows `.dll`，所以 Windows 包里有一个无用的 `.dylib`。
- `opencv-python` 与 `opencv-contrib-python` 同时被锁定（原有状态，未处理）；体积已计入。
- 包内路径布局必须与仓库一致（后端按自身文件位置找 `contracts/`、`fixtures/`、`models/`），不要重排。
