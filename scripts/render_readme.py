#!/usr/bin/env python3
"""Render a README with the latest Firefox add-ons list."""

import argparse
import csv
import datetime as dt
import json
import subprocess
import sys
import urllib.parse
from pathlib import Path

INTRO = """\
# New Firefox add-ons

Hourly lists of add-ons newly created on
[addons.mozilla.org](https://addons.mozilla.org/), taken from the
[AMO API v5](https://addons.mozilla.org/api/v5/) search endpoint sorted by
creation date. A GitHub Actions workflow runs every hour, fetches the add-ons
created since the previous list and commits one CSV per run to [`data/`](data/),
e.g. [`data/new-firefox-addons-<timestamp>.csv`](data/).

Read the latest list below.
"""

SECTION = """\
## Latest list — {end}

New add-ons created between {start} and {end}.

[Full CSV]({csv_path})

{body}
"""

TABLE_HEADER = """\
| Created (UTC) | Add-on | Version | Users | Authors | Description |
| :------------ | :----- | :------ | ----: | :------ | :---------- |"""

ATTRIBUTION = """\
## Data source

Data comes from the [Mozilla Add-ons API](https://addons.mozilla.org/api/v5/),
provided by Mozilla. Add-on metadata is provided by the add-on authors.
This project is not affiliated with or endorsed by the Mozilla Foundation.
"""


def parse_iso(value):
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def display_timestamp(value):
    moment = parse_iso(value)
    if moment is None:
        return str(value)
    return moment.strftime("%Y-%m-%d %H:%M UTC")


def display_time(value):
    moment = parse_iso(value)
    if moment is None:
        return str(value)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def display_count(value):
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return ""


def clean_cell(value, limit=80):
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "\u2026"
    return text.replace("|", "\\|")


def addon_link(row):
    slug = str(row.get("slug") or "").strip()
    name = str(row.get("addon") or "").strip()
    if slug:
        url = "https://addons.mozilla.org/firefox/addon/" + urllib.parse.quote(slug)
        return f"[{name or slug}]({url})"
    return clean_cell(name, 60)


def read_csv_text(path):
    file = Path(path)
    if file.exists():
        return file.read_text(encoding="utf-8")
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{path}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout


def render_rows(rows):
    return "\n".join(
        f"| {display_time(row.get('created_at'))} | {addon_link(row)} "
        f"| {clean_cell(row.get('version'), 20)} "
        f"| {display_count(row.get('users'))} "
        f"| {clean_cell(row.get('authors'), 30)} "
        f"| {clean_cell(row.get('description'))} |"
        for row in rows
    )


def read_manifest_text(path):
    """Read the manifest from disk, or fall back to the committed copy."""
    manifest_path = Path(path)
    try:
        return manifest_path.read_text(encoding="utf-8")
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["git", "show", f"HEAD:{manifest_path.as_posix()}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout


def load_manifest(path):
    text = read_manifest_text(path)
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def render_section(entry, rows, limit):
    path = entry.get("path")
    if rows is None:
        body = "_The latest CSV could not be read; open it for the full list._"
    elif not rows:
        body = "_No add-ons were created in this window._"
    else:
        body = TABLE_HEADER + "\n" + render_rows(rows[:limit])
        if len(rows) > limit:
            body += (
                f"\n\n_Showing the first {limit:,} of {len(rows):,} add-ons; "
                f"see the [full CSV]({path})._"
            )
    return SECTION.format(
        end=display_timestamp(entry.get("to")),
        start=display_timestamp(entry.get("from")),
        csv_path=path,
        body=body,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="latest.json")
    parser.add_argument("--output", default="README.md")
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    entry = manifest.get("list")
    if not isinstance(entry, dict):
        entry = None
    content = INTRO + "\n"
    if entry and entry.get("path"):
        text = read_csv_text(entry["path"])
        rows = list(csv.DictReader(text.splitlines())) if text is not None else None
        content += render_section(entry, rows, args.limit)
    else:
        content += "_No list has been generated yet._\n"
        print(
            "no list found in the manifest; rendering an empty README",
            file=sys.stderr,
        )

    if manifest.get("source_truncated"):
        content += "\n> The search page limit was reached, so some add-ons in this\n> window may be missing.\n"

    content += "\n" + ATTRIBUTION
    Path(args.output).write_text(content, encoding="utf-8")
    print(f"wrote README to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
