# 模型权重

这些文件随仓库发布，用 Git LFS 存储（`.gitattributes` 里的规则）。克隆后必须
`git lfs pull` 才有实际内容；否则只是几百字节的指针文件。

| 文件 | 大小 | sha256 | 用途 |
|---|---|---|---|
| `detector/rf-detr-nano.pth` | 366,287,238 B | `d8d6b9ee57d4d0ed2b1f305163624712a0532cb7bce0c747317984fc5457440d` | RF-DETR nano，COCO：person / sports ball / tennis racket |
| `ball/wasb_tennis_best.pth.tar` | 6,102,633 B | `9d391239ab10c733f8e5bfadf16ab72838e7a8ebc88e8ae2038501c03d42b4bb` | WASB-SBDT HRNet，专用网球检测（远景） |
| `pose/pose_landmarker_full.task` | 9,398,198 B | `5134a3aad27a58b93da0088d431f366da362b44e3ccfbe3462b3827a839011b1` | MediaPipe Pose Landmarker Full |

## 来源与许可

- `detector/rf-detr-nano.pth`：Roboflow RF-DETR nano COCO 权重。`rfdetr` 包为
  Apache License 2.0；权重再分发前请确认 Roboflow 的权重许可条款适用于你的发布方式。
- `ball/wasb_tennis_best.pth.tar`：WASB-SBDT（NTT Communications），模型代码以 MIT
  许可收录在 `local-backend/src/tennis_backend/vendor/wasb/`，附 `LICENSE.md`。
- `pose/pose_landmarker_full.task`：MediaPipe Pose Landmarker，Apache License 2.0。

## 校验

```sh
shasum -a 256 models/detector/rf-detr-nano.pth
```

后端从这些路径加载（`local-backend/src/tennis_backend/media/weights.py`），可用环境变量
`TENNIS_DETECTOR_WEIGHTS` / `TENNIS_BALL_WEIGHTS` 覆盖；缺文件时返回带错误码的
拒绝信息，不会联网自动下载。
