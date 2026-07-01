#!/usr/bin/env python3
"""Test cookies Twitter untuk search & like"""
import requests, json

COOKIE_FILE = "/home/hermes/x-monitor/x_cookies.txt"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

session = requests.Session()

# Load cookies dari file Netscape
with open(COOKIE_FILE) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split('\t')
        if len(parts) >= 7:
            domain, _, path, secure, expires, name, value = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], parts[6]
            session.cookies.set(name, value, domain=domain, path=path)

session.headers.update({
    "User-Agent": USER_AGENT,
    "X-Csrf-Token": session.cookies.get("ct0", domain=".x.com"),
    "Content-Type": "application/json",
})

# Test 1: Akses homepage
r = session.get("https://x.com/", timeout=15)
print(f"Homepage: {r.status_code}")

# Cari screen_name
import re
screen_names = re.findall(r'"screen_name":"([^"]+)"', r.text)
print(f"Screen names: {list(set(screen_names))[:3]}")

# Test 2: Coba search via API
search_url = "https://api.twitter.com/1.1/search/tweets.json?q=cosmos&count=1"
r2 = session.get(search_url, timeout=15)
if r2.status_code == 200:
    data = r2.json()
    tweets = data.get('statuses', [])
    print(f"Search results: {len(tweets)} tweets")
    for t in tweets[:1]:
        print(f"  {t.get('text','')[:80]}")
elif r2.status_code == 429:
    print("Search: RATE LIMITED (429)")
else:
    print(f"Search: {r2.status_code} - {r2.text[:200]}")

# Test 3: Coba GraphQL search
gql_url = "https://x.com/i/api/graphql/3XjoWG1BJ4kMDd-BVBH1gA/SearchTimeline"
gql_vars = {"rawQuery":"cosmos","count":1,"product":"Top"}
headers = {"Authorization": "Bearer AAAAAAAAAAAAAAAA...AAAAAAAAAAAAAA%3D......"}
r3 = session.get(gql_url, params={"variables": json.dumps(gql_vars)}, headers=headers, timeout=15)
print(f"GraphQL search: {r3.status_code}")
if r3.status_code == 200:
    print(f"  Data: {json.dumps(r3.json())[:200]}")
elif r3.text:
    print(f"  Response: {r3.text[:200]}")
else:
    print("  Empty response")
