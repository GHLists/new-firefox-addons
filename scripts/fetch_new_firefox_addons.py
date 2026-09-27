#!/usr/bin/env python3
"""Fetch Firefox add-ons newly created on addons.mozilla.org.

New add-ons are read from the [AMO API v5](https://addons.mozilla.org/api/v5/)
search endpoint sorted by creation date. The search is capped by pagination,
so the manifest records ``source_truncated`` whenever the page limit is
reached before the requested window is covered.

The end of the last list is stored in the manifest so the next run resumes
where the previous one stopped.
"""

import argparse
import csv
import datetime as dt
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SEARCH_URL = (
    "https://addons.mozilla.org/api/v5/addons/search/"
    "?sort=created&sort_direction=descending&locale=en-US&page_size=50"
)
DEFAULT_USER_AGENT = (
    "new-firefox-addons/1.0 (https://github.com/GHLists/new-firefox-addons)"
)

MAX_PAGES = 20
PAGE_SIZE = 50
DESCRIPTION_LIMIT = 300
CSV_HEADER = (
    "created_at",
    "addon",
    "slug",
    "version",
    "users",
    "authors",
    "description",
)

TRANSIENT_ERRORS = (
    urllib.error.URLError,
    TimeoutError,
    json.JSONDecodeError,
    http.client.HTTPException,
    OSError,
)


class NotFound(Exception):
    pass


def iso(moment):
    moment = moment.astimezone(dt.timezone.utc)
    if moment.microsecond:
        fraction = f"{moment.microsecond:06d}".rstrip("0")
        return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{fraction}Z"
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value):
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    moment = dt.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc)


def timestamp_filename(moment):
    moment = moment.astimezone(dt.timezone.utc)
    stamp = moment.strftime("%Y-%m-%dT%H-%M-%S")
    if moment.microsecond:
        stamp += "-" + f"{moment.microsecond:06d}".rstrip("0")
    return stamp + "Z"


def fetch_json(url, user_agent, retries=3, backoff=5.0):
    last_error = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise NotFound(url) from error
            last_error = error
        except TRANSIENT_ERRORS as error:
            last_error = error
        if attempt < retries:
            print(f"attempt {attempt} failed ({last_error}), retrying", file=sys.stderr)
            time.sleep(backoff * attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def localized(value):
    """AMO returns localized fields as dicts keyed by locale."""
    if isinstance(value, dict):
        if not value:
            return ""
        text = value.get("en-US")
        if text is None:
            key = sorted(value)[0]
            text = value[key]
        return text
    return value


def clean_text(value, limit=DESCRIPTION_LIMIT):
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "\u2026"
    return text


def build_row(addon, created):
    authors = addon.get("authors")
    if isinstance(authors, list):
        names = [
            str(author.get("name") or "")
            for author in authors
            if isinstance(author, dict)
        ]
        authors_text = "; ".join(name for name in names if name)
    else:
        authors_text = ""
    version = (addon.get("current_version") or {}).get("version")
    users = addon.get("average_daily_users")
    return {
        "created_at": iso(created),
        "addon": clean_text(localized(addon.get("name")) or addon.get("slug") or "", 100),
        "slug": addon.get("slug") or "",
        "version": clean_text(version, 20),
        "users": users if isinstance(users, int) else "",
        "authors": clean_text(authors_text, 100),
        "description": clean_text(localized(addon.get("summary"))),
    }


def write_csv(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temporary, path)


def read_manifest_text(path):
    """Read the manifest from disk, or fall back to the committed copy.

    The workflow checks out only ``scripts`` from the repository, so the
    manifest can be missing from the working tree even though it is committed.
    """
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
    except json.JSONDecodeError as error:
        raise RuntimeError(f"manifest {path} is not valid JSON") from error
    if not isinstance(data, dict):
        raise RuntimeError(f"manifest {path} must contain a JSON object")
    version = data.get("state_version", 1)
    if version != 1:
        raise RuntimeError(f"manifest {path} has an unsupported state version")
    return data


def save_manifest(path, manifest):
    manifest_path = Path(path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_name(f".{manifest_path.name}.tmp")
    text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, manifest_path)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        help="UTC start timestamp as ISO 8601 (default: end of the last list)",
    )
    parser.add_argument(
        "--until",
        help="UTC end timestamp as ISO 8601 (default: now)",
    )
    parser.add_argument("--output-dir", default="data")
    parser.add_argument("--manifest", default="latest.json")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument(
        "--lookback-hours",
        type=float,
        default=1.0,
        help="window length when no previous list exists (default: 1)",
    )
    parser.add_argument(
        "--api-delay",
        type=float,
        default=0.5,
        help="seconds between AMO API requests (default: 0.5)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    until = parse_timestamp(args.until) if args.until else now
    manifest = load_manifest(args.manifest)

    if args.since:
        since = parse_timestamp(args.since)
        if "window" in manifest:
            stored_window = parse_timestamp(manifest["window"])
            if since < stored_window:
                raise RuntimeError(
                    "backfill would move the window backwards; "
                    f"the manifest window is {iso(stored_window)}"
                )
    elif "window" in manifest:
        since = parse_timestamp(manifest["window"])
    else:
        since = until - dt.timedelta(hours=args.lookback_hours)

    rows = []
    skipped = 0
    seen_ids = set()
    window_ended = False
    truncated = False

    page = 1
    while page <= MAX_PAGES and not window_ended:
        url = f"{SEARCH_URL}&page={page}"
        data = fetch_json(url, args.user_agent, retries=args.retries)
        results = data.get("results")
        if not isinstance(results, list):
            raise RuntimeError("AMO response does not contain a results list")
        if not results:
            break
        for addon in results:
            if not isinstance(addon, dict):
                skipped += 1
                continue
            try:
                created = parse_timestamp(addon.get("created"))
            except (TypeError, ValueError):
                skipped += 1
                continue
            if created > until:
                continue
            if created <= since:
                window_ended = True
                break
            key = addon.get("id") or addon.get("slug")
            if key in seen_ids:
                continue
            seen_ids.add(key)
            rows.append(build_row(addon, created))
        if len(results) < PAGE_SIZE:
            break
        if page == MAX_PAGES:
            truncated = True
        page += 1
        time.sleep(args.api_delay)

    if not window_ended and truncated:
        print(
            "AMO search pagination cap reached before the window was "
            "covered; older entries within this window are missing",
            file=sys.stderr,
        )
    if skipped:
        print(f"skipped {skipped} malformed add-ons", file=sys.stderr)

    rows.sort(key=lambda row: row["created_at"])
    manifest["window"] = iso(until)
    manifest["source_truncated"] = bool(truncated)
    if rows:
        output = (
            Path(args.output_dir)
            / f"new-firefox-addons-{timestamp_filename(until)}.csv"
        )
        write_csv(output, rows)
        manifest["list"] = {
            "path": output.as_posix(),
            "from": iso(since),
            "to": iso(until),
            "count": len(rows),
        }
        print(
            f"wrote {len(rows)} add-ons created between {iso(since)} "
            f"and {iso(until)} to {output}"
        )
    else:
        print(f"no new add-ons between {iso(since)} and {iso(until)}")
    save_manifest(args.manifest, manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
