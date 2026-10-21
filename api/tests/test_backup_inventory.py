"""Backup integrity, isolation and failure gates without a live Docker host."""
import importlib.util
import json
from pathlib import Path
import tarfile
import tempfile
from unittest import TestCase
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("hub_backup", Path(__file__).resolve().parents[2] / "scripts" / "backup_inventory.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


class BackupInventoryTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.compose = self.root / "deployment"
        self.data = self.root / "data"
        self.compose.mkdir()
        self.data.mkdir()
        (self.compose / "docker-compose.yml").write_text("services: {}\n")
        (self.data / "snapshot.json").write_text('{"value":"original"}')
        self.calls = []

    def command(self, args, *, stdin=None, stdout=None):
        self.calls.append(args)
        if args[:3] == ["docker", "cp", "a" * 64 + ":/var/lib/inventory-hub-secrets/."]:
            with tarfile.open(fileobj=stdout, mode="w|") as output:
                output.add(self.data / "snapshot.json", arcname="credentials.json")
            return b""
        if "--quiet" in args:
            return ("a" * 64 if args[-1] == "api" else "b" * 64).encode()
        if "{{.State.Running}}" in args:
            return b"false"
        if "{{.Image}}" in args:
            return b"sha256:" + b"c" * 64
        if "pg_dump" in args[-1]:
            stdout.write(b"Synthetic dump for transport checks only")
            return b""
        raise AssertionError(args)

    def create(self, **changes):
        return backup.create_backup(self.compose, self.data, changes.pop("output", self.root / "backups"),
                                    ["docker-compose.yml"], writers_paused=changes.pop("paused", True), **changes)

    def test_backup_requires_paused_writers_stopped_api_and_safe_output(self):
        with patch.object(backup, "run", side_effect=self.command):
            for changes in ({"paused": False}, {"output": self.data / "backup"}):
                with self.assertRaises(backup.BackupError):
                    self.create(**changes)
        self.assertEqual(self.calls, [])
        with patch.object(backup, "run", side_effect=lambda args: b"a" * 64 if "--quiet" in args else b"true"), \
             self.assertRaises(backup.BackupError):
            self.create()

    def test_private_manifest_requires_successful_restore_and_detects_changed_bytes(self):
        with patch.object(backup, "run", side_effect=self.command), \
             patch.object(backup, "restore_check", return_value={"stock_movements": 3}) as restore:
            folder = self.create()
        manifest = json.loads((folder / "manifest.json").read_text())
        self.assertEqual((manifest["status"], manifest["restore_verified"]), ("verified", True))
        self.assertEqual(len(manifest["files"]), 4)
        restore.assert_called_once_with("sha256:" + "c" * 64, folder / "database.dump")
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in folder.iterdir()))
        backup.verify_files(folder, manifest)
        (folder / "database.dump").write_bytes(b"tampered")
        with self.assertRaises(backup.BackupError):
            backup.verify_files(folder, manifest)

    def test_failed_restore_never_labels_backup_verified_or_restarts_production(self):
        with patch.object(backup, "run", side_effect=self.command), \
             patch.object(backup, "restore_check", side_effect=backup.BackupError("restore failed")), \
             self.assertRaises(backup.BackupError):
            self.create()
        manifest = json.loads(next((self.root / "backups").glob("*/manifest.json")).read_text())
        self.assertEqual((manifest["status"], manifest["restore_verified"]), ("incomplete", False))
        self.assertFalse(any("restart" in call or "stop" in call or "up" in call for call in self.calls))

    def test_restore_is_network_isolated_and_only_removes_its_own_container(self):
        dump = self.root / "dump"
        dump.write_bytes(b"dump")
        calls = []
        def command(args, **kwargs):
            calls.append(args)
            return b'{"stock_movements":3}' if "psql" in args else b""
        with patch.object(backup, "run", side_effect=command):
            self.assertEqual(backup.restore_check("sha256:" + "c" * 64, dump), {"stock_movements": 3})
        start = calls[0]
        name = start[start.index("--name") + 1]
        self.assertIn("--pull=never", start)
        self.assertEqual(start[start.index("--network") + 1], "none")
        self.assertTrue(name.startswith("inventory-hub-restore-"))
        self.assertEqual(calls[-1], ["docker", "rm", "--force", name])
        self.assertTrue(all(name in call for call in calls))
        restore = next(call for call in calls if "pg_restore" in call)
        self.assertIn("--single-transaction", restore)
        self.assertIn("--dbname=inventory_hub_restore_check", restore)

    def test_failed_container_creation_cannot_delete_an_existing_container(self):
        dump = self.root / "dump"
        dump.write_bytes(b"dump")
        with patch.object(backup, "run", side_effect=backup.BackupError("create failed")) as run, \
             self.assertRaises(backup.BackupError):
            backup.restore_check("sha256:" + "c" * 64, dump)
        self.assertEqual(run.call_count, 1)

    def test_external_symlink_cannot_silently_omit_required_bytes(self):
        external = self.root / "outside-secret"
        external.write_text("synthetic secret")
        (self.data / "secret-link").symlink_to(external)
        with self.assertRaises(backup.BackupError):
            backup.archive(self.data, self.root / "data.tar.gz", "inventory-data")
