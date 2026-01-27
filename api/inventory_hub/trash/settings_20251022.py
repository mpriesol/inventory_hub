
from pathlib import Path
from pydantic import Field, AliasChoices
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    INVENTORY_DATA_ROOT: Path = Field(
        default=Path(r"C:/!kafe/BikeTrek/web/inventory-data"),
        validation_alias=AliasChoices("INVENTORY_DATA_ROOT", "IH_DATA_ROOT", "ih_data_root"),
    )
    API_BASE_PATH: Path = Field(
        default=Path(r"C:/!kafe/BikeTrek/web/api"),
        validation_alias=AliasChoices("API_BASE_PATH", "IH_API_PATH", "ih_api_path"),
    )
    PIPELINE_LOCK_NAME: str = "pipeline.lock"

    IH_DATABASE_URL: str | None = Field(
        default=None,
        validation_alias=AliasChoices("IH_DATABASE_URL", "ih_database_url"),
    )

settings = Settings()
