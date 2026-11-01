#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Fetch Fathom transcripts from the API and render them as deterministic Markdown."""

import argparse
import email.utils
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple

API_ROOT = "https://api.fathom.ai/external/v1"
API_KEY_ENV = "FATHOM_API_KEY"

# The transcript and summary endpoints are rate limited far more tightly than the
# rest of the API: 30 calls per minute nominally, degrading to 5 under load. Pacing
# sequential fetches is cheaper than retrying after a 429.
DEFAULT_MIN_INTERVAL = 7.0
DEFAULT_MAX_RETRIES = 5
DEFAULT_TIMEOUT = 60.0
DEFAULT_PAGE_LIMIT = 50
DEFAULT_MAX_PAGES = 40

# A recording created just before midnight UTC belongs to the previous local day, so
# metadata lookups widen the window rather than demanding an exact calendar match.
DATE_WINDOW = timedelta(days=2)

SOURCE_URL_RE = re.compile(r"https://fathom\.video/calls/[1-9][0-9]*")
SEGMENT_RE = re.compile(
    r"^\[(?P<label>(?:[0-9]+:)?[0-9]{2}:[0-9]{2})\]"
    r"\((?P<url>https://fathom\.video/calls/[1-9][0-9]*\?timestamp=(?P<seconds>[0-9]+(?:\.[0-9]+)?))\) "
    r"(?P<body>.+)$"
)
ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
API_TIMESTAMP_RE = re.compile(r"\A(?:(?P<hours>[0-9]+):)?(?P<minutes>[0-9]{1,2}):(?P<seconds>[0-9]{2})\Z")
# Fathom's plain-text transcript download: a header (title, share link, "---"), then
# one "0:00 - Speaker" heading per turn followed by its text, indented two spaces.
# Copy-pasted transcripts drift from that, so the parser tolerates lost or changed
# indentation, a missing header, CRLF/BOM, and en/em dashes or non-breaking spaces
# around the heading's separator.
EXPORT_HEADING_RE = re.compile(
    r"(?P<timestamp>(?:[0-9]+:)?[0-9]{1,2}:[0-9]{2})[ \t\u00a0]*[-\u2013\u2014][ \t\u00a0]*(?P<speaker>\S.*?)"
)
INPUT_FORMATS = ("auto", "export", "lines")


class TranscriptFormatError(ValueError):
    """Report invalid metadata or malformed raw transcript content."""


class FathomAPIError(RuntimeError):
    """Report a Fathom API request that failed or returned unusable data."""


class MeetingMetadata(NamedTuple):
    """The metadata a rendered transcript needs, as reported by the Fathom API."""

    recording_id: int
    title: str
    meeting_date: str
    source_url: str


class ValidatedSegment(NamedTuple):
    """One API segment with a stable identity separate from its rendered name."""

    identity: tuple[str, str]
    display_name: str
    seconds: int
    text: str


class ImportResult(NamedTuple):
    """A fetched recording ready for publication, with no filesystem side effects."""

    relative_path: str
    rendered: str
    raw_body: bytes


class PublicationArtifact(NamedTuple):
    """One filesystem destination and its fully prepared content."""

    target: Path
    content: str | bytes
    description: str


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so an authenticated request is never replayed elsewhere."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


DEFAULT_OPENER = urllib.request.build_opener(NoRedirectHandler()).open


def slugify_title(title: str) -> str:
    """Convert a canonical meeting title to a lowercase hyphenated slug."""
    slug_parts: list[str] = []
    separator_pending = False

    for character in title.casefold():
        if character.isalnum():
            if separator_pending and slug_parts:
                slug_parts.append("-")
            slug_parts.append(character)
            separator_pending = False
        elif slug_parts:
            separator_pending = True

    slug = "".join(slug_parts)
    if not slug:
        raise TranscriptFormatError("title must contain at least one alphanumeric character")
    return slug


def validate_title(title: str) -> None:
    """Require a non-empty, single-line canonical title without hidden padding."""
    if not title or title != title.strip():
        raise TranscriptFormatError("title must be non-empty and have no surrounding whitespace")
    if title.splitlines() != [title]:
        raise TranscriptFormatError("title must fit on one line")


def validate_meeting_date(meeting_date: str) -> None:
    """Require a real calendar date in extended ISO YYYY-MM-DD format."""
    if not ISO_DATE_RE.fullmatch(meeting_date):
        raise TranscriptFormatError("date must use YYYY-MM-DD format")
    try:
        date.fromisoformat(meeting_date)
    except ValueError as error:
        raise TranscriptFormatError(f"invalid meeting date: {meeting_date}") from error


