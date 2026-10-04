# Versioning

TuxComp uses a simple scheme, designed for a tool that is installed directly
from GitHub on phones.

## Where the version lives

Two files must always match:

- `tuxcomp/__init__.py` → `__version__`
- `pyproject.toml` → `[project] version`

`tuxcomp --version` reads the package value, and `tuxcomp deploy` upgrades the
phone whenever the remote version differs, so a mismatch would cause push/pull
loops.

## Bump policy

- One version bump per change set (one phase / one focused feature or fix).
- Patch segment by default (`0.8.16` → `0.8.17`); minor/major only for
  breaking changes to compose files or CLI behavior.
- Every bump gets an entry at the top of `CHANGELOG.md` describing what
  changed and how to try it.

## Workflow

1. Make the change on a branch (never directly on `main`).
2. Bump both version files, add the `CHANGELOG.md` entry.
3. Run the test suite: `python -m pytest -q`.
4. Optionally install the branch on a phone for real testing **before**
   pushing, without touching the GitHub install:
   ```bash
   python -m build --wheel
   scp dist/tuxcomp-<version>-py3-none-any.whl root@<phone>:~/
   # on the phone:
   pip install --upgrade ~/tuxcomp-<version>-py3-none-any.whl
   tuxcomp --version && tuxcomp doctor
   ```
   To go back to the published version:
   `pip install --upgrade git+https://github.com/StylizedAce/TuxComp.git`
5. The `CHANGELOG.md` entry keeps `Status: UNPUSHED — awaiting confirmation`
   until the change has been reviewed and approved.
6. Commit with the message convention `vX.Y.Z: short summary`
   (see `git log` for examples).
7. Push only after approval. Deleting the branch discards everything if the
   change was not wanted.

Nothing is committed or pushed to `main` without explicit confirmation.
