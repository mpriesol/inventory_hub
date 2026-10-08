"""Persistent model selection; reads never seed or modify application state."""
from datetime import datetime, timezone

from pydantic import Field
from sqlalchemy import text

from inventory_hub.ai_content_models import AiContentSettings
from inventory_hub.ai_content_types import StrictModel
from inventory_hub.services.ai_content_provider import RATES, LONG_CONTEXT_THRESHOLD
from inventory_hub.services.catalog import CatalogError
from inventory_hub.settings import settings


class ModelSettingsSave(StrictModel):
    expected_revision: int = Field(ge=0)
    model: str = Field(min_length=1, max_length=100)


def available_models() -> list[dict]:
    labels = {"gpt-6.1-sol": "GPT-6.1 Sol", "gpt-6-luna": "GPT-6 Luna",
              "gpt-6-astra": "GPT-6 Astra", "gpt-5.6-sol": "GPT-5.6 Sol"}
    return [{"id": model, "label": label,
             "input_usd_per_million": RATES[model]["input"],
             "cached_input_usd_per_million": RATES[model]["cached"],
             "cache_write_usd_per_million": RATES[model]["cache_write"],
             "output_usd_per_million": RATES[model]["output"],
             "web_search_usd": RATES[model]["search"],
             "context_tokens": 1050000,
             "long_context_threshold": LONG_CONTEXT_THRESHOLD,
             "long_context_input_usd_per_million": RATES[model]["long_input"],
             "long_context_cached_input_usd_per_million": RATES[model]["long_cached"],
             "long_context_cache_write_usd_per_million": RATES[model]["long_cache_write"],
             "long_context_output_usd_per_million": RATES[model]["long_output"]}
            for model, label in labels.items()]


async def effective_model(db) -> str:
    saved = await db.get(AiContentSettings, 1)
    return saved.model if saved else settings.AI_CONTENT_MODEL


def summary(saved: AiContentSettings | None) -> dict:
    return {"revision": saved.revision if saved else 0,
            "model": saved.model if saved else settings.AI_CONTENT_MODEL,
            "source": "hub" if saved else "server", "models": available_models(),
            "updated_at": saved.updated_at.isoformat() if saved else None}


async def read(db) -> dict:
    return summary(await db.get(AiContentSettings, 1))


async def save(db, request: ModelSettingsSave) -> dict:
    if request.model not in RATES:
        raise CatalogError("ai_model_unpriced", "Choose a model with a verified rate card", 422)
    # Serialize both initial insertion and later CAS updates, including empty state.
    await db.execute(text("SELECT pg_advisory_xact_lock(691432123)"))
    saved = await db.get(AiContentSettings, 1, populate_existing=True)
    revision = saved.revision if saved else 0
    if request.expected_revision != revision:
        raise CatalogError("ai_settings_changed", "AI settings changed; reload before saving", 409)
    if saved is None:
        saved = AiContentSettings(id=1, revision=0)
        db.add(saved)
    saved.model = request.model
    saved.revision += 1
    saved.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return summary(saved)
