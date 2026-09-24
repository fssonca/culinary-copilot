#!/usr/bin/env python3
"""Sync ``.env`` with ``.env.example`` without losing local values.

Template-driven sync:

* ``.env.example`` defines the desired keys, order, comments, and defaults.
* Existing keys in ``.env`` keep their current values verbatim.
* Missing keys are added with the default value from ``.env.example``.
* Keys in ``.env`` but not in ``.env.example`` are preserved at the end
  under an ``# Extra local variables`` header (unless ``--drop-extra``).
* Blank/break lines, trailing whitespace, and CRLF are normalized so the
  output matches the example byte-for-byte (whitespace-only lines -> ``""``,
  single trailing newline, exactly one blank before the extras header).

Only the standard library is used. A timestamped backup (``.env.bak.*``)
is created before any write unless ``--no-backup`` is given.

Examples:
    python scripts/sync_env.py
    python scripts/sync_env.py --check
    python scripts/sync_env.py --example .env.example --env .env --dry-run
"""

from __future__ import annotations

import argparse
import datetime
import re
import shutil
import sys
from pathlib import Path

_ASSIGNMENT_RE = re.compile(
    r"^(?:(?P<export>export\s+))?"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)$",
)


def _split_lines(text: str) -> list[str]:
    """Split text into lines, normalizing CRLF/CR and dropping the final newline marker."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    # ``"a\\n".split("\\n")`` -> ``["a", ""]``; the last empty marks the final
    # newline, not a real blank line. Drop it so blank-line counts match.
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def normalize_blank(line_wo_nl: str) -> str | None:
    """Return ``""`` for empty/whitespace-only lines, else ``None``."""
    if line_wo_nl.strip() == "":
        return ""
    return None


def parse_assignment(line_wo_nl: str) -> tuple[str, str, str] | None:
    """Parse a ``KEY=VALUE`` line (no trailing newline).

    Returns ``(key, normalized_value, export_prefix)`` where the value is
    stripped of surrounding whitespace (inner content, quotes, and inline
    ``#`` are preserved verbatim). Returns ``None`` for blank lines,
    comments, and non-assignments.
    """
    if line_wo_nl.strip() == "" or line_wo_nl.lstrip().startswith("#"):
        return None
    match = _ASSIGNMENT_RE.match(line_wo_nl.strip())
    if not match:
        return None
    key = match.group("key")
    raw_value = match.group("value")
    value = raw_value.strip()
    export_raw = match.group("export")
    export_prefix = "export " if export_raw else ""
    return key, value, export_prefix


def render_assignment(key: str, value: str, export_prefix: str) -> str:
    """Render a canonical ``[export ]KEY=value`` line (empty value -> ``KEY=``)."""
    return f"{export_prefix}{key}={value}"


def load_env_file(path: Path) -> tuple[list[str], dict[str, str], dict[str, str], list[str]]:
    """Read a dotenv file, returning ``(lines, values, exports, order)``.

    ``lines`` are normalized (no line endings, blank lines as ``""``,
    trailing whitespace stripped). ``values`` maps key to normalized value
    (last occurrence wins); ``exports`` maps key to ``"export "`` or ``""``.
    """
    text = path.read_text(encoding="utf-8")
    raw_lines = _split_lines(text)
    lines: list[str] = []
    values: dict[str, str] = {}
    exports: dict[str, str] = {}
    order: list[str] = []
    for raw in raw_lines:
        blank = normalize_blank(raw)
        if blank is not None:
            lines.append(blank)
            continue
        parsed = parse_assignment(raw)
        if parsed is None:
            # Comment or garbage: keep it, minus trailing whitespace.
            lines.append(raw.rstrip())
            continue
        key, value, export_prefix = parsed
        lines.append(render_assignment(key, value, export_prefix))
        if key not in values:
            order.append(key)
        else:
            print(
                f"warning: duplicate key {key!r} in {path}, using last value",
                file=sys.stderr,
            )
        values[key] = value
        exports[key] = export_prefix
    return lines, values, exports, order


def build_synced_lines(
    example_lines: list[str],
    example_values: dict[str, str],
    env_values: dict[str, str],
    env_exports: dict[str, str],
) -> tuple[list[str], list[str], list[str], set[str]]:
    """Render synced lines from the example template.

    Blank lines and comments match the example exactly (normalized).
    Existing keys keep the ``.env`` value; missing keys use the example default.
    Returns ``(lines, added, kept, example_keys)``.
    """
    lines: list[str] = []
    added: list[str] = []
    kept: list[str] = []
    example_keys = set(example_values.keys())
    for line in example_lines:
        parsed = parse_assignment(line)
        if parsed is None:
            # Blank or comment: already normalized, matches example exactly.
            lines.append(line)
            continue
        key, example_value, example_export = parsed
        if key in env_values:
            lines.append(render_assignment(key, env_values[key], env_exports.get(key, "")))
            kept.append(key)
        else:
            lines.append(render_assignment(key, example_value, example_export))
            added.append(key)
    return lines, added, kept, example_keys


def collect_extra_lines(env_lines: list[str], example_keys: set[str]) -> list[str]:
    """Collect canonical lines for keys present only in the existing ``.env``.

    Order follows last occurrence in ``.env``. Values are already normalized.
    """
    last_index: dict[str, int] = {}
    for i, line in enumerate(env_lines):
        parsed = parse_assignment(line)
        if parsed is None:
            continue
        key = parsed[0]
        if key not in example_keys:
            last_index[key] = i
    ordered_keys = sorted(last_index, key=lambda k: last_index[k])
    extras: list[str] = []
    for key in ordered_keys:
        # Re-derive export prefix from the last occurrence line.
        line = env_lines[last_index[key]]
        parsed = parse_assignment(line)
        if parsed is None:  # pragma: no cover - already filtered
            continue
        _, value, export_prefix = parsed
        extras.append(render_assignment(key, value, export_prefix))
    return extras


def _canonical_text(text: str) -> str:
    """Canonical form for comparison: LF endings, blanks as ``""``.

    Trailing whitespace stripped, assignment spacing canonicalized so
    ``KEY = v``, ``KEY=v ``, and ``export KEY=v`` compare by effective value.
    Break-line positions/counts are preserved, so real blank-line drift still
    counts as out of sync.
    """
    out: list[str] = []
    for raw in _split_lines(text):
        if raw.strip() == "":
            out.append("")
            continue
        parsed = parse_assignment(raw)
        if parsed is None:
            out.append(raw.rstrip())
        else:
            key, value, export_prefix = parsed
            out.append(render_assignment(key, value, export_prefix))
    return "\n".join(out) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--example", default=".env.example", help="Template file (default: .env.example)"
    )
    parser.add_argument("--env", default=".env", help="Target file to update (default: .env)")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if .env would change; do not write.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the synced content to stdout; do not write.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not create a timestamped .env.bak.* backup before writing.",
    )
    parser.add_argument(
        "--drop-extra",
        action="store_true",
        help="Drop keys that exist in .env but not in .env.example instead of preserving them.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    example_path = Path(args.example)
    env_path = Path(args.env)

    if not example_path.is_file():
        print(f"error: example file not found: {example_path}", file=sys.stderr)
        return 2
    if not env_path.is_file():
        print(f"error: env file not found: {env_path}", file=sys.stderr)
        return 2

    example_lines, example_values, _, _ = load_env_file(example_path)
    env_lines, env_values, env_exports, _ = load_env_file(env_path)
    synced_lines, added, kept, example_keys = build_synced_lines(
        example_lines, example_values, env_values, env_exports
    )

    extras: list[str] = []
    if not args.drop_extra:
        extras = collect_extra_lines(env_lines, example_keys)
        if extras:
            # Strip trailing blanks from the template, then separate with
            # exactly one blank line so break lines always match.
            while synced_lines and synced_lines[-1] == "":
                synced_lines.pop()
            synced_lines.append("")
            synced_lines.append("# Extra local variables (in .env, not in example; preserved).")
            synced_lines.extend(extras)

    new_content = "\n".join(synced_lines) + "\n"
    raw_old = env_path.read_text(encoding="utf-8")
    old_content = raw_old.replace("\r\n", "\n").replace("\r", "\n")
    in_sync = _canonical_text(old_content) == _canonical_text(new_content)

    if args.dry_run:
        sys.stdout.write(new_content)
        return 0

    if in_sync and old_content == new_content:
        print("in sync: .env matches .env.example keys; local values preserved.")
        return 0

    missing_msg = f"missing to add: {len(added)}"
    if added:
        missing_msg += f" ({', '.join(added)})"
    extra_msg = f"extra preserved: {len(extras)}"
    if extras:
        extra_msg += f" ({len(extras)} lines)"
    if args.check:
        if in_sync:
            print("in sync (keys match; only whitespace formatting differs).")
            return 0
        print(f"out of sync: {missing_msg}; {extra_msg}; kept values: {len(kept)}.")
        return 1

    if in_sync:
        # Keys/values/blanks match semantically; normalize formatting only
        # (whitespace-only blanks -> "", trailing spaces stripped, LF endings)
        # so break lines match the example byte-for-byte next time.
        if not args.no_backup:
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = env_path.with_name(f"{env_path.name}.bak.{stamp}")
            shutil.copy2(env_path, backup_path)
            print(f"backup: {backup_path}")
        env_path.write_text(new_content, encoding="utf-8")
        print("normalized: keys in sync; blank-line/whitespace formatting fixed.")
        return 0

    if not args.no_backup:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = env_path.with_name(f"{env_path.name}.bak.{stamp}")
        shutil.copy2(env_path, backup_path)
        print(f"backup: {backup_path}")
    env_path.write_text(new_content, encoding="utf-8")
    if args.drop_extra:
        print(f"synced: kept {len(kept)}, added {len(added)}, dropped extras.")
    else:
        print(f"synced: kept {len(kept)}, added {len(added)}, preserved {len(extras)} extra(s).")
    if added:
        print(f"added: {', '.join(added)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
