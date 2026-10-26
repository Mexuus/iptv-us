import json
import urllib.request


def get(name):
    return json.load(urllib.request.urlopen(f"https://iptv-org.github.io/api/{name}.json"))


channels = get("channels")
streams = get("streams")
have = {s["channel"] for s in streams if s.get("channel")}
us = sorted(
    (c for c in channels
     if c.get("country") == "US" and not c.get("is_nsfw") and not c.get("closed") and c["id"] in have),
    key=lambda c: c["name"],
)

print("=== PLUTO ===")
for c in us:
    if "pluto" in c["name"].lower():
        print("  " + c["name"])

print("=== KIDS/FAMILY CATEGORY ===")
for c in us:
    cats = c.get("categories") or []
    if {"kids", "family", "animation"} & set(cats):
        print(f"  {c['name']}  [{','.join(cats)}]")

print("=== SERIES/MOVIES CATEGORY ===")
for c in us:
    cats = c.get("categories") or []
    if {"series", "classic"} & set(cats):
        print(f"  {c['name']}  [{','.join(cats)}]")

print("=== SPORTS CATEGORY ===")
for c in us:
    if "sports" in (c.get("categories") or []):
        print(f"  {c['name']}")
