#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Derive a deterministic Markdown index from a package of Fathom transcripts."""

import argparse
import json
import math
import os
import re
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

FRONT_MATTER_RE = re.compile(
    r"\A---\n"
    r'title: (?P<title>"(?:[^"\\]|\\.)*")\n'
    r"date: (?P<date>\d{4}-\d{2}-\d{2})\n"
    r"recording_id: (?P<recording_id>[1-9][0-9]*)\n"
    r"source_url: \"(?P<source_url>https://fathom\.video/calls/[1-9][0-9]*)\"\n"
    r"---\n"
)
SEGMENT_RE = re.compile(
    r"^\[(?:[0-9]+:)?[0-9]{2}:[0-9]{2}\]"
    r"\(https://fathom\.video/calls/[1-9][0-9]*\?timestamp=[0-9]+(?:\.[0-9]+)?\) "
    r"(?P<speaker>.+?): (?P<text>.*)$"
)
WORD_RE = re.compile(r"[a-z][a-z'\-]*")

# Function words plus the conversational filler that dominates spoken transcripts.
# Without the second group, frequency ranking returns "thing", "kind", "really".
STOP_WORDS = frozenset(
    """
    a able about above actually after again against ago all almost already also always am an
    and any anyone anything anyway are aren't around as at back bad basically be because been
    before being below between bit both but by called can can't cannot come comes coming
    completely could couldn't couple course day definitely did didn't different do does
    doesn't doing don't done down during each easy either else enough especially even ever
    every everybody everyone everything everywhere exactly fact far feel feels felt few figure
    find fine first for four from full further get gets getting give given go goes going gone
    gonna good got gotta great guess guy guys had hadn't happen happened happening hard has
    hasn't have haven't having he he'd he'll he's hear heard help her here here's hers herself
    high him himself his hmm hold hour hours how how's huge huh i i'd i'll i'm i've idea if in
    inside instead interesting into is isn't it it's its itself just keep kind kinda knew know
    known lately least less let let's like liked likes lit literally little long look looked
    looking looks lot lots made make makes making many matter may maybe me mean means meant
    might mind minute minutes mm moment months more most move much mustn't my myself need
    needs never new next nice no nope nor not nothing now number obviously of off often oh
    okay old on once one ones only oops open or other others ought our ours ourselves out over
    own part particular people perhaps person pick place point pretty probably problem put
    question quite rather real really reason remember right run said same saw say saying says
    second see seeing seem seems seen sense set several shall shan't she she'd she'll she's
    short should shouldn't show side similar simply since sit six small so some somebody
    somehow someone something sometimes somewhere soon sorry sort sounds speak start started
    starting still stop stuff such suppose supposed sure take taken takes taking talk talked
    talking talks tell tells ten than thank thanks that that'd that's the their theirs them
    themselves then there there'd there's these they they'd they'll they're they've thing
    things think thinking thinks third this those thought three through thus time times to
    today together told too took top total totally trying turn two uh um under understand
    until up us use used uses using usually versus very wait want wanted wants was wasn't
    watch way ways we we'd we'll we're we've week weeks well went were weren't what what's
    whatever when when's where where's whether which while who who's whoa whole whom why why's
    will with won't wonder word words work worked working world would wouldn't wow write wrong
    yea yeah year years yep yes yet you you'd you'll you're you've your yours yourself
    yourselves
    """.split()
)


class TranscriptIndexError(ValueError):
    """Report a transcripts directory that cannot be indexed."""


class Transcript(NamedTuple):
    """One parsed transcript and the term counts derived from its speech."""

    relative_path: str
    title: str
    meeting_date: str
    recording_id: int
    source_url: str
    segment_count: int
    word_count: int
    terms: Counter[tuple[str, ...]]


class Corpus(NamedTuple):
    """A set of transcripts plus the document frequency of every term in them."""

    transcripts: tuple[Transcript, ...]
    document_frequency: Counter[tuple[str, ...]]


def parse_front_matter(text: str, source: str) -> tuple[dict[str, str], str]:
    """Split a rendered transcript into its front matter fields and body."""
    match = FRONT_MATTER_RE.match(text)
    if not match:
        raise TranscriptIndexError(f"{source}: missing or malformed front matter")
    try:
        date.fromisoformat(match.group("date"))
    except ValueError as error:
        raise TranscriptIndexError(f"{source}: invalid meeting date") from error
    fields = match.groupdict()
    try:
        title = json.loads(fields["title"])
    except json.JSONDecodeError as error:
        raise TranscriptIndexError(f"{source}: invalid JSON-quoted title") from error
    if not title or title != title.strip() or title.splitlines() != [title]:
        raise TranscriptIndexError(f"{source}: title must be nonempty, unpadded and single-line")
    fields["title"] = title
    return fields, text[match.end() :]