def validate_recording_id(recording_id: int) -> None:
    """Require a positive numeric Fathom recording identifier."""
    if not isinstance(recording_id, int) or isinstance(recording_id, bool):
        raise TranscriptFormatError("recording ID must be a positive integer")
    if recording_id <= 0:
        raise TranscriptFormatError("recording ID must be a positive integer")


def validate_source_url(source_url: str) -> None:
    """Require a canonical HTTPS Fathom call URL without query or fragment data."""
    if not SOURCE_URL_RE.fullmatch(source_url):
        raise TranscriptFormatError("source URL must match https://fathom.video/calls/<call-id>")


def validate_metadata(title: str, meeting_date: str, recording_id: int, source_url: str) -> None:
    """Validate all metadata used in a rendered transcript."""
    validate_title(title)
    validate_meeting_date(meeting_date)
    validate_recording_id(recording_id)
    validate_source_url(source_url)


def package_path(title: str, meeting_date: str, recording_id: int, source_url: str) -> str:
    """Return the deterministic package-relative path for a meeting transcript."""
    validate_metadata(title, meeting_date, recording_id, source_url)
    return f"transcripts/{meeting_date}--{slugify_title(title)}--{recording_id}.md"


def _timestamp_seconds(label: str) -> int:
    """Convert an MM:SS or H:MM:SS timestamp label to whole seconds."""
    fields = [int(field) for field in label.split(":")]
    if fields[-1] >= 60 or (len(fields) == 3 and fields[-2] >= 60):
        raise TranscriptFormatError(f"invalid timestamp label: {label}")
    if len(fields) == 2:
        minutes, seconds = fields
        return minutes * 60 + seconds
    hours, minutes, seconds = fields
    return hours * 3600 + minutes * 60 + seconds


def parse_api_timestamp(timestamp: str) -> int:
    """Convert a Fathom API HH:MM:SS offset to whole seconds."""
    if not isinstance(timestamp, str):
        raise TranscriptFormatError(f"timestamp must be a string, got {type(timestamp).__name__}")
    match = API_TIMESTAMP_RE.match(timestamp)
    if not match:
        raise TranscriptFormatError(f"unrecognized API timestamp: {timestamp!r}")
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    if seconds >= 60 or minutes >= 60:
        raise TranscriptFormatError(f"invalid API timestamp: {timestamp!r}")
    return int(match.group("hours") or 0) * 3600 + minutes * 60 + seconds


def format_timestamp_label(total_seconds: int) -> str:
    """Render whole seconds as the MM:SS or H:MM:SS label used in transcript links."""
    if total_seconds < 0:
        raise TranscriptFormatError("timestamp cannot be negative")
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def validated_api_segments(segments: Sequence[Any]) -> list[ValidatedSegment]:
    """Validate API transcript segments without conflating identity and display name.

    Display names and text are copied verbatim. Any line boundary would silently
    change the line-oriented transcript shape, so it is rejected even when it is
    trailing or is the entire value. Empty text remains representable and valid.
    """
    if not isinstance(segments, Sequence) or isinstance(segments, (str, bytes)):
        raise TranscriptFormatError("transcript must be a list of segments")
    if not segments:
        raise TranscriptFormatError("transcript must contain at least one segment")

    validated: list[ValidatedSegment] = []
    previous_seconds = -1
    for position, segment in enumerate(segments, start=1):
        if not isinstance(segment, dict):
            raise TranscriptFormatError(f"segment {position} is not an object")

        speaker_field = segment.get("speaker")
        if not isinstance(speaker_field, dict):
            raise TranscriptFormatError(f"segment {position} has no speaker object")
        speaker = speaker_field.get("display_name")
        if not isinstance(speaker, str) or not speaker.strip():
            raise TranscriptFormatError(f"segment {position} has no speaker display name")
        if speaker.splitlines() != [speaker]:
            raise TranscriptFormatError(f"segment {position} speaker name spans multiple lines")
        # A line is parsed by splitting on the first ": ", so a name such as
        # "Doe: Jane" would be attributed to "Doe" with the rest folded into the
        # spoken text. Bare colons remain unambiguous in all three tools.
        if ": " in speaker:
            raise TranscriptFormatError(
                f"segment {position} speaker name {speaker!r} contains ': ', "
                "which collides with the speaker and text delimiter"
            )

        email = speaker_field.get("matched_calendar_invitee_email")
        if email is not None and not isinstance(email, str):
            raise TranscriptFormatError(
                f"segment {position} matched calendar invitee email must be a string or null"
            )
        normalized_email = email.strip().casefold() if isinstance(email, str) else ""
        identity = ("email", normalized_email) if normalized_email else ("display_name", speaker)

        text = segment.get("text")
        if not isinstance(text, str):
            raise TranscriptFormatError(f"segment {position} has no text")
        if text and text.splitlines() != [text]:
            raise TranscriptFormatError(f"segment {position} text spans multiple lines")

        seconds = parse_api_timestamp(segment.get("timestamp"))
        # Checked on the source segments, because coalescing a speaker's run keeps
        # only its opening timestamp and would otherwise hide an out-of-order pair.
        if seconds < previous_seconds:
            raise TranscriptFormatError(f"segment {position} timestamp precedes the previous segment")
        previous_seconds = seconds

        validated.append(ValidatedSegment(identity, speaker, seconds, text))
    return validated


