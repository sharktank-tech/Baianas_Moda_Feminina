#!/usr/bin/env python3
"""Cria PR via API do GitHub. Uso: create_pr.py <branch> <title> <body_file>."""
import json
import sys
import urllib.request
from pathlib import Path

REPO = "sharktank-tech/PolloModas"
line = Path("/home/opencode/.git-credentials").read_text().splitlines()[0]
token = line.split("//", 1)[1].split("@", 1)[0].split(":", 1)[1]

branch, title, body_file = sys.argv[1], sys.argv[2], sys.argv[3]
body = Path(body_file).read_text(encoding="utf-8")

req = urllib.request.Request(
    f"https://api.github.com/repos/{REPO}/pulls",
    data=json.dumps(
        {"title": title, "head": branch, "base": "master", "body": body}
    ).encode(),
    headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "opencode",
    },
    method="POST",
)
with urllib.request.urlopen(req, timeout=60) as resp:
    data = json.load(resp)
print(data["html_url"])
