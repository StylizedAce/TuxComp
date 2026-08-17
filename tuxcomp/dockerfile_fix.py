"""Translate Dockerfile instructions that proot-distro build cannot handle.

proot-distro's build command is a Dockerfile *interpreter* for proot, not a
full docker build. It chokes on:

- `WORKDIR` + `COPY`: it creates the workdir directory, then fails writing the
  COPY layer into it ("Is a directory").
- Commands that rely on the image WORKDIR as the process CWD: proot runs
  RUN/CMD from the rootfs root instead.

This module rewrites a Dockerfile into an equivalent one that works under
proot-distro, WITHOUT changing the file on disk:

- `WORKDIR /x` -> dropped (tracked in memory instead)
- `COPY a .` / `ADD a .` -> destination made absolute against the workdir
- `RUN <shell>` -> prefixed with `cd <workdir> &&` (when workdir != /)
- `RUN ["a", "b"]` (exec form) -> `RUN /bin/sh -c 'cd <wd> && exec a b'`
- `CMD`/`ENTRYPOINT` shell form -> prefixed with `cd <workdir> && `
- `CMD`/`ENTRYPOINT` exec form -> wrapped in
  `["/bin/sh", "-c", "cd <wd> && exec \"$0\" \"$@\"", <args...>]`
- `USER x` -> dropped (proot always runs as root)
- everything else (FROM/ENV/ARG/EXPOSE/LABEL/COPY --from=...) passes through

The result is written to a sibling file (`.tuxcomp-<name>`) so the original
Dockerfile still builds on a real docker host (e.g. a Raspberry Pi).
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

_INSTRUCTION = None  # placeholder; regex built below for clarity

import re

_INSTRUCTION_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)\s*(.*)$")


def _logical_lines(lines: list[str]) -> list[str]:
    """Join backslash-continued lines so each instruction is one block.

    Shell continuations (`RUN a && \\` + newline + `b`) are preserved as-is;
    only the instruction keyword is parsed from the first physical line.
    """
    logical: list[str] = []
    buf: list[str] = []
    for line in lines:
        buf.append(line)
        if line.rstrip().endswith("\\"):
            continue
        logical.append("\n".join(buf))
        buf = []
    if buf:
        logical.append("\n".join(buf))
    return logical


def _split_block(block: str) -> tuple[str, str]:
    """Split an instruction block into (first line, continuation lines)."""
    if "\n" in block:
        first, _, rest = block.partition("\n")
        return first, rest
    return block, ""


def _is_run_block(block: str) -> bool:
    first, _ = _split_block(block)
    m = _INSTRUCTION_RE.match(first.strip())
    return bool(m and m.group(1).upper() == "RUN")


def _run_body(block: str) -> str | None:
    """Return a RUN block's command body (single line), or None if exec-form."""
    first, rest = _split_block(block)
    m = _INSTRUCTION_RE.match(first.strip())
    if not m or m.group(1).upper() != "RUN":
        return None
    body = m.group(2).strip()
    if body.startswith("["):
        return None  # exec form - cannot merge with &&
    if rest:
        # collapse backslash-continuations into a single line
        body = body + " " + rest.replace("\\\n", " ").strip()
    return body


def _merge_consecutive_runs(logical: list[str]) -> list[str]:
    """Fuse consecutive shell-form RUN blocks into one.

    proot-distro build snapshots the entire rootfs after every RUN (no
    overlayfs), so each RUN is expensive. Merging adjacent RUNs with `&&`
    (standard Docker best practice) cuts the number of snapshots and can
    nearly halve build time on multi-RUN Dockerfiles.
    """
    merged: list[str] = []
    pending: list[str] = []
    for block in logical:
        body = _run_body(block)
        if body is not None:
            pending.append(body)
            continue
        if pending:
            merged.append("RUN " + " && ".join(pending))
            pending = []
        merged.append(block)
    if pending:
        merged.append("RUN " + " && ".join(pending))
    return merged


def _workdir_join(workdir: str, dest: str) -> str:
    """Join a relative COPY/ADD destination onto the current workdir."""
    base = workdir.rstrip("/")
    if dest == ".":
        return base or "/"
    return (base + "/" + dest).replace("//", "/")


def _rewrite_copy(kw: str, args_text: str, workdir: str) -> str | None:
    """Absolute-ify the destination of COPY/ADD. None = leave unchanged.

    proot-distro build cannot COPY a *file* into a directory that already
    exists (it reports "Is a directory" naming the workdir). RUN instructions
    may have mkdir'd the workdir by the time a COPY runs, so:
      - single FILE source, dest == workdir (was `.`) -> target the explicit
        file path <workdir>/<basename>
      - directory source (`.`, `dir/`) -> keep the dir form (works even when
        the dir exists)
    """
    try:
        parts = shlex.split(args_text)
    except ValueError:
        return None
    if not parts:
        return None
    dest = parts[-1]
    if dest.startswith("/"):
        return None
    base = workdir.rstrip("/")
    sources = parts[:-1]
    dir_source = any(s.endswith("/") or s == "." for s in sources)

    if not dir_source and len(sources) == 1:
        # single-file COPY: proot-distro "renames" the file to the dest NAME
        # instead of placing it inside a dir, so a dir dest must be expanded
        # to an explicit file path <dir>/<basename>
        src_base = os.path.basename(sources[0].rstrip("/"))
        if dest == ".":
            new_dest = (base + "/" + src_base).replace("//", "/")
        elif dest.endswith("/"):
            new_dest = (base + "/" + dest + src_base).replace("//", "/")
        else:
            new_dest = (base + "/" + dest).replace("//", "/")
    else:
        new_dest = (base + "/" + dest).replace("//", "/") if dest != "." else (base or "/")
    new_parts = sources + [new_dest]
    return kw + " " + " ".join(shlex.quote(p) for p in new_parts)


