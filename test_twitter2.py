#!/usr/bin/env python3
"""Twitter access - cari + like + quote via cookies + bearer token"""
import requests, json, re, time, os, sys

# ---------- CONFIG ----------
COOKIE_FILE = "/home/hermes/x-monitor/x_cookies.txt"
BEARER_TOKEN = "AAAAA" + "AAAA" * 10 + "..."  # akan diisi dari file
# ----------------------------

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})

# Load cookies
with open(COOKIE_FILE) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split('\t')
        if len(parts) >= 7:
            session.cookies.set(parts[5], parts[6], domain=parts[0], path=parts[2])

# Set CSRF token
csrf = session.cookies.get("ct0", domain=".x.com") or session.cookies.get("ct0", domain="x.com")
if csrf:
    session.headers.update({"X-Csrf-Token": csrf, "Content-Type": "application/json"})

# Test 1: Akses homepage
r = session.get("https://x.com/", timeout=15)
print(f"1. Homepage: {r.status_code}")
names = set(re.findall(r'"screen_name":"([^"]+)"', r.text))
print(f"   Akun: {names}")

# Test 2: Cari via GraphQL
headers_gql = {
    "Authorization": f"Bearer {BEARER_TOKEN}",
    "Content-Type": "application/json",
}

# Coba search tweets
search_vars = {
    "rawQuery": "cosmos",
    "count": 5,
    "product": "Top",
}
gql_search = "https://x.com/i/api/graphql/3XjoWG1BJ4kMDd-BVBH1gA/SearchTimeline"

r2 = session.get(gql_search, params={"variables": json.dumps(search_vars)}, headers=headers_gql, timeout=15)
print(f"2. GraphQL search: {r2.status_code}")
if r2.status_code == 200:
    data = r2.json()
    print(f"   Response keys: {list(data.keys())}")
elif r2.text:
    print(f"   Response: {r2.text[:200]}")

# Test 3: Coba tweet detail
tweet_vars = {"tweetId": "123456789", "count": 1}
gql_tweet = "https://x.com/i/api/graphql/Ax0sB9ZMRvFJpEPcnzayPA/TweetDetail"
r3 = session.get(gql_tweet, params={"variables": json.dumps(tweet_vars)}, headers=headers_gql, timeout=15)
print(f"3. Tweet detail: {r3.status_code}")

# Test 4: Coba search dgn cookies saja (tanpa bearer)
r4 = session.get("https://x.com/i/api/2/search/adaptive.json?q=test&count=1", timeout=15)
print(f"4. Adaptive search: {r4.status_code} - {r4.text[:100] if r4.text else 'empty'}")
