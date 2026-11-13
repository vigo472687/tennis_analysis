"""One local, cancellable real-video stage per process invocation."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from tennis_backend.experience.measure import (
    analyze_pose,
    evidence_frames,
    file_hash,
    inspect_image_quality,
)
from tennis_backend.media.probe import probe_video

MODEL_HASH = "5134a3aad27a58b93da0088d431f366da362b44e3ccfbe3462b3827a839011b1"


def write_json(location: Path, value: Any) -> None:
    temporary = location.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    os.replace(temporary, location)


def read_json(location: Path) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(location.read_text(encoding="utf-8"))
    return value


def run_stage(root: Path, stage: str, model: Path) -> None:
    metadata = read_json(root / "job.json")
    result_path = root / "result.json"
    result: dict[str, Any] = (
        read_json(result_path) if result_path.exists() else {"simulation": False}
    )

    def progress(percent: int, message: str = "") -> None:
        write_json(
            root / "progress.json",
            {"status": "running", "stage": stage, "progress": percent, "message": message},
        )

    progress(1)
    if stage == "inspect":
        source = root / "source"
        probe = probe_video(source)
        result["media"] = probe.summary
        write_json(result_path, result)
        if probe.is_blocked:
            code = "DURATION_EXCEEDED" if probe.media.duration_s > 180 else "MEDIA_UNSUPPORTED"
            raise ValueError(code)
        if probe.media.width * probe.media.height > 3840 * 2160 or probe.media.fps > 240:
            raise ValueError("VIDEO_RESOURCE_LIMIT")
        progress(15, "正在生成本地播放副本")
        temporary = root / "preview.tmp.mp4"
        completed = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-y",
                "-v",
                "error",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-vf",
                "scale=w='min(1280,iw)':h='min(1280,ih)':"
                "force_original_aspect_ratio=decrease:force_divisible_by=2,fps=24",
                "-t",
                "180",
                "-an",
                "-c:v",
                "libx264",
                "-threads",
                "2",
                "-preset",
                "fast",
                "-crf",
                "22",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(temporary),
            ],
            capture_output=True,
            timeout=240,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError("VIDEO_CONVERSION_FAILED")
        os.replace(temporary, root / "preview.mp4")
        progress(80, "正在检查画面")
        result["quality"] = inspect_image_quality(root / "preview.mp4")
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-y",
                "-v",
                "error",
                "-ss",
                "0",
                "-i",
                str(root / "preview.mp4"),
                "-frames:v",
                "1",
                str(root / "poster.jpg"),
            ],
            capture_output=True,
            timeout=20,
            check=True,
        )
        result["source_sha256"] = file_hash(source)
    elif stage == "pose":
        if not (root / "preview.mp4").is_file():
            raise ValueError("INSPECTION_REQUIRED")
        if not model.is_file() or file_hash(model) != MODEL_HASH:
            raise ValueError("POSE_MODEL_UNAVAILABLE")
        pose = analyze_pose(
            root / "preview.mp4",
            model,
            metadata["handedness"],
            lambda percent: progress(percent, "逐帧检测人体关键点"),
        )
        write_json(root / "pose.json", pose)
        result["pose"] = {key: value for key, value in pose.items() if key != "frames"}
        result["model"] = {
            "name": "MediaPipe Pose Landmarker Full",
            "version": "float16/1",
            "runtime": "mediapipe 1.2.2",
            "sha256": MODEL_HASH,
        }
    elif stage == "evidence":
        if not (root / "pose.json").is_file():
            raise ValueError("POSE_REQUIRED")
        result["evidence"] = evidence_frames(
            root / "preview.mp4", read_json(root / "pose.json"), root
        )
    else:
        raise ValueError("STAGE_UNKNOWN")
    write_json(result_path, result)
    write_json(
        root / "progress.json",
        {"status": "completed", "stage": stage, "progress": 100, "message": "处理完成"},
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-root", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--stage", choices=("inspect", "pose", "evidence"), required=True)
    args = parser.parse_args()
    try:
        run_stage(args.job_root, args.stage, args.model)
    except Exception as error:
        code = str(error)
        if not code.isupper() or len(code) > 80:
            code = "VIDEO_PROCESSING_FAILED"
        write_json(
            args.job_root / "progress.json",
            {"status": "failed", "stage": args.stage, "progress": 0, "error": code},
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
