#!/usr/bin/env python3
"""Read-only shared PR observations, using GitHub CLI's HTTP cache."""

import argparse
import datetime as dt
import email.parser
import email.utils
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


TTL = 30
WATCH_TTL = 5
QUERY = """query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      number url state isDraft headRefOid baseRefOid baseRefName
      mergeable mergeStateStatus reviewDecision
      baseRef { target { oid } }
      commits(last: 1) { nodes { commit {
        oid
        statusCheckRollup {
          state
          contexts(first: 100, after: $cursor) {
            totalCount pageInfo { hasNextPage endCursor }
            nodes {
              __typename
              ... on CheckRun { id name status conclusion detailsUrl }
              ... on StatusContext { id context state targetUrl }
            }
          }
        }
      } } }
    }
  }
}"""


class StateError(Exception):
    pass


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise StateError("Cache directory must be owned by the current user and not a symlink")
    path.chmod(0o700)
    return path


def cache_root():
    return private_directory(Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "pr-state")


def parse_response(raw):
    parts = re.split(r"\r?\n\r?\n", raw, maxsplit=1)
    if len(parts) != 2 or not re.match(r"HTTP/\S+ 200\b", parts[0]):
        raise StateError("GitHub returned an invalid HTTP response")
    headers = email.parser.Parser().parsestr(parts[0].partition("\n")[2])
    try:
        data = json.loads(parts[1])
    except json.JSONDecodeError as error:
        raise StateError("GitHub returned invalid JSON") from error
    if not isinstance(data, dict):
        raise StateError("GitHub returned an invalid GraphQL response")
    if data.get("errors"):
        raise StateError("GitHub GraphQL returned errors; no fresh state is available")
    observed = None
    try:
        date = email.utils.parsedate_to_datetime(headers["Date"])
        if date.tzinfo is None:
            date = date.replace(tzinfo=dt.timezone.utc)
        if date.timestamp() <= time.time() + 5:
            observed = date.timestamp()
    except (TypeError, ValueError, OverflowError):
        pass
    return data, observed


def check_page(data):
    try:
        pr = data["data"]["repository"]["pullRequest"]
        if not pr:
            raise KeyError("pullRequest")
        commit = pr["commits"]["nodes"][-1]["commit"]
        rollup = commit.get("statusCheckRollup") or {}
        contexts = rollup.get("contexts") or {}
        if (not isinstance(pr["headRefOid"], str) or not isinstance(commit["oid"], str)
                or not isinstance(contexts, dict)):
            raise TypeError("Invalid PR or check data")
        return pr, commit, rollup, contexts
    except (KeyError, IndexError, TypeError, AttributeError) as error:
        raise StateError("PR unavailable or GitHub returned incomplete PR data") from error