def coalesce_turns(segments: Sequence[ValidatedSegment]) -> list[ValidatedSegment]:
    """Merge consecutive nonblank segments with the same stable speaker identity."""
    turns: list[tuple[tuple[str, str], str, int, list[str]]] = []
    for identity, display_name, seconds, text in segments:
        # Blank utterances are omitted before grouping so they cannot split a
        # speaker's turn or donate an earlier timestamp to its first spoken text.
        if not text.strip():
            continue
        if turns and turns[-1][0] == identity:
            turns[-1][3].append(text)
        else:
            turns.append((identity, display_name, seconds, [text]))

    return [
        ValidatedSegment(identity, display_name, seconds, " ".join(texts))
        for identity, display_name, seconds, texts in turns
    ]


def transcript_from_api_segments(segments: Sequence[Any], source_url: str, *, coalesce: bool = True) -> str:
    """Convert Fathom API transcript segments to the canonical line-oriented form."""
    validate_source_url(source_url)
    validated = validated_api_segments(segments)
    if coalesce:
        validated = coalesce_turns(validated)

    lines = [
        f"[{format_timestamp_label(segment.seconds)}]"
        f"({source_url}?timestamp={segment.seconds}) {segment.display_name}: {segment.text}"
        for segment in validated
    ]
    # Reuse the shared validator so an API-sourced transcript satisfies exactly the
    # same invariants as one recovered from a stored raw record.
    return normalize_transcript("\n".join(lines), source_url)


def export_segments(raw_export: str) -> list[dict[str, Any]]:
    """Parse Fathom's plain-text transcript download into API-shaped segments.

    Lines before the first heading are header (title, share link, "---") and are
    ignored; metadata is supplied explicitly instead. An unindented line matching
    "0:00 - Speaker" starts a turn. Every other nonblank line is one utterance of
    the current turn, so coalescing and validation match the API path exactly.

    Text is copied verbatim apart from the transcript's own text indent: the
    shortest indentation used by every text line (two spaces in a native
    download, a tab when pasted, or none when a paste drops it). Deeper nesting,
    such as text under a Fathom highlight, is kept. A line that does not start
    with that indent is stripped of its indentation. Indented lines are never headings, so a spoken "5:30 - ..." in a
    native download cannot start a turn. Heading errors, such as out-of-order
    timestamps, name the export's file line.
    """
    turns: list[tuple[dict[str, Any], list[str]]] = []
    previous_seconds = -1
    for line_number, line in enumerate(raw_export.lstrip("\ufeff").splitlines(), start=1):
        if not line.strip():
            continue
        match = None if line[:1].isspace() else EXPORT_HEADING_RE.fullmatch(line.strip())
        if match:
            heading = {"timestamp": match.group("timestamp"), "speaker": {"display_name": match.group("speaker")}}
            # Validate the heading here, where its file line is known; the shared
            # validator later sees only segment positions, which count text lines.
            try:
                (validated,) = validated_api_segments([{**heading, "text": ""}])
            except TranscriptFormatError as error:
                detail = re.sub(r"\Asegment 1 ", "", str(error))
                raise TranscriptFormatError(f"export line {line_number}: {detail}") from error
            if validated.seconds < previous_seconds:
                raise TranscriptFormatError(
                    f"export line {line_number}: timestamp {heading['timestamp']} precedes the previous turn"
                )
            previous_seconds = validated.seconds
            turns.append((heading, []))
        elif turns:
            turns[-1][1].append(line)

    indents = [line[: len(line) - len(line.lstrip())] for _, texts in turns for line in texts]
    # A paste that drops the base indent leaves some text lines unindented; then the
    # remaining indentation is nesting, not the transcript's indent, and is kept.
    indent = "" if "" in indents else min(indents, key=len, default="")
    segments = [
        {**heading, "text": line[len(indent) :] if line.startswith(indent) else line.lstrip()}
        for heading, texts in turns
        for line in texts
    ]
    if not segments:
        raise TranscriptFormatError("no '0:00 - Speaker' turns with text found in the export")
    return segments


