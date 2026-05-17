# Developer notes: uv

Internal cheat sheet for building and publishing **helmadm**. End users: [README.md](../../README.md) and [docs/](../).

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+ (see `requires-python` in `pyproject.toml`).

## Setup

```bash
uv sync          # .venv + project (editable) + dev deps (pytest)
uv run pytest
uv run helmadm --help
```

## Dependencies

```bash
uv add <package>              # runtime dep → pyproject.toml + uv.lock
uv add --dev <package>        # dev group (pytest, etc.)
uv lock                       # refresh lock only
uv lock --upgrade-package X   # bump one locked package
```

Commit `pyproject.toml` and `uv.lock` together when deps change.

## Version bump

Version lives in `[project].version` in `pyproject.toml`.

```bash
uv version                    # print current
uv version 0.2.0              # set explicitly
uv version --bump patch       # 0.1.0 → 0.1.1
uv version --bump minor       # 0.1.0 → 0.2.0
uv version --bump major       # 0.1.0 → 1.0.0
uv version --bump patch --dry-run
```

`uv version` re-locks by default; use `--frozen` to skip lock updates.

Typical release prep:

```bash
uv version --bump patch
uv run pytest
git add pyproject.toml uv.lock
git commit -m "Release 0.1.1"
git tag v0.1.1
```

## Build

Uses **hatchling** (`[build-system]` in `pyproject.toml`). Artifacts go to `dist/`:

```bash
uv build              # sdist + wheel
uv build --sdist      # tarball only
uv build --wheel      # wheel only
uv build -o /tmp/out  # custom output dir
```

Inspect before publish:

```bash
tar tzf dist/helmadm-*.tar.gz
unzip -l dist/helmadm-*.whl
```

## Publish (PyPI)

```bash
uv build
export UV_PUBLISH_TOKEN=pypi-...   # or: uv publish --token pypi-...
uv publish --dry-run                 # validate, no upload
uv publish                           # uploads dist/*
```

Alternatives: `--username` / `--password`, or `~/.pypirc`. Use `uv publish --publish-url …` for TestPyPI.

After a successful upload, push the tag:

```bash
git push && git push --tags
```

Install from PyPI (sanity check):

```bash
uvx helmadm --help
uv tool install helmadm
# or: pip install helmadm
```

## Useful flags

| Task | Command |
|------|---------|
| Reproducible CI install | `uv sync --locked` |
| Skip dev deps | `uv sync --no-dev` |
| Run without syncing | `uv run --no-sync pytest` |
| Clean rebuild | `rm -rf dist/ && uv build` |

`dist/` is build output only — do not commit.
