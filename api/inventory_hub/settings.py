# C:\!kafe\BikeTrek\web\api\inventory_hub\settings.py
from __future__ import annotations
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, AliasChoices

class Settings(BaseSettings):
    # Akceptujeme nové aj legacy názvy premenných:
    INVENTORY_DATA_ROOT: Path = Field(
        default=(Path(__file__).resolve().parents[2] / "inventory-data"),
        validation_alias=AliasChoices("INVENTORY_DATA_ROOT", "ih_data_root", "api_base_path"),
    )
    INVENTORY_DB_URL: str = Field(
        default=f"sqlite:///{(Path(__file__).resolve().parents[1] / 'inventory.db').as_posix()}",
        validation_alias=AliasChoices("INVENTORY_DB_URL", "ih_database_url"),
    )

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parents[1] / ".env"),
        env_prefix="",
        case_sensitive=False,
        extra="ignore",   # <-- kritické: ignoruj neznáme položky v .env
    )

settings = Settings()
INVENTORY_DATA_ROOT = settings.INVENTORY_DATA_ROOT
INVENTORY_DB_URL    = settings.INVENTORY_DB_URL
