#!/usr/bin/env python3
"""Download the yearly result files of tennis-data.co.uk, politely and only what is missing.

The links are read from the site's data page. Files of the current year are fetched again on
every run because they keep growing. A sha256 manifest records what was received and when.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PAGE = "https://tennis-data.co.uk/data.php"
USER_AGENT = "RNGN-Matchday-data/0.1"
DEFAULT_OUT = Path(__file__).resolve().parents[2] / "local-data" / "tennis" / "raw" / "tennis-data"
LINK = re.compile(r'href="([^"]+/(\d{4}w?)/(\d{4})\.xlsx?)"', re.I)


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


def yearly_links(html: str) -> list[tuple[str, str, str]]:
    """[(link, folder such as 2024w, file name)], sorted; a trailing w marks the women's tour."""
    found = {}
    for link, folder, _year in LINK.findall(html):
        found[folder] = (link, folder, link.rsplit("/", 1)[1])
    return [found[key] for key in sorted(found)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--pause", type=float, default=3.0)
    args = parser.parse_args()

    links = yearly_links(fetch(PAGE).decode("utf-8", "replace"))
    current = str(datetime.now(timezone.utc).year)
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "manifest.jsonl").open("a", encoding="utf-8") as manifest:
        for link, folder, name in links:
            target = args.out / folder / name
            if target.exists() and not folder.startswith(current):
                continue
            body = fetch(urllib.parse.urljoin(PAGE, link))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            manifest.write(json.dumps({
                "file": f"{folder}/{name}", "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "at": datetime.now(timezone.utc).isoformat(),
            }) + "\n")
            manifest.flush()
            print(f"{folder}/{name} {len(body)}", flush=True)
            time.sleep(args.pause)
    print(f"links: {len(links)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
