#!/usr/bin/env python3
"""Build the summarizer's input table (excerpts) from a list of S3 objects.

Each line of the objects file is the s3:// URI of one file, or of a file kind: a prefix
plus a glob on the file name (s3://bucket/prefix/*.json, one sample is read). Blank lines
and lines starting with '#' are ignored. The source bucket is only read (GET/LIST).

Objects are read with your own AWS credentials (standard AWS chain); --anonymous reads a
public bucket without credentials. Columns follow ../schemas/input.schema.json; list-valued
cells hold JSON arrays. Handles md/txt/yaml/json/xml/log, csv/tsv, parquet, xlsx, docx,
pdf and vcf; other types are skipped and reported on stderr.

--excerpt-chars 0 and --pdf-pages 0 read documents whole (recommended for papers).
Signposts are read only from the separately approved --signposts file of exact URIs.
--parent-abstract adds a `parent` column: for a file in an article's folder (a supplement),
the title and abstract of that article from a JATS XML file in the same folder.
"""
import csv
import fnmatch
from functools import lru_cache
import html
import io
import json
import logging
import re
import sys
import zipfile


EXCERPT_CHARS = 8000      # cap on every excerpt (--excerpt-chars; 0 = no cap)
PDF_PAGES = 20            # pages read from a PDF (--pdf-pages; 0 = all)
PARENT = False            # --parent-abstract: add the `parent` column
TABLE_ROWS = 5            # data rows shown for tables
MENTION_CONTEXT = 200     # characters either side of a mention
MAX_MENTIONS = 5
MIN_TEXT = 20             # units with fewer non-space characters than this are skipped
ANCESTOR_LEVELS = 2       # signposts are read from the prefix and this many levels up (never the bucket root)
COLUMNS = ["unit_id", "prefix", "unit", "file_type", "members", "example_keys",
           "size_bytes", "excerpt_method", "excerpt", "mentions"]

s3 = None  # set in main()
APPROVED_SIGNPOSTS = frozenset()  # exact (bucket, key) pairs, opt-in per invocation


def split_uri(uri):
    bucket, _, key = uri[len("s3://"):].partition("/")
    return bucket, key


@lru_cache(maxsize=32)
def list_children(bucket, prefix):
    """Objects directly under prefix: [(key, size)]."""
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        out += [(o["Key"], o["Size"]) for o in page.get("Contents", [])]
    return out


def get(bucket, key, max_bytes=None):
    kwargs = {"Range": f"bytes=0-{max_bytes - 1}"} if max_bytes else {}
    return s3.get_object(Bucket=bucket, Key=key, **kwargs)["Body"].read()


def file_type(key):
    name = key.rsplit("/", 1)[-1].lower()
    return name.rsplit(".", 1)[-1] if "." in name else ""


def cap(text, method):
    if EXCERPT_CHARS and len(text) > EXCERPT_CHARS:
        return f"{method}; truncated to the first {EXCERPT_CHARS:,} of {len(text):,} characters", text[:EXCERPT_CHARS]
    return method, text


