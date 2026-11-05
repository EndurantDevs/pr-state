#!/usr/bin/env python3
"""Install or update pr-state without changing shell profiles or credentials."""

import hashlib
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.error
import urllib.request


BASE_URL = "https://raw.githubusercontent.com/EndurantDevs/pr-state/main"


def download(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read()


def install(prefix, base_url=BASE_URL):
    source = download(f"{base_url}/pr_state.py")
    manifest = download(f"{base_url}/pr_state.py.sha256").decode("ascii")
    match = re.fullmatch(r"([0-9a-f]{64})  pr_state\.py\n?", manifest)
    if not match or hashlib.sha256(source).hexdigest() != match[1]:
        raise ValueError("Source checksum mismatch; existing installation was preserved")
    compile(source, "pr_state.py", "exec")

    prefix = Path(prefix)
    package = prefix / "share" / "pr-state"
    bindir = prefix / "bin"
    target = package / "pr_state.py"
    launcher = bindir / "pr-state"
    package.mkdir(parents=True, exist_ok=True, mode=0o700)
    bindir.mkdir(parents=True, exist_ok=True)
    if package.is_symlink() or package.stat().st_uid != os.getuid():
        raise ValueError("Installation directory must be owned by this user and not a symlink")
    if target.is_symlink():
        raise ValueError("Refusing to replace a symlink at the source destination")
    if launcher.exists() or launcher.is_symlink():
        if not launcher.is_symlink() or launcher.resolve() != target.resolve():
            raise ValueError("An unrelated pr-state launcher exists; move it before installing")

    # Stage both files before replacing the managed source and launcher.
    with tempfile.TemporaryDirectory(prefix=".install-", dir=package) as source_stage:
        with tempfile.TemporaryDirectory(prefix=".pr-state-", dir=bindir) as link_stage:
            staged_source = Path(source_stage) / "pr_state.py"
            staged_source.write_bytes(source)
            staged_source.chmod(0o755)
            staged_link = Path(link_stage) / "pr-state"
            staged_link.symlink_to(os.path.relpath(target, bindir))
            os.replace(staged_source, target)
            os.replace(staged_link, launcher)


def main():
    if sys.version_info < (3, 10):
        print("pr-state requires Python 3.10 or newer", file=sys.stderr)
        return 1
    os.umask(0o077)
    try:
        install(Path.home() / ".local")
    except (OSError, ValueError, SyntaxError, urllib.error.URLError) as error:
        print(f"pr-state installation failed: {error}", file=sys.stderr)
        return 1
    print("Installed pr-state. Re-run the same command to update.")
    print('If needed, add to PATH: export PATH="$HOME/.local/bin:$PATH"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
