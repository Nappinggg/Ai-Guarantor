"""Checkable evidence: is the PR merged, and did CI pass?"""
import os
import re
import time

import httpx

PR_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")
CACHE_TTL = 60  # seconds; keeps demo re-runs well under GitHub's unauthenticated rate limit
_cache: dict[str, tuple[float, dict]] = {}


def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json"}
    if os.getenv("GITHUB_TOKEN"):
        h["Authorization"] = f"Bearer {os.getenv('GITHUB_TOKEN')}"
    return h


def check_pr(url: str) -> dict:
    m = PR_RE.search(url or "")
    if m and m.group(0) in _cache and time.time() - _cache[m.group(0)][0] < CACHE_TTL:
        return _cache[m.group(0)][1]
    result = _fetch_pr(url)
    if m and result["ok"]:
        _cache[m.group(0)] = (time.time(), result)
    return result


def _fetch_pr(url: str) -> dict:
    m = PR_RE.search(url or "")
    if not m:
        return {"ok": False, "error": "Not a GitHub pull request URL"}
    owner, repo, number = m.groups()
    api = f"https://api.github.com/repos/{owner}/{repo}"
    with httpx.Client(headers=_headers(), timeout=15) as http:
        r = http.get(f"{api}/pulls/{number}")
        if r.status_code != 200:
            return {"ok": False, "error": f"GitHub API {r.status_code}: {r.json().get('message', '')}"}
        pr = r.json()
        ci = "none"
        runs = http.get(f"{api}/commits/{pr['head']['sha']}/check-runs")
        if runs.status_code == 200 and runs.json().get("check_runs"):
            conclusions = {cr.get("conclusion") for cr in runs.json()["check_runs"]}
            ci = "failure" if conclusions & {"failure", "cancelled", "timed_out"} else (
                "success" if conclusions <= {"success", "skipped", "neutral"} else "pending")
        files = http.get(f"{api}/pulls/{number}/files")
        file_names = [f["filename"] for f in files.json()][:30] if files.status_code == 200 else []
    return {
        "ok": True,
        "url": pr["html_url"],
        "number": pr["number"],
        "title": pr["title"],
        "body": (pr.get("body") or "")[:1500],
        "merged": bool(pr.get("merged")),
        "merged_at": pr.get("merged_at"),
        "state": pr["state"],
        "ci": ci,
        "changed_files": pr.get("changed_files"),
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
        "files": file_names,
    }