def table_text(header, rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def excerpt(bucket, key, size):
    """(method, text) for one object, or None if this file type isn't handled."""
    ft = file_type(key)
    if ft in {"md", "txt", "yaml", "yml", "json", "xml", "log"}:
        if not EXCERPT_CHARS or size <= EXCERPT_CHARS:
            return "full text", get(bucket, key).decode("utf-8", "replace")
        text = get(bucket, key, 4 * EXCERPT_CHARS).decode("utf-8", "replace")
        return f"first {EXCERPT_CHARS:,} characters of a {size:,}-byte file", text[:EXCERPT_CHARS]
    if ft in {"csv", "tsv"}:
        limit = 2_000_000
        data = get(bucket, key, limit if size > limit else None).decode("utf-8", "replace")
        rows = list(csv.reader(io.StringIO(data), delimiter="\t" if ft == "tsv" else ","))
        if size > limit:
            rows = rows[:-1]  # last row may be cut off
        n = f"{len(rows) - 1:,} data rows" if size <= limit else f"at least {len(rows) - 1:,} data rows (partial read)"
        return cap(table_text(rows[0], rows[1:1 + TABLE_ROWS]),
                   f"header and first {TABLE_ROWS} rows; {n}; {len(rows[0])} columns")
    if ft == "parquet" and size <= 50_000_000:
        import pyarrow.parquet as pq
        parquet = pq.ParquetFile(io.BytesIO(get(bucket, key)))
        schema = "\n".join(f"{f.name}: {f.type}" for f in parquet.schema_arrow)
        batch = next(parquet.iter_batches(batch_size=TABLE_ROWS), None)
        head = batch.to_pylist() if batch is not None else []
        columns = parquet.schema_arrow.names
        rows = [[r[c] for c in columns] for r in head]
        return cap(f"schema:\n{schema}\n\nfirst rows:\n" + table_text(columns, rows),
                   f"schema and first {TABLE_ROWS} rows; {parquet.metadata.num_rows:,} rows; {len(columns)} columns")
    if ft == "xlsx" and size <= 20_000_000:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(get(bucket, key)), read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets[:5]:
            rows = []
            for _, r in zip(range(8), ws.iter_rows(values_only=True)):
                r = ["" if v is None else v for v in r]
                while r and r[-1] == "":
                    r.pop()
                rows.append(r)
            size = f" ({ws.max_row} rows x {ws.max_column} columns)" if ws.max_row else ""
            parts.append(f"sheet {ws.title!r}{size}:\n" + table_text(rows[0] if rows else [], rows[1:]))
        return cap("\n".join(parts), f"first 8 rows of each of {min(len(wb.worksheets), 5)} of {len(wb.worksheets)} sheets")
    if ft == "docx" and size <= 30_000_000:
        z = zipfile.ZipFile(io.BytesIO(get(bucket, key)))
        x = z.read("word/document.xml").decode("utf-8", "replace")
        paras = [t for t in (html.unescape("".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p))).strip()
                             for p in re.findall(r"<w:p[ >].*?</w:p>", x, re.S)) if t]
        media = sum(1 for n in z.namelist() if n.startswith("word/media/"))
        return cap("\n".join(paras), f"document text, {len(paras)} paragraphs, {media} embedded images (not shown)")
    if ft == "pdf" and size <= 30_000_000:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(get(bucket, key)))
        pages = reader.pages[:PDF_PAGES] if PDF_PAGES else reader.pages
        text = "\n".join((p.extract_text() or "") for p in pages)
        return cap(text, f"text of the first {len(pages)} of {len(reader.pages)} pages" if len(pages) < len(reader.pages)
                   else f"text of all {len(reader.pages)} pages")
    if ft == "vcf":
        limit = 262_144
        raw = get(bucket, key, limit if size > limit else None)
        data = raw.decode("utf-8", "replace").splitlines()
        if size > len(raw) and not raw.endswith((b"\n", b"\r")):
            data = data[:-1]  # only a byte-range-truncated partial line
        def short(line):
            return line if len(line) <= 400 else f"{line[:400]} [{len(line) - 400:,} more characters]"
        meta, seen = [], {}
        for line in (line for line in data if line.startswith("##")):
            kind = re.split(r"[=<0-9-]", line[2:], maxsplit=1)[0]    # ##contig, ##GVCFBlock, ##FORMAT ...
            seen[kind] = seen.get(kind, 0) + 1
            if seen[kind] <= 3:
                meta.append(short(line))
        meta += [f"[{n - 3} more ##{k} lines omitted]" for k, n in seen.items() if n > 3]
        tail = "\n".join([line for line in data if line.startswith("#CHROM")] + [short(line) for line in data if not line.startswith("#")][:3])
        body = "\n".join(meta)
        if EXCERPT_CHARS:
            body = body[:max(0, EXCERPT_CHARS - len(tail) - 1)]
        return "VCF header (at most 3 lines of each type, long lines shortened), column line and first 3 records", f"{body}\n{tail}"
    return None


_signposts = {}


def signposts(bucket, prefix):
    """Text of README/Markdown and XML files directly under prefix: {name: text}."""
    if (bucket, prefix) not in _signposts:
        found = {}
        for source_bucket, key in sorted(APPROVED_SIGNPOSTS):
            folder, _, name = key.rpartition("/")
            folder = folder + "/" if folder else ""
            if source_bucket != bucket or folder != prefix:
                continue
            if name.lower().startswith("readme") or name.lower().endswith((".md", ".xml")):
                size = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
                if size <= 2_000_000:
                    found[key] = get(bucket, key).decode("utf-8", "replace")
        _signposts[(bucket, prefix)] = found
    return _signposts[(bucket, prefix)]


def mentions(bucket, prefix, term, self_key=None):
    """Passages in nearby signposts that name term, each prefixed with the signpost's key."""
    # The bucket root is skipped: listing it can mean millions of prefixes.
    parts = prefix.rstrip("/").split("/") if prefix else []
    ancestors = ["/".join(parts[:n]) + "/" for n in range(len(parts), 0, -1)][:ANCESTOR_LEVELS + 1]
    out = []
    for p in ancestors:
        for key, text in signposts(bucket, p).items():
            if key == self_key:
                continue
            for m in re.finditer(re.escape(term), text):
                window = text[max(0, m.start() - MENTION_CONTEXT):m.end() + MENTION_CONTEXT]
                if key.lower().endswith(".xml"):                      # strip markup, including tags cut by the window
                    window = re.sub(r"^[^<]*?>", "", window) if window.find(">") < window.find("<") else window
                    window = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"<[^>]*$", "", window)))
                window = re.sub(r"\s+", " ", window).strip()
                out.append(f"{key}: {window}")
                if len(out) >= MAX_MENTIONS:
                    return out
    return out


