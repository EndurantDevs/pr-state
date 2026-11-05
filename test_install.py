import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location("installer", ROOT / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="pr-state-install-test-")
        self.addCleanup(self.scratch.cleanup)
        self.prefix = Path(self.scratch.name) / ".local"
        self.source = b'#!/usr/bin/env python3\nprint("fixture-one")\n'

    def download(self, url):
        if url.endswith(".sha256"):
            return (hashlib.sha256(self.source).hexdigest() + "  pr_state.py\n").encode()
        return self.source

    def install(self):
        with patch.object(installer, "download", side_effect=self.download):
            installer.install(self.prefix)

    def test_install_update_and_preserve_unmanaged_files(self):
        self.install()
        launcher = self.prefix / "bin" / "pr-state"
        package = self.prefix / "share" / "pr-state"
        guide = package / "local-notes.txt"
        guide.write_text("keep this")
        self.assertTrue(launcher.is_symlink())
        self.assertFalse(os.path.isabs(os.readlink(launcher)))
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertEqual(subprocess.check_output([sys.executable, str(launcher)], text=True).strip(), "fixture-one")
        self.source = b'#!/usr/bin/env python3\nprint("fixture-two")\n'
        self.install()
        self.assertEqual(launcher.read_bytes(), self.source)
        self.assertEqual(guide.read_text(), "keep this")
        self.assertEqual(list(package.glob(".install-*")), [])
        self.assertEqual(list(launcher.parent.glob(".pr-state-*")), [])

    def test_bad_download_checksum_or_syntax_preserves_installed_source(self):
        self.install()
        target = self.prefix / "share" / "pr-state" / "pr_state.py"
        previous = target.read_bytes()
        for response in [b"incorrect checksum", b"0" * 64 + b"  pr_state.py\n"]:
            with patch.object(installer, "download", side_effect=[self.source, response]):
                with self.assertRaises(ValueError): installer.install(self.prefix)
            self.assertEqual(target.read_bytes(), previous)
        with patch.object(installer, "download", side_effect=OSError("fixture outage")):
            with self.assertRaises(OSError): installer.install(self.prefix)
        self.source = b"this is not valid python!"
        with self.assertRaises(SyntaxError): self.install()
        self.assertEqual(target.read_bytes(), previous)

    def test_refuses_unrelated_launcher_and_source_symlink(self):
        launcher = self.prefix / "bin" / "pr-state"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("unrelated")
        with self.assertRaisesRegex(ValueError, "unrelated"): self.install()
        self.assertEqual(launcher.read_text(), "unrelated")
        launcher.unlink()
        victim = self.prefix / "keep.txt"
        victim.write_text("keep")
        target = self.prefix / "share" / "pr-state" / "pr_state.py"
        target.symlink_to(victim)
        with self.assertRaisesRegex(ValueError, "symlink"): self.install()
        self.assertEqual(victim.read_text(), "keep")

    def test_published_manifest_matches_source(self):
        expected = hashlib.sha256((ROOT / "pr_state.py").read_bytes()).hexdigest()
        self.assertEqual((ROOT / "pr_state.py.sha256").read_text(), expected + "  pr_state.py\n")


if __name__ == "__main__":
    unittest.main()
