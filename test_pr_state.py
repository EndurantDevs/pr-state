import copy
import datetime as dt
import email.utils
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location("pr_state", ROOT / "pr_state.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture(*, status="COMPLETED", conclusion="SUCCESS", rollup="SUCCESS"):
    head = "a" * 40
    return {"data": {"repository": {"pullRequest": {
        "number": 123, "url": "https://github.com/example/api/pull/123", "state": "OPEN",
        "isDraft": False, "headRefOid": head, "baseRefOid": "b" * 40,
        "baseRefName": "dev", "baseRef": {"target": {"oid": "c" * 40}},
        "mergeable": "MERGEABLE", "mergeStateStatus": "BEHIND", "reviewDecision": "APPROVED",
        "commits": {"nodes": [{"commit": {"oid": head, "statusCheckRollup": {
            "state": rollup, "contexts": {"totalCount": 1, "pageInfo": {"hasNextPage": False},
                "nodes": [{"__typename": "CheckRun", "id": "check-0", "name": "CI", "status": status,
                           "conclusion": conclusion, "detailsUrl": "https://github.com/example/api/actions/runs/1"}]}
        }}}]}
    }}}}


def pull(data):
    return data["data"]["repository"]["pullRequest"]


class NormalizationTests(unittest.TestCase):
    def test_ci_states_and_behind_are_distinct(self):
        for status, conclusion, rollup, expected in [
            ("COMPLETED", "SUCCESS", "SUCCESS", "CHECKS_PASS"),
            ("COMPLETED", "FAILURE", "FAILURE", "CHECKS_FAILED"),
            ("COMPLETED", "ACTION_REQUIRED", "FAILURE", "CHECKS_FAILED"),
            ("IN_PROGRESS", None, "PENDING", "CHECKS_RUNNING"),
            ("QUEUED", None, "PENDING", "CHECKS_QUEUED"),
            ("PENDING", None, "PENDING", "CHECKS_PENDING"),
            ("UNKNOWN", None, "PENDING", "CHECKS_INCOMPLETE"),
        ]:
            with self.subTest(expected=expected):
                result = module.normalize(fixture(status=status, conclusion=conclusion, rollup=rollup), 0, "example/api")
                self.assertEqual(result["ci"], expected)
                self.assertEqual(result["merge_state"], "BEHIND")
                self.assertEqual(result["recorded_base"], "b" * 40)
                self.assertEqual(result["target_tip"], "c" * 40)

    def test_unknown_truncated_raced_and_absent_checks(self):
        for kind in ["truncated", "count", "raced", "unknown"]:
            data = fixture()
            pr = pull(data)
            commit = pr["commits"]["nodes"][0]["commit"]
            contexts = commit["statusCheckRollup"]["contexts"]
            if kind == "truncated": contexts["pageInfo"]["hasNextPage"] = True
            if kind == "count": contexts["totalCount"] = 101
            if kind == "raced": commit["oid"] = "d" * 40
            if kind == "unknown": contexts["nodes"][0]["conclusion"] = None
            self.assertEqual(module.normalize(data, None, "example/api")["ci"], "CHECKS_INCOMPLETE")
        data = fixture()
        pull(data)["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = None
        self.assertEqual(module.normalize(data, None, "example/api")["ci"], "NO_CHECKS")

    def test_skipped_and_neutral_checks_are_not_reported_as_passed(self):
        for conclusion, field in [("SKIPPED", "skipped"), ("NEUTRAL", "neutral")]:
            result = module.normalize(fixture(conclusion=conclusion), None, "example/api")
            self.assertEqual(result["checks"]["passed"], 0)
            self.assertEqual(result["checks"][field], 1)
            self.assertEqual(result["ci"], "CHECKS_PASS")
            self.assertIn(field + ":1", module.render(result))

    def test_graphql_and_incomplete_data_are_errors(self):
        raw = "HTTP/2.0 200 OK\r\nDate: Thu, 05 Nov 2026 12:00:00 GMT\r\n\r\n" + json.dumps({"errors": [{"message": "fixture"}]})
        with self.assertRaises(module.StateError): module.parse_response(raw)
        with self.assertRaises(module.StateError): module.parse_response("HTTP/2.0 200 OK\n\n[]")
        with self.assertRaises(module.StateError): module.normalize({"data": {"repository": None}}, None, "example/api")

    def test_wait_returns_on_change_and_times_out_silently(self):
        initial = module.normalize(fixture(status="QUEUED", conclusion=None, rollup="PENDING"), time.time() - 30, "example/api")
        changed = module.normalize(fixture(), time.time(), "example/api")
        with patch.object(module, "observe", return_value=changed):
            state, did_change = module.wait_for_change("example/api", 123, initial, 1)
            self.assertTrue(did_change)
            self.assertEqual(state["ci"], "CHECKS_PASS")
        initial["observed_at"] = time.time()
        with patch.object(module, "observe") as observe:
            start = time.monotonic()
            state, did_change = module.wait_for_change("example/api", 123, initial, 0.05)
            self.assertFalse(did_change)
            self.assertLess(time.monotonic() - start, 0.2)
            observe.assert_not_called()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        request_body = bytearray()
        if self.headers.get("Transfer-Encoding") == "chunked":
            while True:
                size = int(self.rfile.readline().split(b";", 1)[0], 16)
                if size == 0:
                    while self.rfile.readline().strip():
                        pass
                    break
                request_body.extend(self.rfile.read(size))
                self.rfile.readline()
        else:
            request_body.extend(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        self.cursor = json.loads(request_body)["variables"].get("cursor")
        with self.server.guard:
            self.server.requests += 1
            payload = self.server.pages.get(self.cursor, self.server.payload)
            body = json.dumps(payload).encode()
            error = self.server.error
        time.sleep(self.server.delay)
        self.send_response(error or 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass

    def date_time_string(self, timestamp=None):
        return email.utils.formatdate(time.time() - self.server.date_offsets.get(self.cursor, 0), usegmt=True)


class NativeCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.real_gh = shutil.which("gh")
        if not cls.real_gh: raise unittest.SkipTest("GitHub CLI is required")

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="pr-state-test-")
        self.root = Path(self.scratch.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.requests = 0
        self.server.payload = fixture()
        self.server.pages = {}
        self.server.date_offsets = {}
        self.server.error = None
        self.server.delay = 0.1
        self.server.guard = threading.Lock()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        bindir = self.root / "bin"
        bindir.mkdir()
        wrapper = bindir / "gh"
        wrapper.write_text(f'''#!{sys.executable}
import os, sys
args = sys.argv[1:]
i = args.index("--hostname")
del args[i:i+2]
args[1] = os.environ["PR_STATE_FIXTURE_URL"]
args += ["-H", "Authorization: token " + os.environ["GH_TOKEN"]]
os.execv({self.real_gh!r}, [{self.real_gh!r}, *args])
''')
        wrapper.chmod(0o700)
        self.env = os.environ.copy()
        self.env.update({"PATH": str(bindir) + os.pathsep + self.env["PATH"],
                         "XDG_CACHE_HOME": str(self.root / "cache"), "GH_TOKEN": "synthetic-account-a",
                         "PR_STATE_FIXTURE_URL": f"http://127.0.0.1:{self.server.server_port}/graphql"})
        self.env.pop("GH_HOST", None)
        self.command = [sys.executable, str(ROOT / "pr_state.py"), "example/api", "123", "--json"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.scratch.cleanup()

    def call(self, *args, env=None, expected=0):
        result = subprocess.run([*self.command, *args], env=env or self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, expected, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def test_concurrent_readers_share_one_native_response(self):
        processes = [subprocess.Popen(self.command, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(6)]
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr or stdout)
            self.assertEqual(json.loads(stdout)["ci"], "CHECKS_PASS")
        self.assertEqual(self.server.requests, 1)
        cache = self.root / "cache/pr-state"
        self.assertEqual(cache.stat().st_mode & 0o777, 0o700)
        for path in cache.rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o077, 0, str(path))

    def test_native_ttl_expiry_and_auth_isolation(self):
        self.call()
        self.call()
        self.assertEqual(self.server.requests, 1)
        with patch.dict(os.environ, self.env):
            module.observe("Example/API", 123)
        self.assertEqual(self.server.requests, 1)
        native_cache = self.root / "cache/pr-state/responses/gh"
        for path in native_cache.rglob("*"):
            if path.is_file(): os.utime(path, (time.time() - 31, time.time() - 31))
        self.call()
        self.assertEqual(self.server.requests, 2)
        other = dict(self.env, GH_TOKEN="synthetic-account-b", GH_CONFIG_DIR=str(self.root / "other-gh-profile"))
        self.call(env=other)
        self.assertEqual(self.server.requests, 3)
        self.call()
        self.assertEqual(self.server.requests, 3)

    def test_fresh_observes_changed_head_and_updates_shared_cache(self):
        self.call()
        new = fixture()
        pull(new)["headRefOid"] = "d" * 40
        pull(new)["commits"]["nodes"][0]["commit"]["oid"] = "d" * 40
        self.server.payload = new
        self.assertEqual(self.call()["head"], "a" * 40)
        self.assertEqual(self.call("--fresh")["head"], "d" * 40)
        self.assertEqual(self.call()["head"], "d" * 40)
        self.assertEqual(self.server.requests, 2)

    def test_three_watchers_share_fast_refresh_and_update_passive_reader(self):
        self.server.payload = fixture(status="QUEUED", conclusion=None, rollup="PENDING")
        processes = [subprocess.Popen([*self.command, "--wait", "--timeout", "9"],
                     env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for _ in range(3)]
        def cleanup():
            for process in processes:
                if process.poll() is None: process.kill()
                process.communicate()
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 3
        while self.server.requests == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.server.requests, 1)
        with self.server.guard:
            self.server.payload = fixture()
        start = time.monotonic()
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr or stdout)
            state = json.loads(stdout)
            self.assertTrue(state["changed"])
            self.assertEqual(state["ci"], "CHECKS_PASS")
        self.assertLess(time.monotonic() - start, 8)
        self.assertEqual(self.server.requests, 2)
        self.assertEqual(self.call()["ci"], "CHECKS_PASS")
        self.assertEqual(self.server.requests, 2)

    def test_api_and_cached_graphql_errors_are_never_fresh_success(self):
        self.server.error = 400
        result = self.call(expected=1)
        self.assertEqual(result["state"], "ERROR")
        self.server.error = None
        self.server.payload = {"errors": [{"message": "synthetic error"}]}
        result = self.call("--fresh", expected=1)
        self.assertEqual(result["state"], "ERROR")
        self.assertNotIn("synthetic-account", json.dumps(result))

    def test_wait_cli_has_one_output_and_bounded_timeout(self):
        start = time.monotonic()
        state = self.call("--wait", "--timeout", "0.8")
        self.assertFalse(state["changed"])
        self.assertLess(time.monotonic() - start, 1.5)

    def test_wait_timeout_bounds_initial_request(self):
        self.server.delay = 1
        start = time.monotonic()
        state = self.call("--wait", "--timeout", "0.2", expected=1)
        self.assertEqual(state["state"], "ERROR")
        self.assertLess(time.monotonic() - start, 0.8)

    def test_bad_cache_location_reports_json_error(self):
        invalid = self.root / "not-a-directory"
        invalid.write_text("fixture")
        state = self.call(env=dict(self.env, XDG_CACHE_HOME=str(invalid)), expected=1)
        self.assertEqual(state["state"], "ERROR")
        self.assertEqual(state["error"], "Local cache is unavailable")
        self.assertEqual(self.server.requests, 0)

    def test_malformed_success_response_returns_json_error(self):
        for kind in ["missing-head", "missing-commit", "bad-contexts", "bad-page-info"]:
            with self.subTest(kind=kind):
                data = fixture()
                pr = pull(data)
                commit = pr["commits"]["nodes"][0]["commit"]
                if kind == "missing-head": del pr["headRefOid"]
                if kind == "missing-commit": del commit["oid"]
                if kind == "bad-contexts": commit["statusCheckRollup"]["contexts"] = ["invalid"]
                if kind == "bad-page-info": commit["statusCheckRollup"]["contexts"]["pageInfo"] = ["invalid"]
                self.server.payload = data
                result = self.call("--fresh", expected=1)
                self.assertEqual(result["state"], "ERROR")
                self.assertNotIn("ci", result)

    def paged_fixture(self):
        first, second = fixture(rollup="FAILURE"), fixture(conclusion="FAILURE", rollup="FAILURE")
        first_contexts = pull(first)["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
        prototype = first_contexts["nodes"][0]
        first_contexts["nodes"] = [dict(prototype, id=f"check-{i}", name=f"CI {i}") for i in range(100)]
        first_contexts.update(totalCount=101, pageInfo={"hasNextPage": True, "endCursor": "cursor-100"})
        second_contexts = pull(second)["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
        second_contexts["nodes"][0].update(id="check-100", name="CI 100")
        second_contexts.update(totalCount=101, pageInfo={"hasNextPage": False, "endCursor": "cursor-101"})
        return first, second

    def test_all_pages_include_late_failure_and_oldest_age(self):
        first, second = self.paged_fixture()
        self.server.payload, self.server.pages = first, {"cursor-100": second}
        self.server.date_offsets = {None: 12, "cursor-100": 1}
        state = self.call()
        self.assertFalse(state["incomplete"])
        self.assertEqual(state["ci"], "CHECKS_FAILED")
        self.assertEqual(state["checks"]["passed"], 100)
        self.assertEqual(state["checks"]["failed"], 1)
        self.assertEqual(state["failed_checks"], ["CI 100"])
        self.assertGreaterEqual(time.time() - state["observed_at"], 11)
        self.assertEqual(self.server.requests, 2)
        self.call("--fresh")
        self.assertEqual(self.server.requests, 4)
        self.call()
        self.assertEqual(self.server.requests, 4)

    def test_bad_pagination_or_head_race_never_returns_mixed_success(self):
        for kind in ["missing-cursor", "repeated-cursor", "head-race", "page-error", "count-change"]:
            with self.subTest(kind=kind):
                first, second = self.paged_fixture()
                a = pull(first)["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
                b = pull(second)["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]
                if kind == "missing-cursor": a["pageInfo"]["endCursor"] = None
                if kind == "repeated-cursor":
                    a["totalCount"] = b["totalCount"] = 102
                    b["pageInfo"] = {"hasNextPage": True, "endCursor": "cursor-100"}
                if kind == "head-race": pull(second)["headRefOid"] = "d" * 40
                if kind == "page-error": second = {"errors": [{"message": "fixture page error"}]}
                if kind == "count-change": b["totalCount"] = 102
                self.server.payload, self.server.pages = first, {"cursor-100": second}
                state = self.call("--fresh", expected=1)
                self.assertEqual(state["state"], "ERROR")
                self.assertNotIn("ci", state)


if __name__ == "__main__":
    unittest.main()
