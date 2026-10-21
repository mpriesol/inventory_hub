#!/usr/bin/env python3
"""Private pre-pilot backup and isolated restore verification on the Hub host.

Never restores to production, stops services, enables writers or renames cards.
Run only after the operator has paused every relevant writer. Backup contents
include credentials and customer data: keep the output on protected storage.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import time
from uuid import uuid4


class BackupError(Exception):
    pass


def run(command, *, stdin=None, stdout=None):
    result = subprocess.run(command, stdin=stdin, stdout=stdout or subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=1800, check=False)
    if result.returncode:
        # Child errors can contain connection strings or customer metadata.
        raise BackupError("backup_command_failed")
    return result.stdout or b""


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def archive(root, destination, name):
    def checked(member):
        relative = Path(member.name).relative_to(name)
        path = root / relative
        if member.issym() and not path.resolve().is_relative_to(root):
            raise BackupError("backup_external_symlink_requires_explicit_backup")
        if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
            raise BackupError("backup_special_file_requires_review")
        return member
    with tarfile.open(destination, "w:gz", dereference=False) as output:
        output.add(root, arcname=name, filter=checked)


def verify_files(folder, manifest):
    """Integrity check only; this does not substitute for the DB restore test."""
    if not manifest.get("files"):
        raise BackupError("backup_files_missing")
    for name, metadata in manifest["files"].items():
        if Path(name).name != name or name in (".", ".."):
            raise BackupError("backup_invalid_filename")
        path = folder / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != metadata["size"] or digest(path) != metadata["sha256"]:
            raise BackupError("backup_integrity_failed")
        if name.endswith((".tar.gz", ".tar")):
            with tarfile.open(path, "r:*") as source:
                for _member in source:
                    pass


def restore_check(image, dump):
    """The only writable database here is a random, network-isolated container."""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise BackupError("backup_postgres_image_unknown")
    name = "inventory-hub-restore-" + uuid4().hex
    started = False
    try:
        run(["docker", "run", "--detach", "--rm", "--pull=never", "--network", "none", "--name", name,
             "--label", "inventory-hub.backup-verification=true", "--env", "POSTGRES_HOST_AUTH_METHOD=trust", image])
        started = True
        deadline = time.monotonic() + 60
        while True:
            try:
                run(["docker", "exec", name, "pg_isready", "--username=postgres", "--dbname=postgres"])
                break
            except BackupError:
                if time.monotonic() >= deadline:
                    raise BackupError("backup_restore_database_unavailable") from None
                time.sleep(0.5)
        run(["docker", "exec", name, "createdb", "--username=postgres", "inventory_hub_restore_check"])
        with dump.open("rb") as stream:
            run(["docker", "exec", "--interactive", name, "pg_restore", "--exit-on-error", "--single-transaction",
                 "--no-owner", "--no-privileges", "--username=postgres", "--dbname=inventory_hub_restore_check"], stdin=stream)
        # Required core tables and immutable history must be present/readable.
        query = "SELECT json_build_object(" + ",".join(
            "'" + table + "', (SELECT count(*) FROM " + table + ")" for table in
            ("products", "product_identifiers", "shop_products", "stock_balances", "stock_movements",
             "reservations", "fifo_layers", "fifo_allocations", "shop_orders")) + ")"
        result = run(["docker", "exec", name, "psql", "--username=postgres", "--dbname=inventory_hub_restore_check",
                      "--no-psqlrc", "--tuples-only", "--no-align", "--set=ON_ERROR_STOP=1", "--command", query])
        return json.loads(result)
    finally:
        if started:
            # Only this successfully created private container is removed.
            run(["docker", "rm", "--force", name])


def create_backup(compose_dir, data_dir, output_root, compose_files, *, writers_paused=False):
    if not writers_paused:
        raise BackupError("backup_pause_all_writers_required")
    compose_dir, data_dir, output_root = (Path(path).resolve() for path in (compose_dir, data_dir, output_root))
    if not compose_dir.is_dir() or not data_dir.is_dir():
        raise BackupError("backup_source_missing")
    if any(output_root == source or output_root.is_relative_to(source) for source in (compose_dir, data_dir)):
        raise BackupError("backup_output_inside_source")
    files = [Path(path) if Path(path).is_absolute() else compose_dir / path for path in compose_files]
    if not files or any(not path.is_file() or not path.resolve().is_relative_to(compose_dir) for path in files):
        raise BackupError("backup_compose_files_missing")
    compose = ["docker", "compose", "--project-directory", str(compose_dir)]
    for path in files:
        compose.extend(["--file", str(path)])
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder = output_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8])
    folder.mkdir(mode=0o700)
    manifest = {"version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "status": "incomplete",
                "writers_paused_operator_assertion": True, "restore_verified": False,
                "scope": ["postgresql", "inventory_data", "deployment_files", "supplier_secrets"], "files": {}}
    manifest_path = folder / "manifest.json"
    try:
        manifest["phase"] = "check_containers"
        api_container = run([*compose, "ps", "--all", "--quiet", "api"]).decode().strip()
        if not re.fullmatch(r"[0-9a-f]{12,64}", api_container):
            raise BackupError("backup_api_container_unknown")
        if run(["docker", "inspect", "--format", "{{.State.Running}}", api_container]).decode().strip() != "false":
            raise BackupError("backup_api_must_be_stopped")
        container = run([*compose, "ps", "--quiet", "postgres"]).decode().strip()
        if not re.fullmatch(r"[0-9a-f]{12,64}", container):
            raise BackupError("backup_postgres_container_unknown")
        image = run(["docker", "inspect", "--format", "{{.Image}}", container]).decode().strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise BackupError("backup_postgres_image_unknown")
        manifest["postgres_image"] = image
        manifest["phase"] = "database_dump"
        with (folder / "database.dump").open("xb") as stream:
            run([*compose, "exec", "--no-TTY", "postgres", "sh", "-c",
                 'exec pg_dump --no-password --format=custom --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"'], stdout=stream)
        manifest["phase"] = "filesystem_archives"
        archive(data_dir, folder / "inventory-data.tar.gz", "inventory-data")
        archive(compose_dir, folder / "deployment.tar.gz", "deployment")
        # docker cp also reads a stopped API container and its mounted volume.
        with (folder / "supplier-secrets.tar").open("xb") as stream:
            run(["docker", "cp", api_container + ":/var/lib/inventory-hub-secrets/.", "-"], stdout=stream)
        for path in sorted(folder.iterdir()):
            os.chmod(path, 0o600)
            if path.stat().st_size == 0:
                raise BackupError("backup_empty_file")
            manifest["files"][path.name] = {"size": path.stat().st_size, "sha256": digest(path)}
        manifest["phase"] = "integrity_check"
        verify_files(folder, manifest)
        manifest["phase"] = "isolated_restore"
        manifest["restored_counts"] = restore_check(image, folder / "database.dump")
        manifest.update(status="verified", phase="completed", restore_verified=True,
                        verified_at=datetime.now(timezone.utc).isoformat())
    finally:
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        os.chmod(manifest_path, 0o600)
    return folder


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-dir", default="/opt/inventory-hub")
    parser.add_argument("--data-dir", default="/opt/inventory-data")
    parser.add_argument("--output-root", default="/opt/inventory-backups")
    parser.add_argument("--compose-file", action="append", required=True,
                        help="Every active Compose file, in the same order as deployment; repeat for overlays")
    parser.add_argument("--writers-paused", action="store_true", help="Operator confirms all DB/filesystem writers are paused")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        folder = create_backup(args.compose_dir, args.data_dir, args.output_root, args.compose_file,
                               writers_paused=args.writers_paused)
        print(json.dumps({"status": "verified", "manifest": str(folder / "manifest.json")}))
    except (BackupError, OSError, ValueError, subprocess.SubprocessError, tarfile.TarError):
        print(json.dumps({"status": "failed", "error": "Backup is incomplete; do not start the pilot."}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
