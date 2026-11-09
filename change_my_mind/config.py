from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEBATE_", env_file=".env", extra="ignore")
    data_dir: Path = Path("data")
    host: str = "127.0.0.1"
    port: int = 8000
    allow_paid_api: bool = False
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    frontend_dir: Path = Path(__file__).resolve().parent.parent / "frontend" / "dist"


PRICING = {
    "gpt-6.1-sol": {"input": 2.0, "cached_input": 0.10, "output": 10.0},
    "gpt-6-astra": {"input": 10.0, "cached_input": 1.0, "output": 50.0},
    "gpt-6.1-terra": {"input": 1.0, "cached_input": 0.05, "output": 5.0},
    "gpt-6-luna": {"input": 0.10, "cached_input": 0.01, "output": 0.50},
}
PRICE_VERSION = "openai-standard-2026-11-08"
SEARCH_PRICE = 0.01
STAGES = [
    "framing",
    "research",
    "dossier",
    "openings",
    "responses",
    "rebuttals",
    "follow_up",
    "closings",
    "judgment",
    "insight",
    "consistency",
    "complete",
]
TERMINAL = {"completed", "failed", "cancelled", "budget_exhausted", "needs_review"}