def detect_input_format(raw_transcript: str) -> str:
    """Return 'lines' when the first nonblank line is a canonical linked segment, else 'export'."""
    for line in raw_transcript.lstrip("\ufeff").splitlines():
        if line.strip():
            return "lines" if SEGMENT_RE.fullmatch(line) else "export"
    return "lines"


def transcript_from_export(raw_export: str, source_url: str, *, coalesce: bool = True) -> str:
    """Convert Fathom's plain-text transcript export to the canonical line-oriented form."""
    return transcript_from_api_segments(export_segments(raw_export), source_url, coalesce=coalesce)


def normalize_transcript(raw_transcript: str, source_url: str) -> str:
    """Validate Fathom segment lines and normalize only line endings and outer blanks."""
    validate_source_url(source_url)
    lines = raw_transcript.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        raise TranscriptFormatError("transcript must contain at least one segment")

    previous_seconds = -1
    segment_count = 0
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        match = SEGMENT_RE.fullmatch(line)
        if not match:
            raise TranscriptFormatError(f"line {line_number} is not a Fathom transcript segment")

        linked_url = match.group("url").partition("?")[0]
        if linked_url != source_url:
            raise TranscriptFormatError(f"line {line_number} links to a different Fathom call")

        speaker, separator, _text = match.group("body").partition(": ")
        if not separator or not speaker.strip():
            raise TranscriptFormatError(f"line {line_number} must contain a speaker followed by ': '")

        label_seconds = _timestamp_seconds(match.group("label"))
        linked_seconds = Decimal(match.group("seconds"))
        if label_seconds != linked_seconds:
            raise TranscriptFormatError(f"line {line_number} timestamp label does not match its link")
        if label_seconds < previous_seconds:
            raise TranscriptFormatError(f"line {line_number} timestamp precedes the previous segment")

        previous_seconds = label_seconds
        segment_count += 1

    if not segment_count:
        raise TranscriptFormatError("transcript must contain at least one segment")
    return "\n".join(lines)


def render_markdown(
    title: str,
    meeting_date: str,
    recording_id: int,
    source_url: str,
    raw_transcript: str,
) -> str:
    """Render validated metadata and a verbatim transcript as canonical Markdown."""
    validate_metadata(title, meeting_date, recording_id, source_url)
    transcript = normalize_transcript(raw_transcript, source_url)
    quoted_title = json.dumps(title, ensure_ascii=False)
    quoted_source_url = json.dumps(source_url, ensure_ascii=False)
    return (
        "---\n"
        f"title: {quoted_title}\n"
        f"date: {meeting_date}\n"
        f"recording_id: {recording_id}\n"
        f"source_url: {quoted_source_url}\n"
        "---\n\n"
        f"# {title}\n\n"
        f"{transcript}\n"
    )


