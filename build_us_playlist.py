"""Generate a US-only M3U8 playlist from the iptv-org public API.

Usage:
    python build_us_playlist.py                 # curated list from channels.yml
    python build_us_playlist.py --all           # every US channel iptv-org knows
    python build_us_playlist.py --check         # drop streams that fail a HEAD/GET probe
    python build_us_playlist.py -o path.m3u8    # custom output path

Data sources (all public, no key required):
    https://iptv-org.github.io/api/channels.json   metadata: name, country, categories, nsfw
    https://iptv-org.github.io/api/streams.json    the actual .m3u8 / .ts URLs
    https://iptv-org.github.io/api/feeds.json      per-channel feed variants (East/West/HD...)
    https://iptv-org.github.io/api/logos.json      channel artwork
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API = "https://iptv-org.github.io/api"
# iptv-org no longer hosts prebuilt guides; its tvg-ids won't match this feed's
# ids, so map channels to guide entries in Threadfin/xTeVe rather than relying on this.
EPG_URL = "https://epgshare01.online/epgshare01/epg_ripper_US2.xml.gz"
HERE = Path(__file__).parent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

QUALITY_RANK = {"2160p": 6, "1440p": 5, "1080p": 4, "720p": 3, "576p": 2, "480p": 1}


def fetch_json(name: str):
    req = urllib.request.Request(f"{API}/{name}.json", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def load_groups(path: Path) -> dict[str, list[str]]:
    """Minimal parser for the two-level 'Group:\n  - Name' file."""
    groups: dict[str, list[str]] = {}
    current = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("- "):
            if current is None:
                raise ValueError(f"entry before any group header: {raw!r}")
            groups[current].append(line.lstrip()[2:].strip())
        elif line.endswith(":"):
            current = line[:-1].strip()
            groups.setdefault(current, [])
        else:
            raise ValueError(f"unparsable line: {raw!r}")
    return groups


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def quality_score(stream: dict) -> int:
    q = QUALITY_RANK.get(stream.get("quality") or "", 0)
    penalty = len(stream.get("labels") or [])  # "Not 24/7", "Geo-blocked", etc.
    return q * 10 - penalty


BARE_IP = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")

NUMBERS_FILE = HERE / "channel-numbers.json"
FIRST_CHANNEL = 1000


def load_numbers() -> dict[str, int]:
    if not NUMBERS_FILE.exists():
        return {}
    return {k: int(v) for k, v in json.loads(NUMBERS_FILE.read_text(encoding="utf-8")).items()}


def save_numbers(numbers: dict[str, int]) -> None:
    ordered = dict(sorted(numbers.items(), key=lambda kv: kv[1]))
    NUMBERS_FILE.write_text(json.dumps(ordered, indent=2) + "\n", encoding="utf-8")


def official_host(url: str) -> bool:
    """Bare-IP hosts signal unauthorized restreams; official FAST services always use CDN domains."""
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return False
    return bool(host) and not BARE_IP.match(host)


def probe(url: str) -> bool:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status < 400
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return False


def build(curated: bool, check: bool, out: Path, limit_per_channel: int) -> None:
    print("Downloading iptv-org data ...", file=sys.stderr)
    channels = fetch_json("channels")
    streams = fetch_json("streams")
    feeds = fetch_json("feeds")
    logos = fetch_json("logos")

    by_id = {c["id"]: c for c in channels}

    logo_for: dict[str, str] = {}
    for logo in logos:
        logo_for.setdefault(logo["channel"], logo["url"])

    # iptv-org EPG ids are "<channel>" for the main feed and "<channel>@<feed>" otherwise.
    main_feeds = {(f["channel"], f["id"]) for f in feeds if f.get("is_main")}

    streams_for: dict[str, list[dict]] = defaultdict(list)
    for s in streams:
        cid = s.get("channel")
        if cid:
            streams_for[cid].append(s)

    us = [
        c
        for c in channels
        if c.get("country") == "US"
        and not c.get("is_nsfw")
        and not c.get("closed")
        and streams_for.get(c["id"])
    ]
    print(f"US channels with at least one stream: {len(us)}", file=sys.stderr)

    # channel id -> group label
    group_of: dict[str, str] = {}
    ordered: list[dict] = []

    if curated:
        groups = load_groups(HERE / "channels.yml")
        index: dict[str, list[dict]] = defaultdict(list)
        for c in us:
            names = [c["name"], *(c.get("alt_names") or [])]
            for n in names:
                index[norm(n)].append(c)

        seen: set[str] = set()
        missing: list[str] = []
        for group, wanted in groups.items():
            for name in wanted:
                matches = index.get(norm(name)) or []
                if not matches:
                    missing.append(name)
                    continue
                for c in matches:
                    if c["id"] in seen:
                        continue
                    seen.add(c["id"])
                    group_of[c["id"]] = group
                    ordered.append(c)
        if missing:
            print(
                f"No US match for {len(missing)} entries: {', '.join(sorted(set(missing)))}",
                file=sys.stderr,
            )
    else:
        for c in sorted(us, key=lambda c: c["name"].lower()):
            cats = c.get("categories") or ["general"]
            group_of[c["id"]] = cats[0].title()
            ordered.append(c)

    # Pick the best stream(s) per channel.
    entries: list[tuple[dict, dict]] = []
    restreams = 0
    for c in ordered:
        usable = [s for s in streams_for[c["id"]] if official_host(s["url"])]
        if not usable:
            restreams += 1
            continue
        best = sorted(usable, key=quality_score, reverse=True)
        for s in best[:limit_per_channel]:
            entries.append((c, s))
    if restreams:
        print(f"Skipped {restreams} channels with only bare-IP restreams.", file=sys.stderr)

    if check:
        print(f"Probing {len(entries)} stream URLs ...", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=24) as pool:
            alive = list(pool.map(lambda e: probe(e[1]["url"]), entries))
        dropped = len(entries) - sum(alive)
        entries = [e for e, ok in zip(entries, alive) if ok]
        print(f"Dropped {dropped} dead streams.", file=sys.stderr)

    # Channel numbers are sticky so Plex's DVR lineup survives playlist changes.
    numbers = load_numbers() if curated else {}
    next_free = max(numbers.values(), default=FIRST_CHANNEL - 1) + 1
    added = 0

    lines = [f'#EXTM3U x-tvg-url="{EPG_URL}"']
    for c, s in entries:
        feed = s.get("feed")
        cid = f'{c["id"]}@{feed}' if feed and (c["id"], feed) not in main_feeds else c["id"]
        name = s.get("name") or c["name"]
        quality = s.get("quality")
        if quality and quality not in name:
            name = f"{name} ({quality})"
        attrs = [
            f'tvg-id="{cid}"',
            f'tvg-logo="{logo_for.get(c["id"], "")}"',
            f'group-title="{group_of[c["id"]]}"',
        ]
        if curated:
            if cid not in numbers:
                numbers[cid] = next_free
                next_free += 1
                added += 1
            attrs.insert(1, f'tvg-chno="{numbers[cid]}"')
        lines.append(f'#EXTINF:-1 {" ".join(attrs)},{name}')
        if s.get("referrer"):
            lines.append(f'#EXTVLCOPT:http-referrer={s["referrer"]}')
        lines.append(f'#EXTVLCOPT:http-user-agent={s.get("user_agent") or UA}')
        lines.append(s["url"])

    if curated:
        save_numbers(numbers)
        print(f"Channel numbers: {len(numbers)} known, {added} newly assigned.", file=sys.stderr)

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(entries)} entries to {out}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true", help="include every US channel")
    ap.add_argument("--check", action="store_true", help="probe each URL and drop dead ones")
    ap.add_argument("--per-channel", type=int, default=1, help="max streams per channel")
    ap.add_argument("-o", "--out", type=Path, default=HERE / "us.m3u8")
    args = ap.parse_args()
    build(curated=not args.all, check=args.check, out=args.out, limit_per_channel=args.per_channel)


if __name__ == "__main__":
    main()
