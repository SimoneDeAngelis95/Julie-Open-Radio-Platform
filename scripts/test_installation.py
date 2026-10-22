# SPDX-License-Identifier: GPL-3.0-only
"""Exercise installation generation only in disposable directories."""

import importlib.util
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "configure", ROOT / "scripts/configure.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "config").mkdir()
        shutil.copy(
            ROOT / "config/icecast.xml.example",
            self.root / "config/icecast.xml.example",
        )

    def test_secrets_match_icecast_and_are_private_and_stable(self):
        module.configure(self.root, "radio.example.org", "127.0.0.1")
        env = dict(
            line.split("=", 1) for line in (self.root / ".env").read_text().splitlines()
        )
        tree = ET.parse(self.root / "config/icecast.xml")
        self.assertEqual(
            env["ICECAST_SOURCE_PASSWORD"],
            tree.find("authentication/source-password").text,
        )
        self.assertNotEqual(env["ICECAST_SOURCE_PASSWORD"], env["JULIE_LIVE_PASSWORD"])
        self.assertEqual(tree.find("hostname").text, "radio.example.org")
        for name in [".env", ".env.dashboard", "config/icecast.xml"]:
            path = self.root / name
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        previous = {
            name: (self.root / name).read_bytes()
            for name in [".env", ".env.dashboard", "config/icecast.xml"]
        }
        module.configure(self.root, "changed.example.org", "0.0.0.0")
        self.assertEqual(
            previous, {name: (self.root / name).read_bytes() for name in previous}
        )

    def test_partial_configuration_is_not_overwritten(self):
        (self.root / ".env").write_text("KEEP=unchanged\n")
        with self.assertRaises(ValueError):
            module.configure(self.root, "localhost", "127.0.0.1")
        self.assertEqual((self.root / ".env").read_text(), "KEEP=unchanged\n")
        self.assertFalse((self.root / ".env.dashboard").exists())

    def test_invalid_host_or_bind_cannot_create_configuration(self):
        for host, bind in [
            ("https://example.org", "127.0.0.1"),
            ("example.org", "example.org"),
            ("name\nBAD=value", "0.0.0.0"),
        ]:
            with self.assertRaises(ValueError):
                module.configure(self.root, host, bind)
        self.assertFalse((self.root / ".env").exists())


if __name__ == "__main__":
    unittest.main()
