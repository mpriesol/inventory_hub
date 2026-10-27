#!/usr/bin/env python3
"""Preview/apply deletion of old, unreferenced catalog_stock XML copies only.

Run on the host with read access to every supplier config. No third-party
dependencies, database access, network calls or recursive deletion.
"""
import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import stat
import time


STOCK_COPY = re.compile(r"catalog_stock_\d{8}T\d{6}_[0-9a-f]{8}\.xml")


@dataclass(frozen=True)
class Candidate:
    path: Path
    fingerprint: tuple


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def protected_sources(suppliers):
    protected = set()
    # A different supplier may reference this supplier's local file too.
    for config in suppliers.glob("*/config.json"):
        cfg = json.loads(config.read_text(encoding="utf-8"))
        for source in (cfg.get("feeds", {}).get("sources") or {}).values():
            if not isinstance(source, dict):
                raise ValueError("Invalid supplier source configuration; cleanup stopped")
            location = source.get("local_path")
            if location:
                path = Path(location)
                path = path if path.is_absolute() else config.parent / path
                protected.add(path.resolve())
                # Container-local /data/... corresponds to the host data root.
                # Also conservatively protect matching supplier-relative tails.
                if "suppliers" in path.parts:
                    index = path.parts.index("suppliers")
                    protected.add(suppliers.joinpath(*path.parts[index + 1:]).resolve())
    return protected


def candidates(data_root, supplier, hours, at=None):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,50}", supplier) or hours < 72:
        raise ValueError("Use a valid supplier code and retain at least 72 hours")
    root = data_root.resolve(strict=True)
    suppliers = root / "suppliers"
    directory = suppliers / supplier / "feeds/xml"
    if directory.resolve(strict=True) != directory:
        raise ValueError("Refusing a symlinked supplier/feed directory")
    # A missing target config is not evidence that no input is pinned.
    if not (suppliers / supplier / "config.json").is_file():
        raise ValueError("Target supplier config.json is required")
    protected = protected_sources(suppliers)
    copies = []
    for path in directory.iterdir():
        if STOCK_COPY.fullmatch(path.name):
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                copies.append((path, info))
    copies.sort(key=lambda item: (item[1].st_mtime_ns, item[0].name), reverse=True)
    cutoff = (time.time() if at is None else at) - hours * 3600
    # Keep the newest two even when every copy is older than the cutoff.
    return [Candidate(path, fingerprint(info)) for path, info in copies[2:]
            if info.st_mtime < cutoff and path.resolve() not in protected]


def remove_selected(selected):
    count = size = 0
    for item in selected:
        info = item.path.lstat()
        if not stat.S_ISREG(info.st_mode) or fingerprint(info) != item.fingerprint:
            raise RuntimeError("A selected file changed; cleanup stopped")
        item.path.unlink()
        count += 1
        size += info.st_size
    return count, size


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("/opt/inventory-data"))
    parser.add_argument("--supplier", required=True)
    parser.add_argument("--older-than-hours", type=int, default=72)
    parser.add_argument("--apply", action="store_true", help="Delete the printed candidates")
    args = parser.parse_args()
    selected = candidates(args.data_root, args.supplier, args.older_than_hours)
    total = sum(item.fingerprint[2] for item in selected)
    print(f"{'APPLY' if args.apply else 'PREVIEW'}: {len(selected)} files, {total / 1024**3:.2f} GiB")
    for item in selected:
        print(item.path)
    if args.apply:
        count, size = remove_selected(selected)
        print(f"Deleted: {count} files, {size / 1024**3:.2f} GiB")
    else:
        print("No files deleted. Add --apply to delete these old XML copies.")


if __name__ == "__main__":
    main()
