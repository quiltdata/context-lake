#!/usr/bin/env python3
"""Survey an S3 prefix tree (read-only) and draft the scope for CHECKPOINT 1.

Lists every object under s3://bucket/prefix (or reads a saved listing), then reports per
prefix (to --depth levels): object count, bytes, file types, and "file kinds" (names that
differ only by equal-width numeric IDs, e.g. cell001.tiff and cell002.tiff = one kind). Estimates how many
excerpts the walk will read (one per distinct file or kind sample) and a model cost range
from public list prices (model_rates.json) under stated token assumptions.

Writes a Markdown scope (stdout or --out) and, with --objects, an objects file for
make_inputs.py (one line per distinct file or kind glob, readable types only).

Examples:
  python3 survey.py s3://my-bucket/projects/ --depth 2 --model us.anthropic.claude-sonnet-5-5 \
      --out scope.md --objects objects.txt
  python3 survey.py --listing listing.tsv s3://my-bucket/   # offline: "key<TAB>size" per line
"""
import argparse
import glob
import sys
from collections import Counter, defaultdict

READABLE = {"md", "txt", "yaml", "yml", "json", "xml", "log", "csv", "tsv", "parquet", "xlsx", "docx", "pdf", "vcf"}
TOKENS_PER_EXCERPT = (2500, 8000)   # low/high input tokens per excerpt (8,000-char cap ~ 2,000-2,500 tokens)
OUTPUT_TOKENS_PER_ROW = 400


def validate_line(value):
    if any(char in value for char in "\r\n"):
        raise ValueError("S3 keys and URIs containing CR/LF cannot use the line-delimited objects format")
    return value


def split_uri(uri):
    validate_line(uri)
    if not uri.startswith("s3://"):
        sys.exit("expected s3://bucket/prefix")
    bucket, _, prefix = uri[5:].partition("/")
    return bucket, prefix


def list_s3(bucket, prefix, region=None, anonymous=False):
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    s3 = boto3.client("s3", region_name=region, config=Config(signature_version=UNSIGNED) if anonymous else None)
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            yield o["Key"], o["Size"]


def ext(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def kind_of(name):
    """Glob for equal-width numeric IDs; never match a file with no digits."""
    escaped = {'[': '[[]', '*': '[*]', '?': '[?]'}
    return ''.join('[0-9]' if char in '0123456789' else escaped.get(char, char)
                   for char in name)


def survey(objects, prefix, depth):
    validate_line(prefix)
    by_prefix = defaultdict(lambda: {"n": 0, "bytes": 0, "types": Counter()})
    folders = defaultdict(list)  # direct parent folder -> [(name, size)]
    for key, size in objects:
        validate_line(key)
        if not key.startswith(prefix):
            continue
        if key.endswith("/"):
            continue
        rel = key[len(prefix):]
        parts = rel.split("/")
        top = prefix + "/".join(parts[:min(depth, len(parts) - 1)]) + ("/" if len(parts) > 1 and depth else "")
        p = by_prefix[top]
        p["n"] += 1
        p["bytes"] += size
        p["types"][ext(parts[-1]) or "(none)"] += 1
        folder, _, name = key.rpartition("/")
        folders[folder + "/" if folder else ""].append((name, size))
    units = []  # (folder, glob or name, members, readable)
    for folder, files in folders.items():
        kinds = defaultdict(list)
        for name, size in files:
            # A filename prefix is approved literally, including its digits.
            # Exact entries avoid broadening it when extraction lists the parent.
            pattern = glob.escape(name) if prefix and not prefix.endswith("/") else kind_of(name)
            kinds[pattern].append(name)
        for k, names in kinds.items():
            unit = k if len(names) > 1 else glob.escape(names[0])
            units.append((folder, unit, len(names), ext(names[0]) in READABLE))
    return by_prefix, units


def human(n):
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PiB"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("uri", help="s3://bucket/prefix to survey (read-only)")
    ap.add_argument("--depth", type=int, default=1, help="prefix levels to report (default 1)")
    ap.add_argument("--listing", help="offline: a file of 'key<TAB>size' lines instead of listing S3")
    ap.add_argument("--region")
    ap.add_argument("--anonymous", action="store_true", help="unsigned requests, for public buckets")
    ap.add_argument("--model", help="summarizer model id for the cost estimate (model_rates.json)")
    ap.add_argument("--out", help="write the Markdown scope here (default stdout)")
    ap.add_argument("--objects", help="write an objects file for make_inputs.py")
    a = ap.parse_args(argv)
    try:
        bucket, prefix = split_uri(a.uri)
    except ValueError as error:
        ap.error(str(error))
    if a.listing:
        objs = [(k, int(s)) for k, s in (line.rstrip("\n").split("\t") for line in open(a.listing) if line.strip())]
    else:
        objs = list(list_s3(bucket, prefix, a.region, a.anonymous))
    objs = [(k, s) for k, s in objs if k.startswith(prefix) and not k.startswith(".quilt/")]
    try:
        by_prefix, units = survey(objs, prefix, a.depth)
    except ValueError as error:
        ap.error(str(error))
    readable = [u for u in units if u[3]]
    lines = [f"# Scope draft: s3://{bucket}/{prefix}", "",
             f"{len(objs):,} objects, {human(sum(s for _, s in objs))}; {len(units):,} distinct files/kinds, "
             f"{len(readable):,} of them in readable types (one excerpt each).", "",
             "| Prefix | Objects | Size | Top types |", "|---|---:|---:|---|"]
    for p, v in sorted(by_prefix.items()):
        lines.append(f"| `{p or '/'}` | {v['n']:,} | {human(v['bytes'])} | "
                     + ", ".join(f"{t} {n}" for t, n in v["types"].most_common(4)) + " |")
    skipped = Counter(ext(u[1]) or "(none)" for u in units if not u[3])
    if skipped:
        lines += ["", "Not excerpted (type not handled; listed by name only): "
                  + ", ".join(f"{t} {n}" for t, n in skipped.most_common(8))]
    if a.model:
        from .rates import usd
        lo = usd(a.model, len(readable) * TOKENS_PER_EXCERPT[0], len(readable) * OUTPUT_TOKENS_PER_ROW)
        hi = usd(a.model, len(readable) * TOKENS_PER_EXCERPT[1], len(readable) * OUTPUT_TOKENS_PER_ROW)
        lines += ["", f"Summarizer estimate ({a.model}, public list price): "
                  + (f"${lo:,.2f}-${hi:,.2f}" if lo is not None else "no public rate in model_rates.json")
                  + f" for {len(readable):,} excerpts at {TOKENS_PER_EXCERPT[0]:,}-{TOKENS_PER_EXCERPT[1]:,} input and "
                  f"{OUTPUT_TOKENS_PER_ROW} output tokens each; prefix assessments by the frontier model are extra "
                  f"(~1 call per prefix, {len({u[0] for u in units}):,} prefixes)."]
    lines += ["", "CHECKPOINT 1: approve prefixes, target bucket/namespace and budget before any model call."]
    object_uris = [validate_line(f"s3://{bucket}/{folder}{unit}")
                   for folder, unit, n, ok in sorted(readable)]
    text = "\n".join(lines) + "\n"
    (open(a.out, "w").write(text) if a.out else sys.stdout.write(text))
    if a.objects:
        with open(a.objects, "w") as fh:
            fh.write(f"# from survey.py {a.uri}\n")
            for uri in object_uris:
                fh.write(uri + "\n")


if __name__ == "__main__":
    main()
