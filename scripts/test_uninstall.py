"""Verify destructive boundaries with fake Docker and disposable installation files."""

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "uninstall", ROOT / "scripts/uninstall.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FakeDocker:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.config = {
            "name": "julie",
            "services": {"dashboard": {}, "dashboard-db": {}},
            "volumes": {"db": {"name": "julie_db"}},
            "networks": {"default": {"name": "julie_default"}},
        }
        self.containers = [
            {
                "Id": "own-id",
                "Config": {
                    "Labels": {
                        "com.docker.compose.project": "julie",
                        "com.docker.compose.project.working_dir": str(root),
                        "com.docker.compose.service": "dashboard",
                    }
                },
                "Mounts": [{"Name": "julie_db"}],
            }
        ]
        self.volume_labels = {
            "com.docker.compose.project": "julie",
            "com.docker.compose.volume": "db",
        }
        self.network_users = {"own-id": {}}
        self.fail_volume = False

    def run(self, *args):
        self.calls.append(args)
        if args[0] == "compose":
            return json.dumps(self.config)
        if args[:2] == ("ps", "-aq"):
            return "" if len(args) > 2 else " ".join(c["Id"] for c in self.containers)
        if args[0] == "inspect":
            return json.dumps(self.containers)
        if args[:2] == ("volume", "ls"):
            return "julie_db\n"
        if args[:2] == ("volume", "inspect"):
            return json.dumps([{"Labels": self.volume_labels}])
        if args[:2] == ("network", "ls"):
            return "julie_default\n"
        if args[:2] == ("network", "inspect"):
            return json.dumps(
                [
                    {
                        "Labels": {"com.docker.compose.project": "julie"},
                        "Containers": self.network_users,
                    }
                ]
            )
        if args[:2] == ("volume", "rm") and self.fail_volume:
            raise module.UninstallError("volume failed")
        return ""


class UninstallTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve() / "julie"
        for relative in [
            "install.sh",
            "compose.yaml",
            "compose.dashboard.yaml",
            "dashboard/manage.py",
            ".env",
            ".env.dashboard",
            "config/icecast.xml",
            "audio/library/song.flac",
            "backups/copy.zip",
        ]:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("test")
        self.docker = FakeDocker(self.root)
        self.other = self.root.parent / "unrelated"
        self.other.mkdir()
        (self.other / "keep").write_text("keep")

    def test_default_preserves_database_audio_and_configuration(self):
        plan = module.inspect_plan(self.root, self.docker, False)
        module.execute(self.root, self.docker, plan)
        self.assertTrue((self.root / "audio/library/song.flac").exists())
        self.assertTrue((self.root / ".env").exists())
        self.assertFalse(
            any(call[:2] == ("volume", "rm") for call in self.docker.calls)
        )
        self.assertIn(("rm", "--force", "own-id"), self.docker.calls)
        self.assertTrue((self.other / "keep").exists())

    def test_purge_removes_only_runtime_files_and_owned_database(self):
        plan = module.inspect_plan(self.root, self.docker, True)
        module.execute(self.root, self.docker, plan, purge=True)
        self.assertFalse((self.root / "audio").exists())
        self.assertFalse((self.root / "backups").exists())
        self.assertFalse((self.root / ".env").exists())
        self.assertTrue((self.root / "install.sh").exists())
        self.assertTrue((self.other / "keep").exists())
        self.assertIn(("volume", "rm", "julie_db"), self.docker.calls)
        self.assertFalse(
            any(call[0] in ("system", "image") for call in self.docker.calls)
        )

    def test_remove_directory_requires_only_verified_root(self):
        self.assertEqual(module.validate_root(self.root), self.root)
        plan = module.inspect_plan(self.root, self.docker, True)
        module.execute(self.root, self.docker, plan, purge=True, remove_directory=True)
        self.assertFalse(self.root.exists())
        self.assertTrue((self.other / "keep").exists())

    def test_foreign_working_directory_or_service_is_rejected(self):
        labels = self.docker.containers[0]["Config"]["Labels"]
        labels["com.docker.compose.project.working_dir"] = str(self.other)
        with self.assertRaises(module.UninstallError):
            module.inspect_plan(self.root, self.docker, True)
        self.assertFalse(any(call[0] == "rm" for call in self.docker.calls))

    def test_foreign_container_using_project_data_is_rejected(self):
        self.docker.containers.append(
            {
                "Id": "other",
                "Config": {"Labels": {}},
                "Mounts": [{"Source": str(self.root / "audio")}],
            }
        )
        with self.assertRaises(module.UninstallError):
            module.inspect_plan(self.root, self.docker, True)

    def test_shared_or_unlabelled_volume_is_rejected(self):
        self.docker.volume_labels = {}
        with self.assertRaises(module.UninstallError):
            module.inspect_plan(self.root, self.docker, True)
        self.docker.volume_labels = {
            "com.docker.compose.project": "julie",
            "com.docker.compose.volume": "db",
        }
        self.docker.containers.append(
            {"Id": "other", "Config": {"Labels": {}}, "Mounts": [{"Name": "julie_db"}]}
        )
        with self.assertRaises(module.UninstallError):
            module.inspect_plan(self.root, self.docker, True)

    def test_symlink_and_mount_boundaries_are_rejected(self):
        import shutil

        shutil.rmtree(self.root / "audio")
        (self.root / "audio").symlink_to(self.other, target_is_directory=True)
        with self.assertRaises(module.UninstallError):
            module.check_files(self.root, False)
        (self.root / "audio").unlink()
        with (
            patch.object(Path, "is_mount", return_value=True),
            self.assertRaises(module.UninstallError),
        ):
            module.check_files(self.root, True)
        self.assertTrue((self.other / "keep").exists())

    def test_inventory_change_aborts_before_removal(self):
        plan = module.inspect_plan(self.root, self.docker, True)
        self.docker.containers = []
        with self.assertRaises(module.UninstallError):
            module.execute(self.root, self.docker, plan, purge=True)
        self.assertTrue((self.root / ".env").exists())
        self.assertFalse(any(call[0] == "rm" for call in self.docker.calls))

    def test_docker_failure_preserves_local_configuration(self):
        plan = module.inspect_plan(self.root, self.docker, True)
        self.docker.fail_volume = True
        with self.assertRaises(module.UninstallError):
            module.execute(self.root, self.docker, plan, purge=True)
        self.assertTrue((self.root / ".env").exists())
        self.assertTrue((self.root / "audio/library/song.flac").exists())

    def test_external_volumes_are_never_removed(self):
        self.docker.config["volumes"]["db"]["external"] = True
        plan = module.inspect_plan(self.root, self.docker, True)
        self.assertEqual(plan["volumes"], [])

    def test_shared_network_aborts_before_any_removal(self):
        self.docker.network_users["unrelated-id"] = {}
        with self.assertRaises(module.UninstallError):
            module.inspect_plan(self.root, self.docker, True)
        self.assertFalse(any(call[0] == "rm" for call in self.docker.calls))

    def test_dry_run_creates_no_locks_and_keeps_every_file(self):
        before = sorted(
            str(path.relative_to(self.root)) for path in self.root.rglob("*")
        )
        cwd = Path.cwd()
        try:
            with (
                patch.object(
                    module, "__file__", str(self.root / "scripts/uninstall.py")
                ),
                patch.object(
                    module.sys, "argv", ["uninstall.py", "--purge", "--dry-run"]
                ),
                patch.object(module, "Docker", return_value=self.docker),
                redirect_stdout(io.StringIO()),
            ):
                module.main()
        finally:
            os.chdir(cwd)
        self.assertEqual(
            before,
            sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")),
        )
        self.assertFalse(any(call[0] == "rm" for call in self.docker.calls))

    def test_rejected_confirmation_never_removes_resources(self):
        cwd = Path.cwd()
        try:
            with (
                patch.object(
                    module, "__file__", str(self.root / "scripts/uninstall.py")
                ),
                patch.object(module.sys, "argv", ["uninstall.py", "--purge"]),
                patch.object(module, "Docker", return_value=self.docker),
                patch("builtins.input", return_value="no"),
                redirect_stdout(io.StringIO()),
            ):
                module.main()
        finally:
            os.chdir(cwd)
        self.assertTrue((self.root / ".env").exists())
        self.assertTrue((self.root / "audio/library/song.flac").exists())
        self.assertFalse(any(call[0] == "rm" for call in self.docker.calls))


if __name__ == "__main__":
    unittest.main()