def parse_segments(body: str, source: str) -> tuple[tuple[str, str], ...]:
    """Extract (speaker, text) pairs from the timestamped segments of a body."""
    segments: list[tuple[str, str]] = []
    for line in body.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = SEGMENT_RE.fullmatch(line)
        if not match:
            raise TranscriptIndexError(f"{source}: line is not a transcript segment: {line[:60]!r}")
        segments.append((match.group("speaker").strip(), match.group("text")))
    if not segments:
        raise TranscriptIndexError(f"{source}: no transcript segments found")
    return tuple(segments)


def content_tokens(text: str, stop_words: frozenset[str]) -> list[list[str]]:
    """Split text into runs of content words, broken wherever a stop word appears."""
    runs: list[list[str]] = []
    run: list[str] = []
    for word in WORD_RE.findall(text.casefold()):
        token = word.strip("'-")
        if token.endswith("'s"):
            # Fold the possessive so "state's forest" and "state forest" are one term.
            token = token[:-2]
        if len(token) < 3 or token in stop_words:
            # A stop word ends the current run so phrases never span filler.
            if run:
                runs.append(run)
                run = []
            continue
        run.append(token)
    if run:
        runs.append(run)
    return runs


def singular_forms(vocabulary: Counter[str]) -> dict[str, str]:
    """Map plural tokens to their singular form, but only where the singular occurs.

    Requiring the singular to be attested in the corpus keeps this safe without a
    stemmer: "blankets" folds into "blanket", while "bits" and "physics" are left
    alone because "bit" outranks nothing and "physic" never appears.
    """
    mapping: dict[str, str] = {}
    for token in vocabulary:
        for suffix, stem in (("es", token[:-2]), ("s", token[:-1])):
            if token.endswith(suffix) and len(stem) >= 3 and vocabulary[stem] >= vocabulary[token]:
                mapping[token] = stem
                break
    return mapping


def extract_terms(
    runs: Iterable[Sequence[str]], normalize: dict[str, str], max_phrase: int
) -> Counter[tuple[str, ...]]:
    """Count content-word n-grams within each run, folding plurals to singulars."""
    terms: Counter[tuple[str, ...]] = Counter()
    for run in runs:
        folded = [normalize.get(token, token) for token in run]
        for size in range(1, max_phrase + 1):
            for start in range(len(folded) - size + 1):
                gram = tuple(folded[start : start + size])
                # Speech stutters ("god god", "maybe maybe") are artifacts, not terms.
                if size > 1 and len(set(gram)) == 1:
                    continue
                terms[gram] += 1
    return terms


def metadata_stop_words(title: str, segments: Iterable[tuple[str, str]]) -> frozenset[str]:
    """Derive stop words from the meeting title and speaker labels.

    Both name the container rather than the conversation. Speaker labels alone are
    not enough: a transcript labelled "Alex Example" still says "Alex" in the
    body, and the recurring title supplies words every transcript shares.
    """
    words: set[str] = set(WORD_RE.findall(title.casefold()))
    for speaker, _text in segments:
        words.update(WORD_RE.findall(speaker.casefold()))
    return frozenset(words)


def load_corpus(transcripts_dir: Path, max_phrase: int, link_base: Path | None = None) -> Corpus:
    """Parse every transcript in a directory and tabulate corpus-wide term counts."""
    link_base = link_base if link_base is not None else transcripts_dir.parent
    paths = sorted(transcripts_dir.glob("*.md"))
    if not paths:
        raise TranscriptIndexError(f"no transcripts found in {transcripts_dir}")

    parsed: list[tuple[Path, dict[str, str], tuple[tuple[str, str], ...]]] = []
    names: set[str] = set()
    for path in paths:
        fields, body = parse_front_matter(path.read_text(encoding="utf-8"), path.name)
        segments = parse_segments(body, path.name)
        names |= metadata_stop_words(fields["title"], segments)
        parsed.append((path, fields, segments))

    # Names are collected across the whole corpus before any term counting, so a
    # name is suppressed everywhere and not only where that person spoke.
    stop_words = STOP_WORDS | names

    runs_by_path: dict[Path, list[list[str]]] = {}
    vocabulary: Counter[str] = Counter()
    for path, _fields, segments in parsed:
        runs = [run for _speaker, text in segments for run in content_tokens(text, stop_words)]
        runs_by_path[path] = runs
        vocabulary.update(token for run in runs for token in run)
    normalize = singular_forms(vocabulary)

    transcripts: list[Transcript] = []
    document_frequency: Counter[tuple[str, ...]] = Counter()
    for path, fields, segments in parsed:
        speech = [text for _speaker, text in segments]
        terms = extract_terms(runs_by_path[path], normalize, max_phrase)
        document_frequency.update(terms.keys())
        try:
            relative_link = Path(os.path.relpath(path.resolve(), link_base.resolve())).as_posix()
        except ValueError as error:
            raise TranscriptIndexError("index and transcript paths must share a filesystem drive") from error
        transcripts.append(
            Transcript(
                relative_path=quote(relative_link, safe="/._-~"),
                title=fields["title"],
                meeting_date=fields["date"],
                recording_id=int(fields["recording_id"]),
                source_url=fields["source_url"],
                segment_count=len(segments),
                word_count=sum(len(text.split()) for text in speech),
                terms=terms,
            )
        )
    return Corpus(tuple(transcripts), document_frequency)


