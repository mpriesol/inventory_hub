"""Backup integrity, isolation and failure gates without a live Docker host."""
import importlib.util
import io
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
        self.assertFalse(set(start) & {"--volume", "-v", "--mount", "--publish", "-p", "--privileged"})
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

    def existing_fixture(self):
        """A synthetic archive fixture, never evidence of a production restore."""
        counts = {table: 3 for table in backup.CORE_TABLES}
        with patch.object(backup, "run", side_effect=self.command), \
             patch.object(backup, "restore_check", return_value=counts):
            folder = self.create()
        self.calls.clear()
        return folder, counts

    def test_verify_existing_preserves_source_and_never_touches_production(self):
        folder, counts = self.existing_fixture()
        original = {path.name: path.read_bytes() for path in folder.iterdir()}
        with patch.object(backup, "run", side_effect=AssertionError("No production command allowed")), \
             patch.object(backup, "restore_check", return_value=counts) as restore:
            report = backup.verify_existing(folder)
        self.assertEqual({path.name: path.read_bytes() for path in folder.iterdir()}, original)
        self.assertEqual((report["status"], report["restore_verified"]), ("verified", True))
        self.assertEqual(report["source_manifest_sha256"], backup.digest(folder / "manifest.json"))
        self.assertNotIn(str(folder), json.dumps(report))
        restore.assert_called_once_with("sha256:" + "c" * 64, folder / "database.dump")

    def test_verify_existing_rejects_incomplete_manifest_before_restore(self):
        folder, _ = self.existing_fixture()
        path = folder / "manifest.json"
        original = json.loads(path.read_text())
        changes = [
            {"status": "incomplete"}, {"phase": "isolated_restore"}, {"restore_verified": False},
            {"writers_paused_operator_assertion": False}, {"version": True},
            {"scope": ["postgresql"]}, {"scope": [{"bad": "value"}]}, {"files": {}},
            {"postgres_image": "postgres:latest"}, {"restored_counts": {"products": 3}},
            {"restored_counts": {table: -1 for table in backup.CORE_TABLES}},
            {"created_at": "2026-10-25T00:00:00"},
            {"files": {**original["files"], "database.dump": {"size": True, "sha256": "a" * 64}}},
            {"files": {**original["files"], "database.dump": {"size": 10, "sha256": "invalid"}}},
            {"files": {**original["files"], "../other": original["files"]["database.dump"]}},
        ]
        with patch.object(backup, "restore_check") as restore:
            for changed in changes:
                with self.subTest(changed=changed):
                    path.write_text(json.dumps({**original, **changed}))
                    with self.assertRaises(backup.BackupError):
                        backup.verify_existing(folder)
            path.write_text("not JSON")
            with self.assertRaises(ValueError):
                backup.verify_existing(folder)
        restore.assert_not_called()

    def test_verify_existing_rejects_missing_corrupt_and_symlink_sources(self):
        folder, _ = self.existing_fixture()
        manifest = folder / "manifest.json"
        raw = manifest.read_bytes()
        external = self.root / "outside-manifest.json"
        external.write_bytes(raw)
        with patch.object(backup, "restore_check") as restore:
            manifest.unlink()
            with self.assertRaises(backup.BackupError):
                backup.verify_existing(folder)
            manifest.symlink_to(external)
            with self.assertRaises(backup.BackupError):
                backup.verify_existing(folder)
            manifest.unlink()
            manifest.write_bytes(raw)
            link = self.root / "linked-backup"
            link.symlink_to(folder, target_is_directory=True)
            with self.assertRaises(backup.BackupError):
                backup.verify_existing(link)
            archive = folder / "inventory-data.tar.gz"
            content = archive.read_bytes()
            archive.write_bytes(b"changed")
            with self.assertRaises(backup.BackupError):
                backup.verify_existing(folder)
            archive.unlink()
            target = self.root / "outside.tar.gz"
            target.write_bytes(content)
            archive.symlink_to(target)
            with self.assertRaises(backup.BackupError):
                backup.verify_existing(folder)
        restore.assert_not_called()

    def test_verify_existing_rejects_unsafe_or_unreadable_tar_even_with_matching_hash(self):
        folder, _ = self.existing_fixture()
        path = folder / "manifest.json"
        manifest = json.loads(path.read_text())
        archive = folder / "supplier-secrets.tar"
        with patch.object(backup, "restore_check") as restore:
            for name, link in (("../escape", None), ("/absolute", None), ("link", "/secret"), ("link", "../secret")):
                with self.subTest(name=name, link=link):
                    with tarfile.open(archive, "w") as output:
                        member = tarfile.TarInfo(name)
                        if link is not None:
                            member.type = tarfile.SYMTYPE
                            member.linkname = link
                        output.addfile(member)
                    manifest["files"][archive.name] = {"size": archive.stat().st_size, "sha256": backup.digest(archive)}
                    path.write_text(json.dumps(manifest))
                    with self.assertRaises(backup.BackupError):
                        backup.verify_existing(folder)
            # A valid header claims a body that is missing from this truncated tar.
            member = tarfile.TarInfo("truncated")
            member.size = 4096
            archive.write_bytes(member.tobuf() + b"short")
            manifest["files"][archive.name] = {"size": archive.stat().st_size, "sha256": backup.digest(archive)}
            path.write_text(json.dumps(manifest))
            with self.assertRaises(tarfile.TarError):
                backup.verify_existing(folder)
        restore.assert_not_called()

    def test_verify_existing_rejects_parent_link_hidden_by_symlink_ancestry(self):
        folder, _ = self.existing_fixture()
        path = folder / "manifest.json"
        manifest = json.loads(path.read_text())
        archive = folder / "supplier-secrets.tar"
        with tarfile.open(archive, "w") as output:
            for name, target in (("a", "."), ("a/b", "..")):
                member = tarfile.TarInfo(name)
                member.type = tarfile.SYMTYPE
                member.linkname = target
                output.addfile(member)
            member = tarfile.TarInfo("a/b/escape")
            member.size = 1
            output.addfile(member, io.BytesIO(b"x"))
        manifest["files"][archive.name] = {"size": archive.stat().st_size, "sha256": backup.digest(archive)}
        path.write_text(json.dumps(manifest))
        # Individually normalized targets look internal, but resolving a -> .
        # makes a/b -> .. leave the archive root. Nothing is extracted here.
        with patch.object(backup, "restore_check") as restore, \
             self.assertRaisesRegex(backup.BackupError, "backup_unsafe_archive_link"):
            backup.verify_existing(folder)
        restore.assert_not_called()

    def test_failed_existing_restore_preserves_manifest_and_reports_failure(self):
        folder, _ = self.existing_fixture()
        raw = (folder / "manifest.json").read_bytes()
        output = io.StringIO()
        with patch.object(backup, "restore_check", side_effect=backup.BackupError("synthetic failure")), \
             patch("sys.argv", ["backup_inventory.py", "--verify-existing", str(folder)]), \
             patch("sys.stdout", output):
            self.assertEqual(backup.main(), 1)
        report = json.loads(output.getvalue())
        self.assertEqual((report["status"], report["restore_verified"]), ("failed", False))
        self.assertNotIn("synthetic failure", output.getvalue())
        self.assertEqual((folder / "manifest.json").read_bytes(), raw)

    def test_verify_existing_detects_changed_counts_and_concurrent_source_change(self):
        folder, counts = self.existing_fixture()
        with patch.object(backup, "restore_check", return_value={**counts, "products": 4}), \
             self.assertRaises(backup.BackupError):
            backup.verify_existing(folder)
        path = folder / "manifest.json"
        raw = path.read_bytes()
        def changed_manifest(*args):
            path.write_bytes(raw + b"\n")
            return counts
        with patch.object(backup, "restore_check", side_effect=changed_manifest), \
             self.assertRaises(backup.BackupError):
            backup.verify_existing(folder)
        path.write_bytes(raw)
        def changed_source(*args):
            (folder / "database.dump").write_bytes(b"changed during restore")
            return counts
        with patch.object(backup, "restore_check", side_effect=changed_source), \
             self.assertRaises(backup.BackupError):
            backup.verify_existing(folder)

    def test_verify_existing_cli_needs_no_compose_or_writer_pause(self):
        folder, counts = self.existing_fixture()
        output = io.StringIO()
        with patch.object(backup, "restore_check", return_value=counts), \
             patch("sys.argv", ["backup_inventory.py", "--verify-existing", str(folder)]), \
             patch("sys.stdout", output):
            self.assertEqual(backup.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "verified")
        with patch("sys.argv", ["backup_inventory.py", "--verify-existing", str(folder), "--writers-paused"]), \
             patch("sys.stderr", io.StringIO()), patch.object(backup, "restore_check") as restore, \
             self.assertRaises(SystemExit) as exit_code:
            backup.main()
        self.assertEqual(exit_code.exception.code, 2)
        restore.assert_not_called()