def normalize(data, observed, slug):
    try:
        pr, commit, rollup, contexts = check_page(data)
        nodes = contexts.get("nodes") or []
        incomplete = (commit["oid"] != pr["headRefOid"]
                      or contexts.get("pageInfo", {}).get("hasNextPage", False)
                      or contexts.get("totalCount", len(nodes)) != len(nodes))
        counts = dict(passed=0, neutral=0, skipped=0, failed=0, queued=0, running=0, pending=0, unknown=0)
        failures = []
        for item in nodes:
            if not item:
                counts["unknown"] += 1
                continue
            if item.get("__typename") == "CheckRun":
                name = item.get("name", "unnamed check")
                conclusion = item.get("conclusion")
                status = item.get("status")
                if status == "COMPLETED":
                    bucket = {"SUCCESS": "passed", "NEUTRAL": "neutral", "SKIPPED": "skipped"}.get(
                        conclusion, "failed" if conclusion in {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}
                        else "unknown")
                else:
                    bucket = {"IN_PROGRESS": "running", "QUEUED": "queued", "WAITING": "queued", "REQUESTED": "queued", "PENDING": "pending"}.get(status, "unknown")
            elif item.get("__typename") == "StatusContext":
                name = item.get("context", "unnamed status")
                bucket = {"SUCCESS": "passed", "FAILURE": "failed", "ERROR": "failed", "PENDING": "pending", "EXPECTED": "pending"}.get(item.get("state"), "unknown")
            else:
                name, bucket = "unknown context", "unknown"
            counts[bucket] += 1
            if bucket == "failed":
                failures.append(name)
        aggregate = rollup.get("state")
        if incomplete or counts["unknown"]:
            ci = "CHECKS_INCOMPLETE"
        elif counts["failed"] or aggregate in {"FAILURE", "ERROR"}:
            ci = "CHECKS_FAILED"
        elif counts["running"]:
            ci = "CHECKS_RUNNING"
        elif counts["queued"]:
            ci = "CHECKS_QUEUED"
        elif counts["pending"] or aggregate in {"PENDING", "EXPECTED"}:
            ci = "CHECKS_PENDING"
        elif nodes and aggregate == "SUCCESS":
            ci = "CHECKS_PASS"
        elif not nodes and aggregate is None:
            ci = "NO_CHECKS"
        else:
            ci = "CHECKS_UNKNOWN"
        return {
            "pr": f"{slug}#{pr['number']}", "url": pr["url"],
            "head": pr["headRefOid"], "recorded_base": pr["baseRefOid"],
            "target_branch": pr["baseRefName"],
            "target_tip": ((pr.get("baseRef") or {}).get("target") or {}).get("oid"),
            "state": pr["state"], "draft": pr["isDraft"],
            "mergeable": pr["mergeable"], "merge_state": pr["mergeStateStatus"],
            "review": pr.get("reviewDecision") or "UNKNOWN", "ci": ci,
            "checks": counts, "failed_checks": failures, "incomplete": bool(incomplete),
            "observed_at": observed,
        }
    except (KeyError, IndexError, TypeError, AttributeError) as error:
        raise StateError("PR unavailable or GitHub returned incomplete PR data") from error


