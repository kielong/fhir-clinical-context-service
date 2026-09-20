# ORIGIN: AI — settings plumbing typed by Claude Code, reviewed by Kiel.
"""Every tunable lives here and in .env.example. Nothing else reads the environment."""

from datetime import date
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


# ORIGIN: H-spec — the knob names and defaults are Kiel's decisions (including as_of_date, the
#   reference date for a dataset frozen in 2019). Lines typed by Claude Code.
class Settings(BaseSettings):
    # env_ignore_empty: `AS_OF_DATE=` (or an empty SEED_LIMIT) means "unset", not a parse error.
    # extra="ignore": .env also holds POSTGRES_* and HAPI_JAVA_OPTS, which only compose reads.
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    # FHIR
    fhir_base_url: str = "http://hapi:8080/fhir"
    public_fhir_base_url: str = "http://localhost:8080/fhir"
    fhir_timeout_seconds: float = 10
    fhir_page_size: int = 100  # sent as _count: a PAGE size, never a result cap
    fhir_max_pages: int = 50  # per resource type; exceeding it is an error, not a silent cap

    # Packet
    list_cap: int = 25
    as_of_date: date | None = None  # None = today (UTC); the Synthea sample needs 2019-09-16

    # Ollama
    ollama_host: str = "http://ollama:11434"
    ollama_model: str = "llama3.2:3b"
    ollama_timeout_seconds: float = 120  # the whole summarizer call, retry included
    ollama_num_ctx: int = 4096
    ollama_num_predict: int = 160
    ollama_keep_alive: str = "30m"
    ollama_warmup: bool = True

    # Seed
    seed_data_dir: str = "data/synthea/fhir"
    seed_priority_file: str = "scripts/seed_priority.txt"
    seed_limit: int | None = None
    seed_timeout_seconds: float = 900


@lru_cache
def get_settings() -> Settings:
    return Settings()
