"""Local pose measurements. Image-plane proxies are never tennis diagnoses."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cv2
import numpy as np

CONNECTIONS = (
    (11, 12),
    (11, 13),
    (13, 15),
    (12, 14),
    (14, 16),
    (11, 23),
    (12, 24),
    (23, 24),
    (23, 25),
    (25, 27),
    (24, 26),
    (26, 28),
    (27, 29),
    (29, 31),
    (28, 30),
    (30, 32),
    (15, 19),
    (16, 20),
)
VISIBLE = 0.5


def usable_pose(points: list[list[float]]) -> bool:
    if len(points) != 33 or min(points[i][3] for i in (11, 12, 23, 24)) < VISIBLE:
        return False
    visible = [p for p in points if p[3] >= VISIBLE and 0 <= p[0] <= 1 and 0 <= p[1] <= 1]
    if len(visible) < 10:
        return False
    extent_x = max(p[0] for p in visible) - min(p[0] for p in visible)
    extent_y = max(p[1] for p in visible) - min(p[1] for p in visible)
    return extent_x >= 0.08 and extent_y >= 0.25 and extent_x * extent_y >= 0.025


def file_hash(location: Path) -> str:
    digest = hashlib.sha256()
    with location.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def joint_angle(a: list[float], b: list[float], c: list[float], aspect: float) -> float | None:
    if min(a[3], b[3], c[3]) < VISIBLE:
        return None
    u = ((a[0] - b[0]) * aspect, a[1] - b[1])
    v = ((c[0] - b[0]) * aspect, c[1] - b[1])
    denom = math.hypot(*u) * math.hypot(*v)
    if denom < 1e-8:
        return None
    return round(math.degrees(math.acos(max(-1, min(1, (u[0] * v[0] + u[1] * v[1]) / denom)))), 1)


def summarize_frames(
    frames: list[dict[str, Any]], width: int, height: int, handedness: str
) -> dict[str, Any]:
    aspect = width / max(1, height)
    usable = [
        frame for frame in frames if len(frame["people"]) == 1 and usable_pose(frame["people"][0])
    ]
    multiple = sum(len(frame["people"]) > 1 for frame in frames)
    ratio = len(usable) / max(1, len(frames))
    angles: dict[str, list[float]] = {
        name: [] for name in ("left_elbow", "right_elbow", "left_knee", "right_knee")
    }
    definitions = {
        "left_elbow": (11, 13, 15),
        "right_elbow": (12, 14, 16),
        "left_knee": (23, 25, 27),
        "right_knee": (24, 26, 28),
    }
    wrist = 15 if handedness == "left" else 16
    motions: list[dict[str, float]] = []
    previous: dict[str, Any] | None = None
    feet_count = 0
    for frame in usable:
        points = frame["people"][0]
        feet_count += int(min(points[27][3], points[28][3]) >= VISIBLE)
        for name, indices in definitions.items():
            angle = joint_angle(points[indices[0]], points[indices[1]], points[indices[2]], aspect)
            if angle is not None:
                angles[name].append(angle)
        if previous is not None:
            old = previous["people"][0]
            delta = frame["t"] - previous["t"]
            shoulder = math.hypot(
                (points[11][0] - points[12][0]) * aspect, points[11][1] - points[12][1]
            )
            if (
                0 < delta < 0.35
                and shoulder > 0.04
                and min(points[wrist][3], old[wrist][3], points[11][3], points[12][3]) >= VISIBLE
            ):
                speed = (
                    math.hypot(
                        (points[wrist][0] - old[wrist][0]) * aspect,
                        points[wrist][1] - old[wrist][1],
                    )
                    / delta
                    / shoulder
                )
                motions.append({"t": frame["t"], "value": round(speed, 3)})
        previous = frame
    ranges = {
        name: {
            "min": round(float(np.percentile(values, 5)), 1),
            "max": round(float(np.percentile(values, 95)), 1),
            "samples": len(values),
        }
        for name, values in angles.items()
        if len(values) >= 5
    }
    candidates: list[dict[str, Any]] = []
    if motions:
        threshold = max(1.5, float(np.percentile([m["value"] for m in motions], 70)))
        # Peaks are review anchors, not classified strokes or contact events.
        for index in sorted(range(len(motions)), key=lambda i: motions[i]["value"], reverse=True):
            item = motions[index]
            if item["value"] < threshold or any(
                abs(item["t"] - p["peak_s"]) < 0.85 for p in candidates
            ):
                continue
            candidates.append(
                {
                    "peak_s": item["t"],
                    "start_s": max(0, round(item["t"] - 0.6, 3)),
                    "end_s": round(item["t"] + 0.6, 3),
                    "speed_proxy": item["value"],
                }
            )
            if len(candidates) >= 18:
                break
    candidates.sort(key=lambda p: p["peak_s"])
    enough = len(usable) >= 5 and ratio >= 0.35 and multiple == 0
    return {
        "sampled_frames": len(frames),
        "single_person_frames": len(usable),
        "multiple_person_frames": multiple,
        "detection_ratio": round(ratio, 4),
        "feet_visible_ratio": round(feet_count / max(1, len(usable)), 4),
        "individual_review_available": enough,
        "angle_ranges": ranges if enough else {},
        "motion": motions if enough else [],
        "segments": candidates if enough else [],
        "limitations": [
            "二维画面投影角度，不是标定后的关节角或发力测量。",
            "动作类型来自你的选择；手腕运动峰值只用于定位回放，不代表击球次数或触球时刻。",
            "当前模型检测人体关键点；不检测网球、球拍、落点和来球时机。",
        ],
    }


def inspect_image_quality(video: Path) -> dict[str, Any]:
    capture = cv2.VideoCapture(str(video))
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    samples: list[dict[str, float]] = []
    try:
        for offset in np.linspace(0, max(0, total - 1), min(12, max(1, total))):
            capture.set(cv2.CAP_PROP_POS_FRAMES, int(offset))
            ok, frame = capture.read()
            if not ok:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            samples.append(
                {
                    "brightness": float(gray.mean()),
                    "sharpness": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
                }
            )
    finally:
        capture.release()
    if not samples:
        raise ValueError("VIDEO_DECODE_FAILED")
    return {
        "samples": len(samples),
        "brightness": round(float(np.median([s["brightness"] for s in samples])), 1),
        "sharpness_proxy": round(float(np.median([s["sharpness"] for s in samples])), 1),
        "observation_kind": "measured",
        "pose_visibility": "pending_pose_detection",
    }


def analyze_pose(
    video: Path, model: Path, handedness: str, progress: Callable[[int], None]
) -> dict[str, Any]:
    import mediapipe as mp  # type: ignore[import-untyped]

    capture = cv2.VideoCapture(str(video))
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if fps <= 0 or total <= 0:
        capture.release()
        raise ValueError("VIDEO_DECODE_FAILED")
    stride = max(1, round(fps / 12))
    options = mp.tasks.vision.PoseLandmarkerOptions(
        base_options=mp.tasks.BaseOptions(
            model_asset_path=str(model), delegate=mp.tasks.BaseOptions.Delegate.CPU
        ),
        running_mode=mp.tasks.vision.RunningMode.VIDEO,
        num_poses=2,
        min_pose_detection_confidence=0.5,
        min_pose_presence_confidence=0.5,
        min_tracking_confidence=0.5,
    )
    frames: list[dict[str, Any]] = []
    try:
        with mp.tasks.vision.PoseLandmarker.create_from_options(options) as detector:
            for index in range(total):
                ok = capture.grab()
                if not ok:
                    break
                if index % stride:
                    continue
                ok, frame = capture.retrieve()
                if not ok:
                    continue
                image = mp.Image(
                    image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                )
                timestamp = round(index / fps * 1000)
                result = detector.detect_for_video(image, timestamp)
                people = [
                    [
                        [round(p.x, 5), round(p.y, 5), round(p.z, 5), round(p.visibility or 0, 4)]
                        for p in person
                    ]
                    for person in result.pose_landmarks
                ]
                raw_count = len(people)
                people = [person for person in people if usable_pose(person)]
                frames.append(
                    {
                        "t": round(index / fps, 3),
                        "people": people,
                        "raw_people_count": raw_count,
                        "accepted": len(people) == 1 and usable_pose(people[0]),
                    }
                )
                if len(frames) % 12 == 0:
                    progress(min(99, round(index / max(1, total) * 100)))
    finally:
        capture.release()
    return {
        "simulation": False,
        "observation_kind": "model_estimate",
        "width": width,
        "height": height,
        "sample_fps": round(fps / stride, 2),
        "frames": frames,
        "connections": CONNECTIONS,
        "summary": summarize_frames(frames, width, height, handedness),
    }


def evidence_frames(video: Path, pose: dict[str, Any], output: Path) -> list[dict[str, Any]]:
    valid = [f for f in pose["frames"] if len(f["people"]) == 1 and usable_pose(f["people"][0])]
    if not valid or not pose["summary"]["individual_review_available"]:
        return []
    segments = pose["summary"]["segments"]
    times = [s["peak_s"] for s in segments[:6]] or [valid[len(valid) // 2]["t"]]
    capture = cv2.VideoCapture(str(video))
    records: list[dict[str, Any]] = []
    try:
        for index, timestamp in enumerate(times):
            sample = min(valid, key=lambda f: abs(f["t"] - timestamp))
            capture.set(cv2.CAP_PROP_POS_MSEC, sample["t"] * 1000)
            ok, image = capture.read()
            if not ok:
                continue
            height, width = image.shape[:2]
            points = sample["people"][0]
            for a, b in CONNECTIONS:
                if min(points[a][3], points[b][3]) >= VISIBLE:
                    cv2.line(
                        image,
                        (round(points[a][0] * width), round(points[a][1] * height)),
                        (round(points[b][0] * width), round(points[b][1] * height)),
                        (180, 230, 40),
                        3,
                        cv2.LINE_AA,
                    )
            for p in points:
                if p[3] >= VISIBLE:
                    cv2.circle(
                        image,
                        (round(p[0] * width), round(p[1] * height)),
                        4,
                        (255, 255, 255),
                        -1,
                        cv2.LINE_AA,
                    )
            filename = f"evidence-{index + 1}.jpg"
            if not cv2.imwrite(str(output / filename), image):
                raise ValueError("EVIDENCE_WRITE_FAILED")
            records.append(
                {
                    "file": filename,
                    "t": sample["t"],
                    "sha256": file_hash(output / filename),
                    "kind": "model_overlay",
                }
            )
    finally:
        capture.release()
    return records