def subsumed(candidate: tuple[str, ...], selected: Iterable[tuple[str, ...]]) -> bool:
    """Report whether a phrase is a contiguous fragment of one already selected."""
    for chosen in selected:
        if len(candidate) >= len(chosen):
            continue
        span = len(candidate)
        if any(chosen[start : start + span] == candidate for start in range(len(chosen) - span + 1)):
            return True
    return False


def distinctive_terms(
    transcript: Transcript, corpus: Corpus, *, limit: int, min_count: int, max_document_ratio: float
) -> tuple[tuple[str, ...], ...]:
    """Rank a transcript's terms by TF-IDF against the rest of the corpus."""
    total = sum(transcript.terms.values()) or 1
    document_count = len(corpus.transcripts)
    ceiling = max(1.0, max_document_ratio * document_count)

    scored: list[tuple[float, tuple[str, ...]]] = []
    for term, count in transcript.terms.items():
        frequency = corpus.document_frequency[term]
        if count < min_count or frequency > ceiling:
            continue
        # Sublinear term frequency keeps a repeated aside from outranking a real thread.
        weight = (1.0 + math.log(count)) / total * math.log(document_count / frequency)
        # Prefer phrases: a multi-word term identifies a topic far better than a bare noun.
        scored.append((weight * len(term), term))

    # Sorting on the negated score with the term as tiebreaker makes output byte-stable.
    scored.sort(key=lambda item: (-item[0], item[1]))

    selected: list[tuple[str, ...]] = []
    for _weight, term in scored:
        if subsumed(term, selected):
            continue
        selected.append(term)
        if len(selected) == limit:
            break
    return tuple(selected)


def format_phrase(term: tuple[str, ...]) -> str:
    """Render an n-gram as display text."""
    return " ".join(term)


def build_theme_index(
    selections: Sequence[tuple[Transcript, tuple[tuple[str, ...], ...]]],
    min_documents: int,
    min_words: int,
) -> tuple[tuple[str, tuple[Transcript, ...]], ...]:
    """Group the terms that are distinctive in several transcripts into themes.

    The word floor exists because a bare noun is ambiguous out of context while a
    phrase names its own subject. Single words stay in the per-transcript lists,
    where the surrounding terms disambiguate them.
    """
    occurrences: dict[tuple[str, ...], list[Transcript]] = {}
    for transcript, terms in selections:
        for term in terms:
            if len(term) >= min_words:
                occurrences.setdefault(term, []).append(transcript)

    themes = [
        (format_phrase(term), tuple(sorted(found, key=lambda item: item.meeting_date)))
        for term, found in occurrences.items()
        if len(found) >= min_documents
    ]
    themes.sort()
    return tuple(themes)