def _rewrite_run(args_text: str, workdir: str) -> str | None:
    """Prefix RUN with `mkdir -p <wd> && cd <wd> &&`. None = leave unchanged."""
    if workdir in ("", "/"):
        return None
    text = args_text.strip()
    if text.startswith("["):
        try:
            argv = json.loads(text)
            joined = shlex.join([str(a) for a in argv])
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        return (
            f"RUN /bin/sh -c {shlex.quote(f'mkdir -p {workdir} && cd {workdir} && exec {joined}')}"
        )
    return f"RUN mkdir -p {shlex.quote(workdir)} && cd {shlex.quote(workdir)} && {text}"


def _rewrite_cmd_entrypoint(kw: str, args_text: str, workdir: str) -> str | None:
    """Wrap CMD/ENTRYPOINT so it runs in the workdir. None = leave unchanged."""
    if workdir in ("", "/"):
        return None
    text = args_text.strip()
    if text.startswith("["):
        try:
            argv = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(argv, list) or not argv:
            return None
        wrapped = (
            ["/bin/sh", "-c", f'mkdir -p {workdir} && cd {workdir} && exec "$0" "$@"']
            + [str(a) for a in argv]
        )
        return f"{kw} {json.dumps(wrapped)}"
    return f"{kw} mkdir -p {shlex.quote(workdir)} && cd {shlex.quote(workdir)} && {text}"


def preprocess_dockerfile(
    text: str, wheels_available: bool = False
) -> tuple[str, list[str]]:
    """Return (rewritten Dockerfile text, list of human-readable changes).

    wheels_available: a `wheels/` dir with prefetched aarch64 wheels sits in
    the build context (shipped by `tuxcomp deploy --pip-wheels`). pip RUNs
    then get `--find-links /app/wheels` and a COPY of the wheels dir is added
    when the Dockerfile doesn't `COPY .` the whole context.
    """
    logical = _merge_consecutive_runs(_logical_lines(text.splitlines()))
    out: list[str] = []
    notes: list[str] = []
    workdir = "/"

    for block in logical:
        first, rest = _split_block(block)
        stripped = first.strip()
        m = _INSTRUCTION_RE.match(stripped)
        if not m:
            out.append(block)
            continue
        kw, args = m.group(1).upper(), m.group(2).strip()

        def emit(new_first: str) -> None:
            if rest:
                out.append(new_first + "\n" + rest)
            else:
                out.append(new_first)

        if kw == "WORKDIR":
            target = args.strip('"').strip("'")
            if target.startswith("/"):
                workdir = target.rstrip("/") or "/"
            else:
                workdir = _workdir_join(workdir, target)
            notes.append(f"WORKDIR -> {workdir} (relative paths made absolute)")
            continue

        if kw == "USER":
            notes.append(f"USER '{args}' dropped (proot runs as root)")
            continue

        if kw in ("COPY", "ADD"):
            new_line = _rewrite_copy(kw, args, workdir)
            emit(new_line or first)
            if new_line:
                notes.append(f"{kw} destination made absolute -> {new_line.strip()}")
            continue

        if kw == "RUN":
            new_line = _rewrite_run(args, workdir)
            if wheels_available and new_line and "pip install" in new_line:
                new_line = new_line.replace(
                    "pip install", "pip install --find-links /app/wheels", 1
                )
                notes.append("pip install uses local wheels (--find-links /app/wheels)")
            emit(new_line or first)
            if new_line:
                notes.append("RUN prefixed with cd (workdir)")
            continue

        if kw in ("CMD", "ENTRYPOINT"):
            new_line = _rewrite_cmd_entrypoint(kw, args, workdir)
            emit(new_line or first)
            if new_line:
                notes.append(f"{kw} wrapped to run in workdir")
            continue

        out.append(block)

    if wheels_available:
        # if the Dockerfile doesn't COPY the whole context, the wheels dir
        # isn't in the image - add an explicit COPY before the first pip RUN
        has_full_copy = any(
            _copy_copies_context(b) for b in out
        )
        if not has_full_copy:
            for i, block in enumerate(out):
                if "pip install" in block:
                    out.insert(i, "COPY wheels /app/wheels")
                    notes.append("COPY wheels /app/wheels (context not fully copied)")
                    break

    return "\n".join(out) + "\n", notes


def _copy_copies_context(block: str) -> bool:
    """True if a COPY/ADD block copies the whole build context (`.`)."""
    first, _ = _split_block(block)
    m = _INSTRUCTION_RE.match(first.strip())
    if not m or m.group(1).upper() not in ("COPY", "ADD"):
        return False
    try:
        parts = shlex.split(m.group(2))
    except ValueError:
        return False
    # ignore --from= stage copies; look at source paths
    sources = [p for p in parts if not p.startswith("--")]
    return any(s in (".", "./") for s in sources[:-1])


def fix_dockerfile(path: Path, out_path: Path) -> tuple[bool, list[str]]:
    """Rewrite a Dockerfile for proot-distro compatibility.

    Returns (changed, notes). Only writes out_path when changes are needed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False, []
    wheels_dir = path.parent / "wheels"
    wheels_available = wheels_dir.is_dir() and any(
        wheels_dir.glob("*.whl")
    )
    new_text, notes = preprocess_dockerfile(text, wheels_available=wheels_available)
    if not notes:
        return False, []
    out_path.write_text(new_text, encoding="utf-8")
    return True, notes
