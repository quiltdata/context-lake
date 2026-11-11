# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `qcl gmail` fetches threads matching a Gmail search through the Gmail API and
  exports them with the same converter as `qcl email`. `qcl gmail login` runs a
  one-time, read-only OAuth sign-in in the browser. When no authorization is
  configured, or Google has revoked it, the command explains the Google Cloud
  setup, links to Google's documentation, and in a terminal offers to open the
  relevant console page.
- `qcl schedule` installs, lists, starts, and removes macOS launchd jobs for
  qcl commands or any other program, for example
  `qcl schedule add gmail-daily --at 06:30 -- qcl gmail fetch ...`. Job output
  goes to `~/Library/Logs/qcl/NAME.log` with timestamped start and exit lines,
  and a failed run raises a macOS notification.

## [0.2.0] - 2026-11-02

First release on PyPI.

### Changed

- Publish the distribution as `quilt-context-lake`, because PyPI rejected
  `context-lake` as too similar to the existing `contextlake` project. The
  Python import name stays `context_lake`. ([#5])
- Replace the `clake` and `context-lake` commands with a single `qcl` command. ([#5])

### Fixed

- The hint shown when an optional extra is missing now installs into the
  Python interpreter running `qcl` (`uv pip install --python <interpreter>`)
  and lists the `uv tool` command separately. Previously, following the hint
  left a `qcl` installed in a virtualenv still broken. ([#5])
- Shell-quote the interpreter path in that hint, so it works for virtual
  environments whose path contains spaces. ([#5])

## [0.1.0] - 2026-11-02

Tagged under the earlier `context-lake` name and never published to PyPI.

### Added

- Deterministic context automation as a Python package, with a `context-lake`
  command and these subcommands: `sync`, `email`, `fathom`, `index`,
  `backfill`, `survey`, `extract`, `publish`, `verify`, and the optional
  model-backed `enrich`. ([#1])
- Optional dependency extras: `s3`, `formats`, `quilt` and `models`. ([#1])
- `clake` as the primary command, with `context-lake` kept as an alias. ([#3])
- Release workflow: a `v*` tag checks the tag against `pyproject.toml` and
  `__version__`, builds with uv, tests the wheel outside the checkout, and
  publishes to PyPI with trusted publishing (OIDC), so no PyPI token is
  stored in GitHub. ([#4])

### Changed

- CI and the README use uv instead of pip. ([#4])
- GitHub Actions are pinned to commit SHAs, and checkout no longer persists
  credentials. ([#4])

[Unreleased]: https://github.com/quiltdata/context-lake/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/quiltdata/context-lake/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/quiltdata/context-lake/compare/0047908...v0.1.0
[#1]: https://github.com/quiltdata/context-lake/pull/1
[#3]: https://github.com/quiltdata/context-lake/pull/3
[#4]: https://github.com/quiltdata/context-lake/pull/4
[#5]: https://github.com/quiltdata/context-lake/pull/5
