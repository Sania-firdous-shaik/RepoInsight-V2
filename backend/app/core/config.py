from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
    env_file="../.env",
    extra="ignore"
)
    app_env: str = "development"
    secret_key: str = "change-me"
    jwt_secret_key: str = "change-me-too"
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/repoinsight"
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    groq_api_key: str = ""
    groq_model: str = ""
    groq_temperature: float = 0.2
    groq_max_tokens: int = 4096
    review_ai_concurrency: int = 1
    review_ai_min_interval_seconds: float = 2.5
    review_ai_max_input_chars: int = 8000
    review_ai_max_retries: int = 3
    review_ai_backoff_base_seconds: float = 2.0
    review_ai_max_retry_wait_seconds: float = 120.0
    review_ai_retry_jitter_seconds: float = 0.8
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    frontend_url: str = "http://localhost:5173"
    max_repository_size_mb: int = 200
    max_file_size_mb: int = 2
    max_files: int = 3000
    access_token_minutes: int = 1440
    rag_context_token_budget: int = 8000
    rag_max_context_chunks: int = 16
    rag_explanation_section_token_budget: int = 6000

settings = Settings()