def write_new_file(
    target: Path,
    content: str | bytes,
    *,
    overwrite: bool = False,
    description: str = "file",
) -> Path:
    """Write a file without silent replacement, creating parent directories.

    Bytes are written verbatim so a stored API response keeps its exact encoding.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    binary = isinstance(content, bytes)
    if overwrite:
        if binary:
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8", newline="\n")
        return target
    mode = "xb" if binary else "x"
    text_options: dict[str, Any] = {} if binary else {"encoding": "utf-8", "newline": "\n"}
    try:
        with target.open(mode, **text_options) as output_file:
            output_file.write(content)
    except FileExistsError as error:
        raise TranscriptFormatError(f"refusing to overwrite existing {description}: {target}") from error
    return target


def write_package_file(output_dir: Path, relative_path: str, content: str, *, overwrite: bool = False) -> Path:
    """Write rendered Markdown beneath an output root without silent replacement."""
    return write_new_file(
        output_dir / relative_path,
        content,
        overwrite=overwrite,
        description="transcript",
    )


def resolve_api_key() -> str:
    """Return the Fathom API key from the environment, never from the command line."""
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        raise FathomAPIError(f"{API_KEY_ENV} is not set; export your Fathom API key before running")
    return api_key


def parse_retry_after(raw_value: str | None, *, now: datetime | None = None) -> float | None:
    """Interpret a Retry-After header, distinguishing zero from an invalid value."""
    if raw_value is None:
        return None
    candidate = raw_value.strip()
    if not candidate:
        return None
    try:
        seconds = float(candidate)
    except ValueError:
        pass
    else:
        if not math.isfinite(seconds) or seconds < 0:
            return None
        return seconds

    try:
        deadline = email.utils.parsedate_to_datetime(candidate)
    except (TypeError, ValueError):
        return None
    if deadline is None:
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return max(0.0, (deadline - reference).total_seconds())


def _sleep_for_retry(response_headers: Any, attempt: int, sleep: Callable[[float], None]) -> None:
    """Wait out a rate limit, honouring even a zero Retry-After value."""
    retry_after = (
        parse_retry_after(response_headers.get("Retry-After"))
        if response_headers is not None
        else None
    )
    sleep(min(60.0, 2.0**attempt) if retry_after is None else retry_after)


def api_request(
    path: str,
    api_key: str,
    *,
    params: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    opener: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """Perform an authenticated GET against the Fathom API and return the raw body.

    Retries rate limits and transient server errors; every other failure is raised.
    The API key is never included in an exception message.
    """
    if opener is None:
        opener = DEFAULT_OPENER

    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    url = f"{API_ROOT}{path}{query}"
    request = urllib.request.Request(
        url,
        headers={"X-Api-Key": api_key, "Accept": "application/json"},
        method="GET",
    )

    for attempt in range(max_retries + 1):
        try:
            with opener(request, timeout=timeout) as response:
                payload = response.read()
            break
        except urllib.error.HTTPError as error:
            retryable = error.code == 429 or 500 <= error.code < 600
            if not retryable or attempt == max_retries:
                detail = "authentication failed" if error.code in (401, 403) else error.reason
                raise FathomAPIError(f"GET {path} failed with HTTP {error.code}: {detail}") from error
            _sleep_for_retry(getattr(error, "headers", None), attempt, sleep)
        except urllib.error.URLError as error:
            if attempt == max_retries:
                raise FathomAPIError(f"GET {path} failed: {error.reason}") from error
            sleep(min(60.0, 2.0**attempt))
    else:  # pragma: no cover - loop always breaks or raises
        raise FathomAPIError(f"GET {path} exhausted retries")

    return payload


def decode_api_object(payload: bytes, path: str) -> dict[str, Any]:
    """Decode an API response body that must be a JSON object."""
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FathomAPIError(f"GET {path} returned a body that is not JSON") from error
    if not isinstance(decoded, dict):
        raise FathomAPIError(f"GET {path} returned {type(decoded).__name__}, expected an object")
    return decoded


def api_get(path: str, api_key: str, **request_options: Any) -> dict[str, Any]:
    """Perform an authenticated GET and decode the JSON object it returns."""
    return decode_api_object(api_request(path, api_key, **request_options), path)


def fetch_transcript_document(recording_id: int, api_key: str, **request_options: Any) -> tuple[list[Any], bytes]:
    """Fetch one recording's transcript, returning its segments and the raw body.

    The undecoded bytes are returned alongside so --save-json can store exactly what
    the API sent rather than a re-serialized approximation of it.
    """
    validate_recording_id(recording_id)
    path = f"/recordings/{recording_id}/transcript"
    body = api_request(path, api_key, **request_options)
    segments = decode_api_object(body, path).get("transcript")
    if not isinstance(segments, list):
        raise FathomAPIError(f"recording {recording_id} returned no transcript array")
    return segments, body


def fetch_transcript_segments(recording_id: int, api_key: str, **request_options: Any) -> list[Any]:
    """Fetch the transcript segment list for one recording."""
    return fetch_transcript_document(recording_id, api_key, **request_options)[0]


def iter_meetings(
    api_key: str,
    *,
    created_after: str | None = None,
    created_before: str | None = None,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    max_pages: int = DEFAULT_MAX_PAGES,
    **request_options: Any,
) -> Iterable[dict[str, Any]]:
    """Yield meeting records newest first, following the API's opaque cursor."""
    cursor: str | None = None
    for _page in range(max_pages):
        params = {"limit": str(page_limit)}
        if created_after:
            params["created_after"] = created_after
        if created_before:
            params["created_before"] = created_before
        if cursor:
            params["cursor"] = cursor

        payload = api_get("/meetings", api_key, params=params, **request_options)
        items = payload.get("items")
        if not isinstance(items, list):
            raise FathomAPIError("/meetings returned no items array")
        for item in items:
            if isinstance(item, dict):
                yield item

        cursor = payload.get("next_cursor") or None
        if not cursor:
            return


def _utc_date_of(item: dict[str, Any]) -> str | None:
    """Return the UTC calendar date the meeting started, when reported."""
    for field in ("recording_start_time", "scheduled_start_time"):
        value = item.get(field)
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            return parsed.astimezone(timezone.utc).date().isoformat()
    return None


