"""Redacted diagnostic package export (BE-024).

Builds the local diagnostic package behind the frozen ``review.export``
method (AC-SPEC-036, FR-055) with these hard rules:

- **Only selected materials enter the package** (仅所选): the caller passes
  exactly the resource references to include; nothing is discovered or
  added from the store.
- **Original videos are never packaged**: an ``input_copy`` selection is
  refused as an item (``original_video_excluded``) regardless of consent —
  the default and the only behavior is no original video.
- **Text and JSON diagnostics are redacted** before packaging: identity and
  conversation keys (name, chat/transcript, e-mail, phone, youth/guardian
  identity) are removed and absolute paths are reduced to file names. Every
  packaged item is marked in a manifest that says ``redacted: true``.
- **Failed materials are recorded, not attached** (失败材料不附带): a
  missing or unreadable file becomes a ``failed`` manifest item; the
  package still contains the rest.
- **The manifest is complete**: every item carries its kind, mime, sha256
  and size; the manifest file itself is hashed into the contract resource.
- **No send capability**: this module writes files under the authorized
  root and nothing else — no network, no mail, no queue. Sending is a
  separate consented flow that lives outside the backend (模拟外发 stays
  in the WorkBuddy layer).

The caller must have validated the export consent (BE-011
``check_consent``) before calling; this module is pure packaging.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tennis_backend.domain.models import DomainViolation

#: Resource kinds allowed into a diagnostic package. ``input_copy`` (the
#: original video) is deliberately absent and refused if selected.
_ALLOWED_KINDS = frozenset(
    {"diagnostics", "evidence_frame", "evidence_clip", "trajectory", "report", "record"}
)

#: JSON keys stripped from text diagnostics before packaging. Any key
#: CONTAINING "name" is treated as sensitive as well (real_name, nickname,
#: player_name, …): identity leakage is a red line, over-redaction of a
#: diagnostic key is not.
_SENSITIVE_KEYS = frozenset(
    {
        "name",
        "full_name",
        "user_name",
        "subject_name",
        "real_name",
        "nickname",
        "player_name",
        "surname",
        "given_name",
        "chat",
        "transcript",
        "messages",
        "message",
        "email",
        "phone",
        "birthdate",
        "guardian_identity",
        "guardian_simulated",
        "guardian_required",
        "guardian",
        "youth_identity",
        "age_band",
    }
)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in _SENSITIVE_KEYS or "name" in lowered


#: Absolute-path shape with segments allowed to contain spaces and
#: non-ASCII characters (a previous pattern truncated at the first space
#: and leaked filename segments — BE-024 review). The final segment may be
#: a dotted file name; a bare directory chain also matches, while trailing
#: prose after the path is left alone.
_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:\\|/)(?:[^/\\\n\"']+/)+(?:[^/\\\n\"']*\.\w+)")


@dataclass(frozen=True)
class SelectedMaterial:
    """One caller-selected resource reference with its existing file."""

    resource_id: str
    path: Path
    relative: str
    kind: str
    mime: str


@dataclass(frozen=True)
class PackageOutcome:
    """Contract shapes plus the written files and the item ledger."""

    result: dict[str, Any]
    files: tuple[Path, ...]
    items: tuple[dict[str, Any], ...]


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(str(key)):
                cleaned[str(key)] = "[REDACTED]"
            else:
                cleaned[str(key)] = _redact_json(item)
        return cleaned
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, str):
        return _ABSOLUTE_PATH.sub("[REDACTED_PATH]", value)
    return value


def _redact_text(text: str) -> str:
    return _ABSOLUTE_PATH.sub("[REDACTED_PATH]", text)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _place(staging: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, target)
    return target


def build_diagnostic_package(
    *,
    package_id: str,
    root: Path,
    relative_dir: str,
    task_id: str,
    version: int,
    selected: tuple[SelectedMaterial, ...],
) -> PackageOutcome:
    """Package the selected, redacted diagnostics under ``root``.

    Returns the frozen ``deletion:export_result``-shaped result (the
    manifest resource plus the included resources), the written files and
    the full item ledger including refused and failed entries.
    """
    root = Path(root)
    if not selected:
        raise DomainViolation("EXPORT_EMPTY", "a diagnostic package needs at least one selection")
    package_dir = Path(relative_dir) / package_id
    items: list[dict[str, Any]] = []
    resources: list[dict[str, Any]] = []
    files: list[Path] = []

    for material in selected:
        item: dict[str, Any] = {
            "resource_id": material.resource_id,
            "kind": material.kind,
            "mime": material.mime,
        }
        # appended once, mutated in place below: every selection gets a ledger entry
        items.append(item)
        if material.kind == "input_copy":
            # specific reason first: the original video is never packaged,
            # regardless of selection or consent (FR-055)
            item.update(status="refused", reason="original_video_excluded")
            continue
        if material.kind not in _ALLOWED_KINDS:
            item.update(status="refused", reason="kind_not_allowed_in_diagnostic_package")
            continue
        if not material.path.is_file():
            item.update(status="failed", reason="source_file_missing")
            continue
        try:
            relative = f"{package_dir}/files/{material.resource_id}-{Path(material.relative).name}"
            if material.mime in ("application/json",) or material.path.suffix == ".json":
                payload = _redact_json(json.loads(material.path.read_text(encoding="utf-8")))
                text = json.dumps(payload, ensure_ascii=False, indent=2)
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".json", delete=False, encoding="utf-8"
                ) as handle:
                    handle.write(text)
                    staging = Path(handle.name)
                final_mime = "application/json"
            elif material.mime.startswith("text/") or material.path.suffix in (
                ".txt",
                ".log",
                ".md",
            ):
                redacted = _redact_text(material.path.read_text(encoding="utf-8"))
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".txt", delete=False, encoding="utf-8"
                ) as handle:
                    handle.write(redacted)
                    staging = Path(handle.name)
                final_mime = "text/plain"
            else:
                # binary rendered artifacts: copied as-is, they were
                # generated from measured media and carry no identity fields
                with tempfile.NamedTemporaryFile(
                    suffix=material.path.suffix, delete=False
                ) as handle:
                    staging = Path(handle.name)
                    handle.write(material.path.read_bytes())
                final_mime = material.mime
            final = _place(staging, root / relative)
        except (OSError, ValueError):
            item.update(status="failed", reason="unreadable_source")
            continue
        files.append(final)
        item.update(
            status="included",
            path=relative,
            sha256=_hash(final),
            size_bytes=final.stat().st_size,
            mime=final_mime,
        )
        resources.append(
            {
                "resource_id": material.resource_id,
                "path": relative,
                "mime": final_mime,
                "sha256": item["sha256"],
                "size_bytes": item["size_bytes"],
                "kind": material.kind,
                "simulation": True,
            }
        )

    manifest = {
        "package_id": package_id,
        "task_id": task_id,
        "version": version,
        "created": datetime.now(UTC).isoformat(),
        "redacted": True,
        "items": items,
        "notes": [
            "no original video, no identity or conversation content by default",
            "this package carries no send capability; sending is a separate consented flow",
        ],
    }
    manifest_relative = f"{package_dir}/manifest.json"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(json.dumps(manifest, ensure_ascii=False, indent=2))
        staging = Path(handle.name)
    manifest_final = _place(staging, root / manifest_relative)
    files.append(manifest_final)
    manifest_resource = {
        "resource_id": f"{package_id}-manifest",
        "path": manifest_relative,
        "mime": "application/json",
        "sha256": _hash(manifest_final),
        "size_bytes": manifest_final.stat().st_size,
        "kind": "diagnostics",
        "simulation": True,
    }
    result = {
        "package_id": package_id,
        "manifest": manifest_resource,
        "resources": resources,
        "redacted": True,
        "simulation": True,
    }
    return PackageOutcome(result=result, files=tuple(files), items=tuple(items))