def render_index(
    corpus: Corpus,
    *,
    top: int,
    theme_pool: int,
    min_theme_documents: int,
    min_theme_words: int,
    min_count: int,
) -> str:
    """Render the Markdown index for a parsed corpus."""
    ordered = sorted(corpus.transcripts, key=lambda item: (item.meeting_date, item.recording_id))

    # Themes are drawn from a deeper pool than the per-transcript display cut: a
    # thread that ranks 20th in three conversations is exactly what an index should
    # surface, but it would never appear in a top-8 list.
    ranked = [
        (
            transcript,
            distinctive_terms(
                transcript,
                corpus,
                limit=max(top, theme_pool),
                min_count=min_count,
                max_document_ratio=0.5,
            ),
        )
        for transcript in ordered
    ]
    selections = [(transcript, terms[:top]) for transcript, terms in ranked]
    themes = build_theme_index(ranked, min_theme_documents, min_theme_words)

    segments = sum(item.segment_count for item in ordered)
    words = sum(item.word_count for item in ordered)

    lines = [
        "# Index",
        "",
        "Generated by `transcript_index.py`. Do not edit",
        "by hand; re-run the tool instead.",
        "",
        f"**{len(ordered)} transcripts** \u00b7 {ordered[0].meeting_date} \u2192 "
        f"{ordered[-1].meeting_date} \u00b7 {segments:,} segments \u00b7 {words:,} words",
        "",
        "Terms below are *distinctive*, not frequent: each is weighted by how far it",
        "stands out against the rest of the corpus, so shared vocabulary and filler",
        "fall away and the subject of a given conversation remains.",
        "",
        "## Themes",
        "",
    ]

    if themes:
        lines += [
            f"Terms distinctive in at least {min_theme_documents} conversations, alphabetical.",
            "",
        ]
        for phrase, found in themes:
            dates = ", ".join(f"[{item.meeting_date}]({item.relative_path})" for item in found)
            lines.append(f"- **{phrase}** \u2014 {dates}")
    else:
        lines.append("No term was distinctive in enough conversations to form a theme.")

    lines += ["", "## Transcripts", "", "Most recent first.", ""]

    for transcript, terms in reversed(selections):
        lines += [
            f"### {transcript.meeting_date} \u00b7 {transcript.title}",
            "",
            f"[Transcript]({transcript.relative_path})"
            f" \u00b7 [Fathom]({transcript.source_url})"
            f" \u00b7 {transcript.segment_count} segments"
            f" \u00b7 {transcript.word_count:,} words",
            "",
            " \u00b7 ".join(format_phrase(term) for term in terms) or "*No distinctive terms.*",
            "",
        ]

    return "\n".join(lines).rstrip("\n") + "\n"


def write_index(output_dir: Path, content: str, *, overwrite: bool) -> Path:
    """Write INDEX.md beneath an output root without silent replacement."""
    target = output_dir / "INDEX.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    if overwrite:
        target.write_text(content, encoding="utf-8", newline="\n")
        return target
    try:
        with target.open("x", encoding="utf-8", newline="\n") as output_file:
            output_file.write(content)
    except FileExistsError as error:
        raise TranscriptIndexError(f"refusing to overwrite existing index: {target}") from error
    return target


def positive_integer(value: str) -> int:
    """Parse a positive integer for argparse."""
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--package-dir",
        required=True,
        type=Path,
        help="transcript package root, containing a transcripts/ directory",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="write INDEX.md here with links relative to this directory (stdout defaults to package-root links)",
    )
    parser.add_argument("--overwrite", action="store_true", help="replace an existing INDEX.md")
    parser.add_argument(
        "--top",
        type=positive_integer,
        default=8,
        help="distinctive terms listed per transcript (default: 8)",
    )
    parser.add_argument(
        "--theme-pool",
        type=positive_integer,
        default=40,
        help="ranked terms per transcript considered when building themes (default: 40)",
    )
    parser.add_argument(
        "--min-theme-transcripts",
        type=positive_integer,
        default=2,
        help="conversations a term must be distinctive in to become a theme (default: 2)",
    )
    parser.add_argument(
        "--min-theme-words",
        type=positive_integer,
        default=2,
        help="shortest phrase admitted to the theme index, in words (default: 2)",
    )
    parser.add_argument(
        "--min-count",
        type=positive_integer,
        default=3,
        help="times a term must occur in a transcript to be eligible (default: 3)",
    )
    parser.add_argument(
        "--max-phrase",
        type=positive_integer,
        default=3,
        help="longest phrase considered, in words (default: 3)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the transcript index CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        transcripts_dir = args.package_dir / "transcripts"
        if not transcripts_dir.is_dir():
            raise TranscriptIndexError(f"no transcripts directory in {args.package_dir}")
        corpus = load_corpus(transcripts_dir, args.max_phrase, args.output_dir)
        rendered = render_index(
            corpus,
            top=args.top,
            theme_pool=args.theme_pool,
            min_theme_documents=args.min_theme_transcripts,
            min_theme_words=args.min_theme_words,
            min_count=args.min_count,
        )
        if args.output_dir:
            write_index(args.output_dir, rendered, overwrite=args.overwrite)
            print("INDEX.md")
        else:
            sys.stdout.write(rendered)
    except (OSError, TranscriptIndexError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
