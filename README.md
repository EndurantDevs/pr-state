# pr-state

## Install or update

```sh
python3 -c 'from urllib.request import urlopen; exec(urlopen("https://raw.githubusercontent.com/EndurantDevs/pr-state/main/install.py", timeout=30).read())' && export PATH="$HOME/.local/bin:$PATH"
```

Requires macOS or Linux, Python 3.10+, and [GitHub CLI](https://cli.github.com/).
It reuses your existing `gh` authentication. If you have not signed in, run `gh auth login` once.
No repository configuration is needed.

The same command updates an existing installation. It verifies the downloaded source
against the repository's SHA-256 manifest, then atomically replaces the managed source
and launcher. This checksum detects mismatched downloads; it does not independently
authenticate the publisher. Review [install.py](install.py) before running it if preferred.

Files go in `~/.local/share/pr-state/` with a launcher at `~/.local/bin/pr-state`.
The command adds that directory to the current shell's `PATH`. To keep it available in
new shells, add `export PATH="$HOME/.local/bin:$PATH"` to your shell profile once.
The installer leaves credentials, shell profiles, and unrelated files untouched.

## Use

Replace `OWNER/REPO` and `123` with your repository and PR number:

```sh
pr-state OWNER/REPO 123
pr-state OWNER/REPO 123 --json
pr-state OWNER/REPO 123 --wait --timeout 50 --json
pr-state OWNER/REPO 123 --fresh
```

One line reports the head, recorded base, current target tip, check counts, review
decision, merge state, and observation age. `--json` returns the same information
as one JSON object. `--wait` stays silent until the observation changes or the
timeout expires; JSON includes `changed: true` or `changed: false`.
`--fresh` fetches now and replaces the shared cached observation.
Active `--wait` readers share a 5-second refresh interval. Ordinary reads use a
30-second cache and also benefit from any watcher's newer response. There are no
push notifications: a watcher sees a GitHub change within about 5 seconds plus
request time. Two watchers share each refresh instead of sending two requests.

## Why share PR observations?

![Independent readers each request PR state; pr-state readers share a local cache with one GitHub refresh per page and cache window.](docs/shared-state.svg)

*Illustration of one machine and authentication context. Motion and request timing
are conceptual, not a performance benchmark.*

Parallel workers often watch the same PR. Each can ask GitHub for the same information,
parse the same checks, and report the same unchanged status. `pr-state` gives them
one reusable observation:

- **Fewer duplicate requests.** Readers share GitHub CLI's HTTP cache (30 seconds
  for ordinary reads, 5 seconds while waiting). A
  per-PR file lock lets one reader populate it while the others reuse the response.
- **Less polling chatter.** One normalized line is easier to consume than repeated
  check listings. `--wait` avoids a stream of unchanged output.
- **Clearer state.** Queued, running, failed, skipped, neutral, and unknown checks
  remain distinct. The recorded comparison base and current target tip are separate.

The integration tests start six concurrent ordinary readers for the same one-page PR
and authentication context, and assert exactly one HTTP request. Three concurrent
watchers also share one refresh when the state changes, and an ordinary reader then
reuses their new response. Multi-page check lists
require one request per page. Readers on different machines or using different
credentials do not share responses. This verifies request reuse, not a delivery-time
or CI-runtime speedup.

It is a small Python program using the standard library and the existing
[`gh api --cache`](https://cli.github.com/manual/gh_api) implementation: no daemon,
database, extra service, or Python packages.

For one person watching checks, [`gh pr checks --watch --interval 5`](https://cli.github.com/manual/gh_pr_checks)
is already a good fit. `pr-state` adds shared refreshes across independent local
workers, a bounded wait that returns one observation on change, and normalized
head/base/review information alongside checks. Waiting itself is not new, and any
model-token or delivery-time saving still needs measurement in your workflow.

## Observations and safety

Observations may be up to 30 seconds old. `CHECKS_PASS` describes the check rollup;
it is **not permission to merge**. Required-check policy, approvals, unresolved review
threads, deployment gates, and authorization still need their normal authoritative
checks. Refresh the relevant live state before any PR or CI mutation.
`BEHIND` is information, not an instruction to rebase. A rewritten head needs its own
validation.

All check pages are read, with a shared deadline and consistency checks. Missing,
unknown, incomplete, or changing data is not reported as success. An API, authentication,
pagination, or local-cache error exits nonzero; JSON mode returns `state: "ERROR"`.
A failed CI check itself is an observation and does not make the command fail.

The cache is private to the local OS user, under `$XDG_CACHE_HOME/pr-state` or
`~/.cache/pr-state`. GitHub CLI's response keys include authentication, separating
responses for different credentials. Existing `GH_HOST`, `GH_CONFIG_DIR`, and token
environment variables are honored. Private repository observations stay in the local
cache; avoid copying them into public logs. The tool does not edit PRs, dispatch CI,
claim work, or merge anything.

## Adopt it in a workflow

Use `pr-state` for routine per-PR observation and `--wait --timeout 50` for bounded
waiting. Preserve efficient batched queries when watching many different PRs.
If the helper fails, make a direct read when needed; an error is never acceptance.

To reduce metadata churn as well, prepare PR titles and descriptions before validation.
Batch optional later edits before the next source push or after the full run ends.
Keep generated summaries in review comments where your review tool supports that.
Make necessary public-content corrections immediately. These habits can avoid
metadata-triggered workflow runs; their effect depends on your workflows.

## Development

With Python and `gh` installed:

```sh
python3 -m unittest -v
```

Tests use synthetic responses and a local HTTP server, including the installed `gh`
client's real cache. They do not need a GitHub token or a live repository. Coverage
includes concurrent readers, credential separation, forced refresh, pagination races,
timeouts, error states, and installer updates that preserve unrelated files.

After changing `pr_state.py`, refresh its checksum before running the tests:

```sh
python3 -c 'import hashlib, pathlib; p = pathlib.Path("pr_state.py"); pathlib.Path("pr_state.py.sha256").write_text(hashlib.sha256(p.read_bytes()).hexdigest() + "  pr_state.py\n")'
```

MIT licensed. See [LICENSE](LICENSE).
