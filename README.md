# Quilt Context Lake

Deterministic context automation in Python: collect source material, normalize it,
index it, reconcile changes and publish approved snapshots. Agents can invoke the
same commands as people. Model enrichment is optional and explicit.

## Install

Python 3.10 or newer:

```bash
uv tool install quilt-context-lake
qcl --help
```

Until the first PyPI release, install from GitHub instead:

```bash
uv tool install 'git+https://github.com/quiltdata/context-lake.git'
```

Install `quilt-context-lake`; type `qcl`. Installing does not add a skill or start a
model. The Python import is `context_lake`.

Optional dependencies are separated by operation:

```bash
uv tool install 'quilt-context-lake[s3,formats,quilt,models]'
```

Use only the extras you need: `s3` for AWS inventory/extraction, `formats` for
PDF/Parquet/XLSX extraction, `quilt` for Quilt publication, `models` for Bedrock
adapters. Local email conversion, Fathom export, transcript indexing and snapshot
reconciliation use the standard library. Quote/schema validation uses jsonschema.

## Automate context updates

Start with a directory of source documents or normalized connector exports:

```bash
# Deterministic plan; no writes, credentials or model calls.
qcl sync ./source ./lake --source-id team-documents

# Write an immutable content-addressed revision and promote CURRENT.json.
qcl sync ./source ./lake --source-id team-documents --apply
```

The plan reports added, changed and removed paths. Repeating the same input
produces the same revision hash. Modified/deleted files appear in the new snapshot;
previous snapshots retain the originals. No modification times or current dates
enter the content manifest. Empty directories are not materialized.

`SOURCE.json` marks an owned output directory so an interrupted first apply can
be retried safely. Snapshots live in `lake/revisions/<sha256>/files/`, with a provenance manifest
containing source identity, logical paths, sizes and SHA-256 hashes. `CURRENT.json`
is atomically promoted after copying and checking the new revision. A failed copy
leaves the previous pointer intact. An unchanged verified snapshot is reused
without allocating a staging copy or rewriting its pointer. The source is never written. Source/output
trees must be separate; symlinks and unmanaged nonempty destinations are rejected.
Use one writer per output directory; multi-process coordination is not implemented.
Storage cleanup and retention policies are not yet automated.

## Source adapters and operations

| Command | Operation | Network / model use |
|---|---|---|
| `email` | Saved Gmail/API or connector thread JSON → Markdown; `--list` for list archives | Offline; no model |
| `fathom` | Approved recording IDs or saved transcript → Markdown | API only when IDs are fetched; no model |
| `index` | Transcript metadata, terms and themes → INDEX.md | Offline; no model |
| `backfill` | Repair explicitly selected empty transcript placeholders | Explicit Fathom reads; no model |
| `survey` | S3 inventory, file-kind grouping and optional price estimate | S3 reads; no model |
| `extract` | Source excerpts and bounded table samples → CSV | S3 reads; no model |
| `sync` | File-hash plan and immutable snapshots | Offline; no model |
| `publish` | Approved local/S3 CSV plan → Quilt packages | Dry run by default; `--yes` writes |
| `verify` | Check enriched row schemas and exact source quotes | Offline; no model |
| `enrich` | Optional model descriptions for extracted rows | Explicit billed model calls |

Each command has `--help`. Examples:

```bash
qcl email --input /work/threads.json --output-dir /work/gmail
qcl fathom --recording-id 123 --output-dir /work/fathom
qcl index --package-dir /work/fathom --output-dir /work/fathom
qcl survey s3://YOUR-BUCKET/approved/ --objects /work/objects.txt
qcl extract /work/objects.txt /work/inputs.csv
qcl publish /work/plan.csv --registry s3://YOUR-TARGET
```

Fathom accepts downloaded transcript text offline. Fetching recording IDs uses
`FATHOM_API_KEY` from the environment. AWS operations use the standard
credential chain. Offline converters never load credential files. Approve source
scope and publication targets before running network operations. An agent skill is
an optional interface; the package owns behavior, validation and automation.

### Fidelity and current boundaries

Email conversion takes full Gmail thread objects (single, array, or `threads`
envelope), including Gmail connector flattened messages (`sender`,
`toRecipients`/`ccRecipients`, `plaintextBody`/`htmlBody`), not snippets or raw
RFC email. It preserves supplied message IDs, routing/reply/date headers and
plain text. Quoted reply history and Google Groups footers are stripped by
default (`--keep-quotes` to keep them). `--list ADDRESS` keeps only messages
addressed to that list (exact mailbox match) and, with `--overwrite`, removes
stale thread files so private off-list mail cannot remain in the output
directory. HTML-only messages yield explicitly marked derived text. Attachments
are listed, not fetched; exports cannot prove completeness beyond the messages
supplied. Explicit truncation markers fail.

Fathom import and indexing preserve timestamped source links and support quoted
meeting titles. Backfill is scoped to an explicit package directory, with preview,
pacing and destination checks. Source objects are never overwritten by import.

Sync currently reconciles local files. It does not automatically poll connectors,
remove quoted email history or extract attachments. Run source adapters first,
then sync their outputs. Scheduling belongs to the caller (CI or a scheduler).
Enrichment is never a prerequisite for a context snapshot.

## Python API

```python
from context_lake.sync import plan, apply
from context_lake.email_threads_to_markdown import convert

manifest, changes = plan("./source", "./lake", source_id="team-documents")
# Inspect changes, then apply explicitly:
result = apply("./source", "./lake", source_id="team-documents")
```

## Development

```bash
uv venv
. .venv/bin/activate
uv pip install -e '.[dev,s3,formats,quilt,models]'
pytest -q
ruff check src tests
uv build
tests/wheel_smoke.sh
```

CI tests Python 3.10/3.13, builds wheel/sdist, installs the wheel in an isolated
core-only environment, and runs a deterministic workflow outside the checkout.

## Releasing

Set the same version in `pyproject.toml` and `src/context_lake/__init__.py`, merge
to `main`, then push a matching tag:

```bash
git tag v0.1.0
git push origin v0.1.0
```

The release workflow checks that the tag matches both versions, builds and tests
the wheel, and publishes to PyPI with `uv publish` through trusted publishing in
the `pypi` environment. No API token is stored in GitHub.
Tests use synthetic data and emulated S3; no live credentials or model calls.
MIT licensed. Interfaces are preview quality; provenance/fidelity tests are
retained while the package API evolves.

Extraction limits apply to complete output excerpts, including VCF column headers.
DOCX XML has a separate 30 MB expanded-member limit and bounded read; oversize
members fail rather than expanding unbounded compressed input. Transcript and
index writes refuse symbolic/hard-linked destinations. Publication logs must be
outside every local source directory and must not alias a plan/README input,
so repeated publishes cannot ingest logs or modify approved inputs.
