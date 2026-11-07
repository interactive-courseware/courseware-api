from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CW_", env_file=".env", extra="ignore")
    database_url: str
    secret_key: str = Field(min_length=32)
    public_origin: str = "http://127.0.0.1:8814"
    storage_path: Path = Path(".local/storage")
    secure_cookies: bool = True
    allow_insecure_http: bool = False
    x_accel: bool = False
    allowed_git_hosts: str = "github.com,farfarfun-cn-hangzhou.devops.aliyuncs.com"
    use_local_git_credentials: bool = False
    session_hours: int = 168
    idle_hours: int = 24
    max_files: int = 20000
    max_file_bytes: int = 50 * 1024 * 1024
    max_package_bytes: int = 500 * 1024 * 1024
    max_git_bytes: int = 1024 * 1024 * 1024

    @model_validator(mode="after")
    def production_cookie(self):
        from urllib.parse import urlsplit

        parsed = urlsplit(self.public_origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
            raise ValueError("CW_PUBLIC_ORIGIN must be an HTTP(S) origin")
        if not self.secure_cookies and parsed.hostname not in {"localhost", "127.0.0.1", "testserver"}:
            if parsed.scheme != "http" or not self.allow_insecure_http:
                raise ValueError("Public HTTP requires explicit CW_ALLOW_INSECURE_HTTP=true; HTTPS requires Secure cookies")
        self.public_origin = self.public_origin.rstrip("/")
        self.storage_path = self.storage_path.resolve()
        return self


@lru_cache
def settings() -> Settings:
    return Settings()