def _date_window(meeting_date: str) -> tuple[str, str]:
    """Return an inclusive UTC window around a local meeting date."""
    centre = date.fromisoformat(meeting_date)
    after = (centre - DATE_WINDOW).isoformat()
    before = (centre + DATE_WINDOW).isoformat()
    return f"{after}T00:00:00Z", f"{before}T00:00:00Z"


def _meeting_metadata_of(recording_id: int, item: dict[str, Any]) -> MeetingMetadata:
    """Extract the metadata a rendered transcript needs from one meeting record."""
    title = item.get("title") or item.get("meeting_title")
    if not isinstance(title, str) or not title.strip():
        raise FathomAPIError(f"recording {recording_id} has no usable title")

    source_url = item.get("url")
    if not isinstance(source_url, str) or not SOURCE_URL_RE.fullmatch(source_url):
        raise FathomAPIError(
            f"recording {recording_id} reports url {source_url!r}, "
            "which is not a canonical https://fathom.video/calls/<call-id> link"
        )

    meeting_date = _utc_date_of(item)
    if meeting_date is None:
        raise FathomAPIError(f"recording {recording_id} reports no usable start time")

    return MeetingMetadata(recording_id, title.strip(), meeting_date, source_url)


def fetch_meeting_metadata(
    recording_id: int,
    api_key: str,
    *,
    hint_date: str | None = None,
    **list_options: Any,
) -> MeetingMetadata:
    """Look up one recording's title, date, and call URL from the meetings list.

    The API exposes no per-recording metadata endpoint, so this pages the meetings
    list. A hint date narrows the first pass to a few days instead of walking back
    through the whole account.

    The date filters apply to a meeting's creation time, while the date a
    transcript is filed under comes from its recording time. Those normally agree,
    but processing can lag by more than the window, so a narrowed pass that finds
    nothing falls back to an unfiltered scan rather than reporting a recording that
    plainly exists as missing.
    """
    validate_recording_id(recording_id)
    caller_filtered = "created_after" in list_options or "created_before" in list_options

    attempts: list[dict[str, str]] = []
    if hint_date and not caller_filtered:
        validate_meeting_date(hint_date)
        created_after, created_before = _date_window(hint_date)
        attempts.append({"created_after": created_after, "created_before": created_before})
    attempts.append({})

    for window in attempts:
        for item in iter_meetings(api_key, **{**list_options, **window}):
            if item.get("recording_id") == recording_id:
                return _meeting_metadata_of(recording_id, item)

    # Metadata lookup is not optional: --title/--date/--source-url are assertions
    # checked against it, not a way around it, so do not offer them as a remedy.
    raise FathomAPIError(
        f"recording {recording_id} was not found in the meetings list after scanning "
        f"up to {list_options.get('max_pages', DEFAULT_MAX_PAGES)} pages; confirm the "
        "recording ID, or raise --max-pages if the meeting is older than that"
    )


def reconcile_metadata(
    discovered: MeetingMetadata,
    *,
    title: str | None,
    meeting_date: str | None,
    source_url: str | None,
) -> MeetingMetadata:
    """Treat curator-supplied metadata as an assertion against what the API reports.

    A mismatched title or a date more than a timezone's slack away almost always
    means the wrong recording ID, so it fails closed instead of writing a plausible
    file under the wrong name.
    """
    if title is not None and title != discovered.title:
        raise TranscriptFormatError(
            f"title mismatch for recording {discovered.recording_id}: "
            f"API reports {discovered.title!r}, command line says {title!r}"
        )
    if source_url is not None and source_url != discovered.source_url:
        raise TranscriptFormatError(
            f"source URL mismatch for recording {discovered.recording_id}: "
            f"API reports {discovered.source_url}, command line says {source_url}"
        )

    resolved_date = discovered.meeting_date
    if meeting_date is not None:
        validate_meeting_date(meeting_date)
        drift = abs(date.fromisoformat(meeting_date) - date.fromisoformat(discovered.meeting_date))
        if drift > timedelta(days=1):
            raise TranscriptFormatError(
                f"date mismatch for recording {discovered.recording_id}: "
                f"API reports {discovered.meeting_date}, command line says {meeting_date}"
            )
        # The curator's local date wins: the API reports UTC, and a late-evening
        # meeting is filed under the day it happened, not the day UTC rolled over.
        resolved_date = meeting_date

    return discovered._replace(meeting_date=resolved_date)