def parent_text(bucket, prefix, name):
    """Title and abstract of the JATS article in this folder, unless `name` is that article's own file."""
    import xml.etree.ElementTree as ET
    for key, text in signposts(bucket, prefix).items():
        if not key.lower().endswith(".xml") or "<abstract" not in text:
            continue
        stem = key.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if name.rsplit(".", 1)[0] == stem:
            return ""
        try:
            root = ET.fromstring(text.encode())
        except ET.ParseError:
            continue
        for el in root.iter():
            if isinstance(el.tag, str) and "}" in el.tag:
                el.tag = el.tag.split("}", 1)[1]
        def flat(el):
            return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""
        title = flat(root.find(".//article-meta/title-group/article-title"))
        abstract = root.find(".//article-meta/abstract")
        if abstract is None:
            continue
        parts = []
        secs = abstract.findall("./sec")
        for s in secs or [abstract]:
            label = flat(s.find("title")) if secs else ""
            body = " ".join(flat(p) for p in s.findall(".//p")) or flat(s)
            parts.append(f"{label}: {body}" if label else body)
        return f"Article title: {title}\nAbstract: " + " ".join(parts)
    return ""


def row_for(uri):
    bucket, key = split_uri(uri)
    prefix, _, name = key.rpartition("/")
    prefix = prefix + "/" if prefix else ""
    children = list_children(bucket, prefix)
    if any(c in name for c in "*?["):
        members = sorted((k, s) for k, s in children if fnmatch.fnmatchcase(k.rsplit("/", 1)[-1], name))
    else:
        members = [(k, s) for k, s in children if k == key]
    if not members:
        raise ValueError(f"no objects for {uri}")
    sample_key, sample_size = members[0]
    term = sample_key.rsplit("/", 1)[-1] if len(members) == 1 else prefix.rstrip("/").rsplit("/", 1)[-1]
    ex = excerpt(bucket, sample_key, sample_size)
    if ex is None:
        raise ValueError(f"file type not handled: {uri}")
    method, text = ex
    if len("".join(text.split())) < MIN_TEXT:
        raise ValueError(f"no readable text: {uri}")
    if len(members) > 1:
        method = f"one sample of {len(members):,} files; {method}"
    return {
        "unit_id": uri,
        "prefix": f"s3://{bucket}/{prefix}",
        "unit": name,
        "file_type": file_type(sample_key),
        "members": len(members),
        "example_keys": json.dumps([k.rsplit("/", 1)[-1] for k, _ in members[:3]]),
        "size_bytes": sample_size,
        "excerpt_method": method,
        "excerpt": text,
        "mentions": json.dumps(mentions(bucket, prefix, term, sample_key), ensure_ascii=False),
        **({"parent": parent_text(bucket, prefix, name)} if PARENT else {}),
    }


def main(argv=None):
    import argparse
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config
    global s3, EXCERPT_CHARS, PDF_PAGES, PARENT, APPROVED_SIGNPOSTS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("objects", help="text file, one s3:// URI or prefix/glob per line")
    ap.add_argument("out", help="inputs.csv to write")
    ap.add_argument("--excerpt-chars", type=int, default=EXCERPT_CHARS, help="cap per excerpt (0 = no cap; default %(default)s)")
    ap.add_argument("--pdf-pages", type=int, default=PDF_PAGES, help="PDF pages read (0 = all; default %(default)s)")
    ap.add_argument("--parent-abstract", action="store_true")
    ap.add_argument("--signposts", help="approved exact s3:// signpost URIs, one per line; none read by default")
    ap.add_argument("--anonymous", action="store_true", help="unsigned requests, for public buckets")
    ap.add_argument("--region", help="bucket region (default: your AWS config); read in-region to avoid egress")
    a = ap.parse_args(argv)
    if a.excerpt_chars < 0 or a.pdf_pages < 0:
        ap.error("excerpt-chars and pdf-pages must be nonnegative")
    list_children.cache_clear()
    _signposts.clear()
    APPROVED_SIGNPOSTS = frozenset()
    if a.signposts:
        with open(a.signposts) as fh:
            signpost_uris = [line.strip() for line in fh if line.strip() and not line.lstrip().startswith("#")]
        if any(not uri.startswith("s3://") or not split_uri(uri)[1] for uri in signpost_uris):
            ap.error("signposts must be exact s3://bucket/key URIs")
        APPROVED_SIGNPOSTS = frozenset(split_uri(uri) for uri in signpost_uris)
    EXCERPT_CHARS, PDF_PAGES, PARENT = a.excerpt_chars, a.pdf_pages, a.parent_abstract
    s3 = boto3.client("s3", region_name=a.region, config=Config(signature_version=UNSIGNED) if a.anonymous else None)
    objects_path, out_path = a.objects, a.out
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    uris = [line.strip() for line in open(objects_path) if line.strip() and not line.startswith("#")]
    failed = 0
    with open(out_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS + (["parent"] if PARENT else []))
        w.writeheader()
        for uri in uris:
            try:
                w.writerow(row_for(uri))
                print(f"ok    {uri}", file=sys.stderr)
            except ValueError as e:
                failed += 1
                print(f"skip  {e}", file=sys.stderr)
            except Exception as e:  # finish the inventory, but report an incomplete run
                failed += 1
                print(f"skip  {type(e).__name__} reading {uri}: {str(e)[:120]}", file=sys.stderr)

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
