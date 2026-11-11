# AGENTS.md

## Running qcl locally

`qcl` is not installed globally. Run it from the repo root with `uv run`, which
uses the project's `.venv` and the editable source in `src/context_lake`:

```sh
uv run qcl --help
uv run qcl --version
uv run qcl <command> --help
```

Core commands, including `gmail`, `email`, `fathom`, `sync`, and `index`, need
only the base dependencies. Commands that touch S3, Quilt, document formats, or
Bedrock need optional extras. Add `--extra <name>` (`s3`, `formats`, `quilt`,
`models`), or `--all-extras` to get everything:

```sh
uv run --extra s3 qcl survey --help
uv run --all-extras qcl publish --help
```

Some commands are interactive or open a browser (for example
`qcl gmail login`). Let the user run those in their own terminal.

## Gmail

```sh
uv run qcl gmail setup    # prints the one-time OAuth setup checklist
uv run qcl gmail login --client-secrets ~/Downloads/client_secret_XXXX.json
```

Credentials are written to `~/.config/qcl/gmail.json`.

## Checks

These match CI (`.github/workflows/test.yml`):

```sh
uv run --all-extras ruff check src tests
uv run --all-extras pytest -q
uv build
tests/wheel_smoke.sh
```
