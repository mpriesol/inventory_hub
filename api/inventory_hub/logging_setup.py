# -*- coding: utf-8 -*-
from __future__ import annotations
import logging, logging.handlers
from pathlib import Path

def setup_logging(settings) -> Path:
    """Configure rotating file logging under INVENTORY_DATA_ROOT/logs/inventory_hub.log"""
    root = Path(settings.INVENTORY_DATA_ROOT).expanduser()
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "inventory_hub.log"

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s — %(message)s")
    handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(fmt)
    handler.setLevel(logging.INFO)

    logger = logging.getLogger()  # root
    logger.setLevel(logging.INFO)
    # avoid duplicate handlers
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) and getattr(h, 'baseFilename', '').endswith("inventory_hub.log") for h in logger.handlers):
        logger.addHandler(handler)

    # also wire uvicorn loggers (if present)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        if not any(getattr(h, 'baseFilename', '').endswith("inventory_hub.log") for h in lg.handlers if hasattr(h, 'baseFilename')):
            lg.addHandler(handler)

    return log_path
