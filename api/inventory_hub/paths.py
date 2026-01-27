
from __future__ import annotations
from pathlib import Path

def shop_imports_dir(data_root: Path, shop: str, kind: str = "upgates") -> Path:
    p = Path(data_root) / "shops" / shop / "imports" / kind
    p.mkdir(parents=True, exist_ok=True)
    return p
