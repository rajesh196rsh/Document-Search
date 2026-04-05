from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # App
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    APP_ENV: str = "development"
    LOG_LEVEL: str = "info"

    # SQLite
    SQLITE_DB_PATH: str = "./data/docsearch.db"

    # Standalone mode — run with just SQLite, no ES/Redis/RabbitMQ
    STANDALONE_MODE: bool = False

    # Elasticsearch
    ELASTICSEARCH_HOST: str = "elasticsearch"
    ELASTICSEARCH_PORT: int = 9200

    # Redis
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379

    # RabbitMQ
    RABBITMQ_HOST: str = "rabbitmq"
    RABBITMQ_PORT: int = 5672
    RABBITMQ_USER: str = "guest"
    RABBITMQ_PASSWORD: str = "guest"

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.SQLITE_DB_PATH}"

    @property
    def elasticsearch_url(self) -> str:
        return f"http://{self.ELASTICSEARCH_HOST}:{self.ELASTICSEARCH_PORT}"

    @property
    def redis_url(self) -> str:
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}"

    @property
    def rabbitmq_url(self) -> str:
        return (
            f"amqp://{self.RABBITMQ_USER}:{self.RABBITMQ_PASSWORD}"
            f"@{self.RABBITMQ_HOST}:{self.RABBITMQ_PORT}/"
        )

    class Config:
        env_file = ".env.example"


settings = Settings()
