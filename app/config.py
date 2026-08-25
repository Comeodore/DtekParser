from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings

PARSE_INTERVAL_SECONDS = 60

PARSER_CONFIGS = {
    "kem": {
        "url": "https://www.dtek-kem.com.ua/ua/shutdowns",
        "schedule_id": 1,
    },
    "krem": {
        "url": "https://www.dtek-krem.com.ua/ua/shutdowns",
        "schedule_id": 2,
    },
}


class Settings(BaseSettings):
    database_url: str
    parser_type: str = "kem"
    street: str = Field(validation_alias="DTEK_STREET")
    building: str = Field(validation_alias="DTEK_BUILDING")
    settlement: Optional[str] = Field(default=None, validation_alias="DTEK_SETTLEMENT")

    class Config:
        env_file = ".env"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()
