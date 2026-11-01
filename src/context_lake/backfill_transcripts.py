#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Repair approved Fathom transcript placeholders; no automatic environment-file loading.

An explicit --package-dir identifies an export containing transcripts/. By default,
only empty .md files are repaired; --force also replaces nonempty transcripts.
Recording IDs and dates come from the existing filenames. A fetched recording
must resolve to that exact filename before any file is written. --dry-run never
requires credentials, fetches recordings or writes files.
"""
import argparse
import re
import sys
import time
from pathlib import Path
from typing import NamedTuple

from .fathom_to_markdown import (
    DEFAULT_MAX_PAGES,
    DEFAULT_MIN_INTERVAL,
    FathomAPIError,
    TranscriptFormatError,
    import_recording,
    non_negative_float,
    positive_integer,
    resolve_api_key,
    validate_meeting_date,
    write_new_file,
)

FILENAME = re.compile(r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})--.+--(?P<id>[1-9][0-9]*)\.md")


class Target(NamedTuple):
    path: Path
    meeting_date: str
    recording_id: int


def targets(package_dir, force=False, limit=None):
    """Select a fixed, sorted work list and validate it before any API call."""
    directory = package_dir / "transcripts"
    if not directory.is_dir() or directory.is_symlink():
        raise TranscriptFormatError("package-dir must contain a real transcripts/ directory")
    paths = [p for p in sorted(directory.glob("*.md"))
             if p.is_file() and not p.is_symlink() and (force or p.stat().st_size == 0)]
    if limit is not None:
        paths = paths[:limit]
    selected = []
    for path in paths:
        match = FILENAME.fullmatch(path.name)
        if not match:
            raise TranscriptFormatError(f"invalid transcript filename: {path.name}")
        meeting_date = match.group("date")
        validate_meeting_date(meeting_date)
        selected.append(Target(path, meeting_date, int(match.group("id"))))
    return selected


def backfill(args):
    selected = targets(args.package_dir.expanduser().resolve(), args.force, args.limit)
    if not selected:
        print("Nothing to repair in the selected scope.")
        return 0
    if args.dry_run:
        for target in selected:
            print(f"DRY RUN {target.path.name}: recording {target.recording_id}, date {target.meeting_date}")
        return 0

    # Resolve credentials only after validating scope; never load ~/.env implicitly.
    api_key = resolve_api_key()
    if args.save_json:
        args.save_json = args.save_json.expanduser().resolve()
        planned = [args.save_json / f"{target.recording_id}.json" for target in selected]
        if len(set(planned)) != len(planned):
            raise TranscriptFormatError("selected transcripts have duplicate raw-response destinations")
        for path in planned:
            if path.is_symlink() or path.is_dir() or (path.exists() and not args.force):
                raise TranscriptFormatError(f"refusing to replace raw response: {path}")

    repaired = failed = 0
    for position, target in enumerate(selected):
        if position and args.sleep:
            time.sleep(args.sleep)
        try:
            imported = import_recording(target.recording_id, api_key,
                                        meeting_date=target.meeting_date, max_pages=args.max_pages)
            expected = f"transcripts/{target.path.name}"
            if imported.relative_path != expected:
                raise TranscriptFormatError(
                    f"recording metadata resolves to {imported.relative_path}, expected {expected}; "
                    "review the renamed meeting before replacing its placeholder"
                )
            if target.path.is_symlink() or not target.path.is_file():
                raise TranscriptFormatError("target changed since selection; rerun after review")
            if not args.force and target.path.stat().st_size:
                raise TranscriptFormatError("target was filled since selection; refusing to replace it")
            if args.save_json:
                raw_path = args.save_json / f"{target.recording_id}.json"
                write_new_file(raw_path, imported.raw_body, overwrite=args.force,
                               description="raw transcript response")
            write_new_file(target.path, imported.rendered, overwrite=True, description="transcript")
            repaired += 1
            print(f"REPAIRED {target.path.name}")
        except (OSError, TranscriptFormatError, FathomAPIError) as error:
            failed += 1
            print(f"FAILED {target.path.name}: {error}", file=sys.stderr)
    print(f"Repaired: {repaired}; failed: {failed}")
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-dir", required=True, type=Path, help="explicit export root containing transcripts/")
    parser.add_argument("--save-json", type=Path, help="optional raw-response directory; keep outside the export unless approved")
    parser.add_argument("--sleep", type=non_negative_float, default=DEFAULT_MIN_INTERVAL,
                        help="seconds between recordings, including after failures (default 7)")
    parser.add_argument("--max-pages", type=positive_integer, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--limit", type=positive_integer, help="repair only the first N sorted targets")
    parser.add_argument("--force", action="store_true", help="also replace nonempty transcripts and existing raw responses")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        return backfill(args)
    except (OSError, TranscriptFormatError, FathomAPIError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
