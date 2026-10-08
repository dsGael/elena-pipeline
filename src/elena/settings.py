from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"

    db_host: str
    db_port: int = 5432
    db_name: str
    db_user: str
    db_password: str

    app_timezone: str = "America/Hermosillo"
    algorithm_version: str = "ELENA_V2"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )


settings = Settings()