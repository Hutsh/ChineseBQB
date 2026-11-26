"""Build and validate the repository's assistant-facing sticker index."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from build_site import ROOT, category_identity, folders, image_files
from build_sticker_catalog import VERSION, media_info, validate
from select_sticker import ACTION_SCOPES, DEFAULT_CATALOG, OCCASIONS, RESPONSE_MODES


INDEX = DEFAULT_CATALOG
WORK = ROOT / ".sticker-catalog" / "cloud"
REVIEWED_SAMPLE = ROOT / ".sticker-catalog" / "reviewed-catalog.jsonl"
PROFILE_VALUES = {"occasion": OCCASIONS, "action_scope": ACTION_SCOPES, "response_mode": RESPONSE_MODES}


class IndexBusy(RuntimeError):
    """Another process is modifying the sticker index."""


@contextmanager
def index_lock():
    WORK.mkdir(parents=True, exist_ok=True)
    with (WORK / "runner.lock").open("a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise IndexBusy("Another sticker index writer is running.") from exc
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def validate_result(analysis, profile):
    validate(analysis)
    if not isinstance(profile, dict) or set(profile) != set(PROFILE_VALUES):
        raise ValueError("Incomplete selection profile")
    for key, values in PROFILE_VALUES.items():
        if profile[key] not in values:
            raise ValueError(f"Invalid selection profile field: {key}")


def load_rows(path):
    if not path.is_file():
        return []
    with path.open(encoding="utf-8-sig") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def read_index():
    return load_rows(INDEX)


def write_index(rows):
    temporary = INDEX.with_name(f".{INDEX.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        # On Windows a simultaneous reader can briefly hold the destination open.
        for attempt in range(4):
            try:
                temporary.replace(INDEX)
                break
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(0.15 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def initialize_index():
    existing = {row["id"]: row for row in read_index()}
    reviewed = {row["id"]: row for row in load_rows(REVIEWED_SAMPLE)}
    rows = []
    for folder in folders(ROOT):
        _, _, series = category_identity(folder.name)
        for source in image_files(folder):
            path = source.relative_to(ROOT).as_posix()
            identity = hashlib.sha256(path.encode()).hexdigest()[:24]
            stat = source.stat()
            base = dict(id=identity, path=path, series=series, source_mtime_ns=stat.st_mtime_ns,
                        source_size=stat.st_size, schema_version=VERSION)
            previous = existing.get(identity, {})
            if all(previous.get(key) == value for key, value in base.items()):
                rows.append(previous)
                continue
            try:
                with source.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                base.update(sha256=digest, media=media_info(source), analysis_status="pending")
            except (OSError, ValueError) as exc:
                rows.append(dict(base, analysis_status="error", error=str(exc)))
                continue
            seed = reviewed.get(identity, {})
            for cached in (previous, seed):
                if cached.get("analysis_status") != "visually_reviewed" or cached.get("sha256") != digest:
                    continue
                analysis, profile = cached.get("analysis"), cached.get("selection_profile")
                try:
                    validate_result(analysis, profile)
                except ValueError:
                    continue
                base.update(analysis_status="visually_reviewed", analysis=analysis, selection_profile=profile,
                            selection_profile_version=1, evidence=cached.get("evidence", {}))
                break
            rows.append(base)
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Duplicate sticker IDs")
    write_index(rows)
    return rows


def import_reviewed(rows, path):
    records = json.loads(path.read_text(encoding="utf-8-sig"))
    by_id = {row["id"]: row for row in rows}
    changes = []
    seen = set()
    for record in records:
        identity = record["id"]
        row = by_id.get(identity)
        if row is None or identity in seen or any(row[key] != record[key] for key in ("path", "sha256")):
            raise ValueError(f"Unexpected sticker ID or changed asset: {identity}")
        seen.add(identity)
        analysis, profile = record["analysis"], record["selection_profile"]
        validate_result(analysis, profile)
        frames = record["frames"]
        if not frames or sorted(set(frames)) != frames or not all(
                type(frame) is int and 0 <= frame < row["media"]["frame_count"] for frame in frames):
            raise ValueError(f"Invalid frame numbers: {identity}")
        evidence = {"source": "Codex visual review", "frames": frames}
        if record.get("decode_recovered") is True:
            evidence["decode_recovered"] = True
        changes.append((row, analysis, profile, evidence))
    for row, analysis, profile, evidence in changes:
        row.pop("error", None)
        row.update(analysis_status="visually_reviewed", analysis=analysis, selection_profile=profile,
                   selection_profile_version=1, evidence=evidence)
    write_index(rows)
    return len(changes)


def verify_index(rows):
    assets = {source.relative_to(ROOT).as_posix() for folder in folders(ROOT) for source in image_files(folder)}
    if len(rows) != len(assets) or {row["path"] for row in rows} != assets:
        raise ValueError("The index does not match the current sticker paths")
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("Duplicate sticker IDs")
    for row in rows:
        source = ROOT / row["path"]
        stat = source.stat()
        if (row["source_mtime_ns"], row["source_size"]) != (stat.st_mtime_ns, stat.st_size):
            raise ValueError(f"Image changed after indexing: {row['path']}")
        status = row["analysis_status"]
        if status == "visually_reviewed":
            validate_result(row.get("analysis"), row.get("selection_profile"))
        elif status not in ("pending", "error"):
            raise ValueError(f"Unexpected analysis status: {status}")
    return Counter(row["analysis_status"] for row in rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--import-reviewed", type=Path, help="Import a completed subagent batch before starting the bulk writer")
    mode.add_argument("--verify", action="store_true", help="Verify the existing snapshot and report whether analysis is complete")
    args = parser.parse_args()
    if args.verify:
        rows = read_index()
        try:
            counts = verify_index(rows)
        except (KeyError, OSError, ValueError) as exc:
            parser.exit(1, f"{exc}\n")
        complete = bool(rows) and counts["visually_reviewed"] == len(rows)
        print(json.dumps(dict(total=len(rows), counts=counts, complete=complete)), flush=True)
        return 0 if complete else 2
    try:
        with index_lock():
            rows = initialize_index()
            if args.import_reviewed:
                print(f"Imported {import_reviewed(rows, args.import_reviewed)} reviewed records.", flush=True)
    except IndexBusy as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps({"total": len(rows), "counts": Counter(row["analysis_status"] for row in rows)}), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
