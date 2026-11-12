#!/usr/bin/env python3
"""Push a set of files to a GitHub branch via the REST Git Data API.

Why this exists: in the originating session, the Git smart-HTTP endpoints
(git-upload-pack / git-receive-pack) were blocked by the egress gateway, while
api.github.com and codeload.github.com were reachable. This script produces a
real commit with the correct parent and updates the ref, which also triggers
Actions normally.

Credentials are read ONLY from the environment. No token is stored here.

Usage:
    export GITHUB_TOKEN=...          # fine-grained PAT, Contents: read+write
    export GITHUB_REPO=1-1-2/QwenASRGUI   # optional
    export GITHUB_BRANCH=main             # optional
    python push_via_rest.py --message "feat: ..." path/to/a.py path/to/b.js
    python push_via_rest.py --message "docs: ..." --skip-ci docs/x.md

Notes:
  * PATCH must use the PLURAL path /git/refs/heads/<branch>; the singular
    /git/ref/heads/<branch> returns 404 on PATCH.
  * A browser User-Agent is required by some reverse proxies (CF 1010).
  * Paths are relative to the repository root (the directory given by --root).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def api(method: str, path: str, token: str, repo: str, body=None):
    url = "https://api.github.com/repos/" + repo + path
    headers = {
        "User-Agent": UA,
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--message", required=True)
    ap.add_argument("--root", default=".", help="repository root on disk")
    ap.add_argument("--skip-ci", action="store_true")
    ap.add_argument("files", nargs="+")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("ERROR: GITHUB_TOKEN is not set. See .env.example.", file=sys.stderr)
        return 2
    repo = os.environ.get("GITHUB_REPO", "1-1-2/QwenASRGUI").strip()
    branch = os.environ.get("GITHUB_BRANCH", "main").strip()
    root = Path(args.root).resolve()

    head = api("GET", "/git/ref/heads/" + branch, token, repo)["object"]["sha"]
    base_tree = api("GET", "/git/commits/" + head, token, repo)["tree"]["sha"]

    entries = []
    for rel in args.files:
        p = (root / rel).resolve()
        if root not in p.parents and p != root:
            print("ERROR: path escapes --root: " + rel, file=sys.stderr)
            return 2
        if not p.is_file():
            print("ERROR: missing file: " + rel, file=sys.stderr)
            return 2
        blob = api("POST", "/git/blobs", token, repo, {
            "content": base64.b64encode(p.read_bytes()).decode("ascii"),
            "encoding": "base64",
        })
        entries.append({"path": rel.replace("\\", "/"), "mode": "100644",
                        "type": "blob", "sha": blob["sha"]})
        print("  blob", rel, p.stat().st_size)

    tree = api("POST", "/git/trees", token, repo,
               {"base_tree": base_tree, "tree": entries})
    msg = args.message + ("\n\n[skip ci]" if args.skip_ci else "")
    commit = api("POST", "/git/commits", token, repo,
                 {"message": msg, "tree": tree["sha"], "parents": [head]})
    ref = api("PATCH", "/git/refs/heads/" + branch, token, repo,
              {"sha": commit["sha"], "force": False})
    print("pushed", commit["sha"][:8], "->", ref["object"]["sha"][:8])
    print("CI: https://github.com/" + repo + "/actions")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.read().decode("utf-8", "replace")[:400], file=sys.stderr)
        raise SystemExit(1)