def import_recording(
    recording_id: int,
    api_key: str,
    *,
    title: str | None = None,
    meeting_date: str | None = None,
    source_url: str | None = None,
    coalesce: bool = True,
    max_pages: int = DEFAULT_MAX_PAGES,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    **request_options: Any,
) -> ImportResult:
    """Fetch and render one recording without writing any files."""
    metadata = fetch_meeting_metadata(
        recording_id,
        api_key,
        hint_date=meeting_date,
        max_pages=max_pages,
        page_limit=page_limit,
        **request_options,
    )
    metadata = reconcile_metadata(
        metadata,
        title=title,
        meeting_date=meeting_date,
        source_url=source_url,
    )

    segments, raw_body = fetch_transcript_document(recording_id, api_key, **request_options)
    transcript = transcript_from_api_segments(segments, metadata.source_url, coalesce=coalesce)
    relative_path = package_path(
        metadata.title,
        metadata.meeting_date,
        metadata.recording_id,
        metadata.source_url,
    )
    rendered = render_markdown(
        metadata.title,
        metadata.meeting_date,
        metadata.recording_id,
        metadata.source_url,
        transcript,
    )
    return ImportResult(relative_path, rendered, raw_body)


def positive_integer(value: str) -> int:
    """Parse a positive integer for argparse."""
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def non_negative_float(value: str) -> float:
    """Parse a finite, non-negative float for argparse.

    float() accepts nan and inf, and a nan comparison is always false, so both would
    slip past a bare range check and reach time.sleep to raise there instead.
    """
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number") from error
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("must be a finite number")
    if parsed < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Fetches from the Fathom API by default, reading the key from the "
            f"{API_KEY_ENV} environment variable. Pass --input to re-render a stored "
            "raw transcript instead, which needs no network access or credentials."
        ),
    )
    parser.add_argument(
        "--recording-id",
        action="append",
        dest="recording_ids",
        type=positive_integer,
        metavar="ID",
        help="numeric Fathom recording ID; repeat to import several recordings",
    )
    parser.add_argument("--title", help="assert the meeting title the API reports (default: use it)")
    parser.add_argument(
        "--date",
        dest="meeting_date",
        help="meeting date in YYYY-MM-DD format; narrows API lookup and wins over the API's UTC date",
    )
    parser.add_argument("--source-url", help="assert the canonical https://fathom.video/calls/<call-id> URL")
    parser.add_argument(
        "--input",
        metavar="PATH",
        help=(
            "re-render a stored raw transcript instead of fetching; use - for standard "
            "input (requires --title/--date/--source-url and one --recording-id)"
        ),
    )
    parser.add_argument(
        "--input-format",
        choices=INPUT_FORMATS,
        default=None,
        help=(
            "--input layout: 'export' is Fathom's plain-text transcript download or a "
            "pasted copy ('0:00 - Speaker' headings, then text); 'lines' is canonical "
            "linked segment lines; 'auto' (default) picks 'lines' when the first line "
            "is a linked segment, else 'export'"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="write beneath this directory using the deterministic package path (default: standard output)",
    )
    parser.add_argument("--overwrite", action="store_true", help="replace an existing output file")
    parser.add_argument(
        "--no-coalesce",
        dest="coalesce",
        action="store_false",
        help=(
            "emit one line per API utterance (or export text line) instead of merging "
            "each speaker's turn into one line"
        ),
    )
    parser.add_argument(
        "--save-json",
        type=Path,
        help="also write the raw API transcript response here (a directory when importing several)",
    )
    parser.add_argument(
        "--min-interval",
        type=non_negative_float,
        default=DEFAULT_MIN_INTERVAL,
        help=(
            "seconds to wait between transcript fetches, since transcript endpoints are "
            f"rate limited (default: {DEFAULT_MIN_INTERVAL})"
        ),
    )
    parser.add_argument(
        "--max-pages",
        type=positive_integer,
        default=DEFAULT_MAX_PAGES,
        help=f"meetings-list pages to scan when resolving metadata (default: {DEFAULT_MAX_PAGES})",
    )
    return parser


def _render_from_file(args: argparse.Namespace) -> tuple[str, str]:
    """Re-render a stored raw transcript, keeping existing records reproducible."""
    missing = [
        name
        for name, value in (
            ("--title", args.title),
            ("--date", args.meeting_date),
            ("--source-url", args.source_url),
        )
        if not value
    ]
    if missing:
        raise TranscriptFormatError(f"--input requires {', '.join(missing)}")
    if not args.recording_ids or len(args.recording_ids) != 1:
        raise TranscriptFormatError("--input requires exactly one --recording-id")

    recording_id = args.recording_ids[0]
    raw_transcript = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
    input_format = args.input_format or "auto"
    if input_format == "auto":
        input_format = detect_input_format(raw_transcript)
    if input_format == "export":
        raw_transcript = transcript_from_export(raw_transcript, args.source_url, coalesce=args.coalesce)
    relative_path = package_path(args.title, args.meeting_date, recording_id, args.source_url)
    rendered = render_markdown(
        args.title,
        args.meeting_date,
        recording_id,
        args.source_url,
        raw_transcript,
    )
    return relative_path, rendered


def _json_target(save_json: Path | None, recording_id: int, batch: bool) -> Path | None:
    """Return where to persist a raw API response, if the caller asked for one."""
    if save_json is None:
        return None
    if not batch:
        return save_json
    return save_json / f"{recording_id}.json"


def _preflight_artifacts(artifacts: Sequence[PublicationArtifact], *, overwrite: bool) -> None:
    """Reject colliding, blocked, or protected destinations before publication."""
    planned: list[tuple[Path, Path]] = []
    for artifact in artifacts:
        normalized = artifact.target.expanduser().resolve(strict=False)
        for previous_normalized, previous_target in planned:
            if (
                normalized == previous_normalized
                or normalized in previous_normalized.parents
                or previous_normalized in normalized.parents
            ):
                raise TranscriptFormatError(
                    f"output destinations collide: {previous_target} and {artifact.target}"
                )
        planned.append((normalized, artifact.target))

    for artifact in artifacts:
        if artifact.target.exists() and artifact.target.is_dir():
            raise TranscriptFormatError(f"output destination is a directory: {artifact.target}")
        for parent in artifact.target.parents:
            if parent.exists() or parent.is_symlink():
                if not parent.is_dir():
                    raise TranscriptFormatError(
                        f"output destination parent is not a directory: {parent}"
                    )
                break

    if overwrite:
        return
    for artifact in artifacts:
        if artifact.target.exists() or artifact.target.is_symlink():
            raise TranscriptFormatError(
                f"refusing to overwrite existing {artifact.description}: {artifact.target}"
            )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the transcript renderer CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        outputs: list[tuple[int, str, str, bytes | None]]
        if args.input_format is not None and args.input is None:
            raise TranscriptFormatError("--input-format applies only with --input")
        if args.input is not None:
            relative_path, rendered = _render_from_file(args)
            outputs = [(args.recording_ids[0], relative_path, rendered, None)]
        else:
            if not args.recording_ids:
                raise TranscriptFormatError("either --recording-id or --input is required")
            batch = len(args.recording_ids) > 1
            if batch and not args.output_dir:
                raise TranscriptFormatError("multiple --recording-id values require --output-dir")
            if batch and any((args.title, args.meeting_date, args.source_url)):
                raise TranscriptFormatError(
                    "--title/--date/--source-url describe a single meeting; "
                    "omit them when importing several recordings"
                )

            api_key = resolve_api_key()
            outputs = []
            for position, recording_id in enumerate(args.recording_ids):
                if position and args.min_interval:
                    time.sleep(args.min_interval)
                imported = import_recording(
                    recording_id,
                    api_key,
                    title=args.title,
                    meeting_date=args.meeting_date,
                    source_url=args.source_url,
                    coalesce=args.coalesce,
                    max_pages=args.max_pages,
                )
                outputs.append(
                    (recording_id, imported.relative_path, imported.rendered, imported.raw_body)
                )

        batch = len(outputs) > 1
        artifacts: list[PublicationArtifact] = []
        for recording_id, relative_path, rendered, raw_body in outputs:
            if args.output_dir:
                artifacts.append(
                    PublicationArtifact(args.output_dir / relative_path, rendered, "transcript")
                )
            json_target = _json_target(args.save_json, recording_id, batch)
            if raw_body is not None and json_target is not None:
                artifacts.append(
                    PublicationArtifact(json_target, raw_body, "raw transcript response")
                )

        if args.input is not None and args.input != "-":
            source = Path(args.input).expanduser()
            for artifact in artifacts:
                if (artifact.target.resolve() == source.resolve()
                        or (artifact.target.exists() and artifact.target.samefile(source))):
                    raise TranscriptFormatError("output destination must not replace the input source")
        _preflight_artifacts(artifacts, overwrite=args.overwrite)
        for artifact in artifacts:
            write_new_file(
                artifact.target,
                artifact.content,
                overwrite=args.overwrite,
                description=artifact.description,
            )

        if args.output_dir:
            for _recording_id, relative_path, _rendered, _raw_body in outputs:
                print(relative_path)
        else:
            sys.stdout.write(outputs[0][2])
    except (OSError, TranscriptFormatError, FathomAPIError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
