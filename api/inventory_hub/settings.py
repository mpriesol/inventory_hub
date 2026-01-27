# inventory_hub/settings.py
"""
Inventory Hub Settings - v12 FINAL with PostgreSQL support.
"""
from __future__ import annotations
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, AliasChoices

class Settings(BaseSettings):
    # =========================================================================
    # File Storage (legacy JSON files, invoices, feeds)
    # =========================================================================
    INVENTORY_DATA_ROOT: Path = Field(
        default=(Path(__file__).resolve().parents[2] / "inventory-data"),
        validation_alias=AliasChoices("INVENTORY_DATA_ROOT", "ih_data_root", "api_base_path"),
    )
    
    # =========================================================================
    # PostgreSQL Database (v12 FINAL)
    # =========================================================================
    DB_HOST: str = Field(default="localhost", validation_alias="DB_HOST")
    DB_PORT: int = Field(default=5432, validation_alias="DB_PORT")
    DB_NAME: str = Field(default="inventory_hub", validation_alias="DB_NAME")
    DB_USER: str = Field(default="postgres", validation_alias="DB_USER")
    DB_PASSWORD: str = Field(default="postgres", validation_alias="DB_PASSWORD")
    
    # Connection pool settings
    DB_POOL_SIZE: int = Field(default=5, validation_alias="DB_POOL_SIZE")
    DB_MAX_OVERFLOW: int = Field(default=10, validation_alias="DB_MAX_OVERFLOW")
    DB_ECHO: bool = Field(default=False, validation_alias="DB_ECHO")
    
    # Legacy SQLite URL (for backward compatibility during migration)
    INVENTORY_DB_URL: str = Field(
        default=f"sqlite:///{(Path(__file__).resolve().parents[1] / 'inventory.db').as_posix()}",
        validation_alias=AliasChoices("INVENTORY_DB_URL", "ih_database_url"),
    )
    
    # =========================================================================
    # Feature Flags
    # =========================================================================
    USE_POSTGRES: bool = Field(
        default=True,
        description="Use PostgreSQL instead of JSON files for main data"
    )
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

settings = Settings()

# Legacy exports for backward compatibility
INVENTORY_DATA_ROOT = settings.INVENTORY_DATA_ROOT
INVENTORY_DB_URL = settings.INVENTORY_DB_URL