def observe(slug, number, *, fresh=False, timeout=45, cache_ttl=TTL):
    root = cache_root()
    response_root = private_directory(root / "responses")
    host = os.environ.get("GH_HOST", "github.com")
    key = hashlib.sha256(f"{host}/{slug.lower()}#{number}".encode()).hexdigest()
    env = os.environ.copy()
    env["XDG_CACHE_HOME"] = str(response_root)
    env["GH_PROMPT_DISABLED"] = "1"
    # gh keys cached responses by Authorization and request body, keeping profiles separate.
    command = ["gh", "api", "--hostname", host, "graphql", "--include", "--input", "-",
               "--cache", "1ns" if fresh else f"{cache_ttl}s"]
    slug = slug.lower()
    owner, repo = slug.split("/")
    deadline = time.monotonic() + timeout
    lock_fd = os.open(root / f"{key}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StateError("Timed out waiting for the shared refresh")
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        first, cursor, expected = None, None, None
        all_nodes, dates, seen_cursors, seen_ids = [], [], set(), set()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StateError("GitHub pagination exceeded the request deadline")
            payload = json.dumps({"query": QUERY, "variables": {
                "owner": owner, "repo": repo, "number": number, "cursor": cursor,
            }}, sort_keys=True)
            try:
                result = subprocess.run(command, input=payload, capture_output=True, text=True,
                                        env=env, timeout=remaining)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise StateError("GitHub request could not complete; no fresh state is available") from error
            if result.returncode:
                raise StateError(f"GitHub request failed (exit {result.returncode}); no fresh state is available")
            data, observed = parse_response(result.stdout)
            pr, commit, rollup, contexts = check_page(data)
            total = contexts.get("totalCount", 0)
            signature = (pr["headRefOid"], commit["oid"], rollup.get("state"), total)
            if pr["headRefOid"] != commit["oid"] or (expected is not None and signature != expected):
                raise StateError("PR head or check state changed during pagination; refresh again")
            if first is None:
                first, first_contexts, expected = data, contexts, signature
            nodes = contexts.get("nodes") or []
            if not isinstance(total, int) or total < 0 or not isinstance(nodes, list):
                raise StateError("GitHub returned inconsistent check pagination")
            for item in nodes:
                identity = item.get("id") if isinstance(item, dict) else None
                if not isinstance(identity, str) or not identity or identity in seen_ids:
                    raise StateError("GitHub returned missing or duplicate check identities")
                seen_ids.add(identity)
            all_nodes.extend(nodes)
            dates.append(observed)
            page_info = contexts.get("pageInfo") or {}
            if not isinstance(page_info, dict):
                raise StateError("GitHub returned invalid check pagination")
            if len(all_nodes) > total:
                raise StateError("GitHub returned inconsistent check pagination")
            if not page_info.get("hasNextPage", False):
                if len(all_nodes) != total:
                    raise StateError("GitHub returned incomplete check pagination")
                first_contexts["nodes"] = all_nodes
                first_contexts["pageInfo"] = {"hasNextPage": False}
                oldest = min(dates) if all(date is not None for date in dates) else None
                return normalize(first, oldest, slug)
            next_cursor = page_info.get("endCursor")
            if (not nodes or len(all_nodes) >= total or not isinstance(next_cursor, str)
                    or not next_cursor or next_cursor in seen_cursors):
                raise StateError("GitHub returned missing or repeated check cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor


def fingerprint(state):
    return {key: value for key, value in state.items() if key != "observed_at"}


def wait_for_change(slug, number, initial, timeout):
    deadline = time.monotonic() + timeout
    state = initial
    while time.monotonic() < deadline:
        age = max(0, time.time() - state["observed_at"]) if state["observed_at"] is not None else 0
        delay = min(WATCH_TTL, max(0.25, WATCH_TTL - age + 0.1), deadline - time.monotonic())
        time.sleep(max(0, delay))
        if time.monotonic() >= deadline:
            break
        state = observe(slug, number, timeout=max(0.01, deadline - time.monotonic()), cache_ttl=WATCH_TTL)
        if fingerprint(state) != fingerprint(initial):
            return state, True
    return state, False


def render(state):
    def short(value):
        return value[:10] if value else "unknown"
    age = max(0, int(time.time() - state["observed_at"])) if state["observed_at"] is not None else None
    flags = " draft" if state["draft"] else ""
    failures = " failed=" + json.dumps(state["failed_checks"], ensure_ascii=True) if state["failed_checks"] else ""
    return (f"{state['pr']} head={short(state['head'])} base={short(state['recorded_base'])} "
            f"tip={short(state['target_tip'])} {state['state']}{flags} ci={state['ci']} "
            f"review={state['review']} merge={state['merge_state']} "
            f"checks={','.join(f'{key}:{value}' for key, value in state['checks'].items() if value)} "
            f"age={str(age) + 's' if age is not None else 'unknown'}{failures}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", metavar="OWNER/REPO")
    parser.add_argument("number", type=int)
    parser.add_argument("--wait", action="store_true", help="Wait silently for a meaningful state change, sharing 5-second refreshes")
    parser.add_argument("--timeout", type=float, default=50, help="Maximum total seconds with --wait, including initial fetch (default: 50)")
    parser.add_argument("--fresh", action="store_true", help="Fetch now and replace the shared cached observation")
    parser.add_argument("--json", action="store_true", help="Print one normalized JSON object")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository) or args.number <= 0:
        parser.error("Expected OWNER/REPO and a positive PR number")
    if not 0 < args.timeout <= 3600:
        parser.error("--timeout must be greater than 0 and at most 3600 seconds")
    os.umask(0o077)
    try:
        deadline = time.monotonic() + args.timeout
        state = observe(args.repository, args.number, fresh=args.fresh,
                        timeout=min(45, args.timeout) if args.wait else 45,
                        cache_ttl=WATCH_TTL if args.wait else TTL)
        if args.wait:
            state, changed = wait_for_change(args.repository, args.number, state, max(0, deadline - time.monotonic()))
            state["changed"] = changed
        if args.json:
            print(json.dumps(state, separators=(",", ":"), ensure_ascii=True))
        else:
            print(render(state))
        return 0
    except (StateError, OSError) as error:
        message = str(error) if isinstance(error, StateError) else "Local cache is unavailable"
        if args.json:
            print(json.dumps({"pr": f"{args.repository}#{args.number}", "state": "ERROR", "error": message}))
        else:
            print(f"{args.repository}#{args.number} ERROR: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
