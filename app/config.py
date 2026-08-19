from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True, slots=True)
class Credential:
    """一套乐享开放接口凭证及其权限组标识。"""

    id: str
    app_key: str
    app_secret: str
    group: str


class Settings(BaseSettings):
    """从环境变量或 `.env` 读取服务运行配置。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    redis_url: str = "redis://localhost:6379/0"
    lexiang_credentials: str
    gateway_api_keys: str
    allowed_origins: str = ""
    token_expiry_skew_seconds: int = 300
    conversation_ttl_seconds: int = 60 * 60 * 24 * 30
    credential_cooldown_seconds: int = 600
    upstream_timeout_seconds: float = 90.0
    # This identity is sent to Lexiang as x-staff-id for OpenAI-compatible calls.
    # system-bot can access public knowledge only; use a real authorized staff ID
    # when private knowledge needs to be searchable.
    lexiang_staff_id: str = "system-bot"
    openai_compat_model: str = "lexiang-ai"

    # --- 图片防盗链缓存配置 ---
    # 是否开启图片缓存（将乐享返回的图片下载到本地并替换 URL）
    image_cache_enabled: bool = True
    # 是否缓存 answer / reasoning_content 正文中的图片（可单独关闭）
    image_cache_answer_enabled: bool = True
    # 是否缓存 additional_content（如 reference_chunks[].content）中的图片（可单独关闭）
    image_cache_additional_enabled: bool = True
    # 图片本地缓存目录
    image_cache_dir: str = "./cache/images"
    # 对外暴露的图片服务基础 URL（如 http://your-host:8000/images）
    image_base_url: str = "http://localhost:8000/images"
    # 下载图片时伪装的 Referer，绕过防盗链（留空则不发送 Referer）
    image_referer: str = "https://lexiangla.com"

    @property
    def credentials(self) -> list[Credential]:
        """把逗号分隔的凭证配置解析为对象，并校验格式和重复 ID。"""
        result: list[Credential] = []
        ids: set[str] = set()
        for item in self.lexiang_credentials.split(","):
            parts = item.strip().split(":")
            if len(parts) not in {3, 4} or not all(parts[:3]):
                raise ValueError("LEXIANG_CREDENTIALS must use id:app_key:app_secret[:group]")
            credential = Credential(*parts[:3], parts[3] if len(parts) == 4 and parts[3] else "default")
            if credential.id in ids:
                raise ValueError(f"duplicate credential id: {credential.id}")
            ids.add(credential.id)
            result.append(credential)
        if not result:
            raise ValueError("at least one Lexiang credential is required")
        return result

    @property
    def api_keys(self) -> set[str]:
        """返回允许访问本网关的 API Key 集合，自动忽略空值和首尾空格。"""
        return {key.strip() for key in self.gateway_api_keys.split(",") if key.strip()}

    @property
    def cors_origins(self) -> list[str]:
        """把 CORS 白名单解析为 FastAPI 中间件所需的 URL 列表。"""
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    """创建并进程内缓存配置，避免每个请求重复读取环境变量。"""
    return Settings()
