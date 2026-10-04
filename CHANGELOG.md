# Changelog

All notable changes to TuxComp are recorded here. Entries marked
`UNPUSHED — awaiting confirmation` exist only on a local branch until the
change is reviewed and approved for `main`.

## v0.8.17 — 2026-10-05

Status: UNPUSHED — awaiting confirmation

- Add `docs/RESOURCE-MANAGEMENT.md`: how Android allocates CPU/memory to the
  Termux app, what TuxComp does about it, and the measured limits of a phone
  as a server (background cpuset, thread-count thrash, phantom-process budget).
- Add `docs/VERSIONING.md`: where the version lives, bump policy, branch
  workflow, and how to test a build on a phone via wheel + scp before pushing.
- Add this changelog.
- README: device dedication notice and a link to the resource guide.

## v0.8.16

- Drop dead tunnel helper and other unreferenced code.
