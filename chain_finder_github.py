#!/usr/bin/env python3
"""
chain_finder_github.py — Pantau PR chain baru di repositori Cosmos
Output: kirim ke grup Telegram via stdout (wrapper/format box)

Repositori yang dipantau:
  - chainapsis/keplr-chain-registry
  - cosmos/chain-registry
  - ping-pub/mainnet
  - ping-pub/testnet
"""

import requests, json, os, sys
from datetime import datetime

STATE_FILE = os.path.expanduser("~/.hermes/x-monitor/seen_prs.json")
REPOS = [
    "chainapsis/keplr-chain-registry",
    "cosmos/chain-registry",
    "ping-pub/mainnet",
    "ping-pub/testnet",
]

os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)

def load_seen():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"prs": []}

def save_seen(data):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_pr_files(pr_url):
    """Ambil daftar file yang diubah di PR"""
    try:
        r = requests.get(pr_url + "/files", timeout=10)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return []

def is_chain_pr(files, repo):
    """Deteksi apakah PR ini menambahkan chain baru"""
    keywords = ["chain.json", "assetlist.json", "chain.schema.json"]
    for f in files:
        fname = f["filename"]
        for kw in keywords:
            if kw in fname:
                # Pastikan bukan edit file existing (file baru = addition)
                if f["status"] == "added" or "new folder" in f.get("patch", "").lower() or f["status"] == "modified":
                    return True
    return False

def extract_chain_name(files, pr_title):
    """Coba tebak nama chain dari file path atau judul PR"""
    import re
    # Dari file path: cosmos/chain-registry/nibiru/chain.json → nibiru
    for f in files:
        parts = f["filename"].split("/")
        # Cari folder setelah chain-registry/
        for i, p in enumerate(parts):
            if p in ("chain-registry", "keplr-chain-registry", "mainnet", "testnet") and i+1 < len(parts):
                candidate = parts[i+1]
                if candidate and not candidate.endswith(".json") and not candidate.startswith("."):
                    return candidate
    # Fallback: dari judul PR "Add Nibiru chain" → Nibiru
    m = re.search(r'(?:add|Add|ADD)\s+([A-Za-z0-9_-]+)', pr_title)
    if m:
        return m.group(1)
    return None

def main():
    seen = load_seen()
    seen_prs = set(seen["prs"])

    found = []
    headers = {"Accept": "application/vnd.github.v3+json"}
    # Optional: token dari env
    if "GITHUB_TOKEN" in os.environ:
        headers["Authorization"] = f"token {os.environ['GITHUB_TOKEN']}"

    for repo in REPOS:
        url = f"https://api.github.com/repos/{repo}/pulls?state=open&sort=created&direction=desc"
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code != 200:
                print(f"[WARN] {repo}: HTTP {r.status_code} — {r.json().get('message','')}", file=sys.stderr)
                continue
            prs = r.json()
        except Exception as e:
            print(f"[ERROR] {repo}: {e}", file=sys.stderr)
            continue

        for pr in prs:
            if pr["number"] in seen_prs:
                continue

            files = get_pr_files(pr["url"])
            if not files:
                continue

            if not is_chain_pr(files, repo):
                continue

            chain_name = extract_chain_name(files, pr["title"]) or "?"
            now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

            found.append({
                "repo": repo,
                "pr_number": pr["number"],
                "title": pr["title"],
                "url": pr["html_url"],
                "chain_name": chain_name,
                "created_at": pr["created_at"],
                "user": pr["user"]["login"],
            })
            seen_prs.add(pr["number"])

    # Simpan state
    save_seen({"prs": list(seen_prs)})

    # Output
    if not found:
        print("[⏳] Tidak ada PR chain baru.")
        return

    for item in found:
        print(f"🔗 {item['repo']}  #{item['pr_number']}")
        print(f"   📝 {item['title']}")
        print(f"   🆔 Chain: {item['chain_name']}")
        print(f"   👤 Oleh: {item['user']}")
        print(f"   🔍 {item['url']}")
        print()

if __name__ == "__main__":
    main()
