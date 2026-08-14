from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
from redis.asyncio import Redis

from .config import Credential, Settings
from .exceptions import GatewayError

TOKEN_URL = "https://lxapi.lexiangla.com/cgi-bin/token"


class CredentialPool:
    """管理多套乐享凭证、Redis token 缓存和限流冷却状态。"""

    def __init__(self, redis: Redis, client: httpx.AsyncClient, settings: Settings):
        """注入共享 Redis、复用 HTTP 客户端及运行配置。"""
        self.redis = redis
        self.client = client
        self.settings = settings

    @staticmethod
    def _token_key(credential: Credential) -> str:
        """生成指定凭证的 token 缓存键。"""
        return f"lx:credential:{credential.id}:token"

    @staticmethod
    def _cooldown_key(credential: Credential) -> str:
        """生成指定凭证触发 token 限流后的冷却标记键。"""
        return f"lx:credential:{credential.id}:cooldown"

    @staticmethod
    def _disabled_key(credential: Credential) -> str:
        """生成凭证永久失效（AppKey/AppSecret 错误）的标记键。"""
        return f"lx:credential:{credential.id}:disabled"

    async def select(self, group: str | None = None, exclude: set[str] | None = None) -> Credential:
        """选出可用凭证。

        会话续聊时限制在同一权限组；优先使用已有有效 token 的凭证，
        再按最近最少使用原则分摊负载。
        """
        exclude = exclude or set()
        candidates = [c for c in self.settings.credentials if c.id not in exclude and (not group or c.group == group)]
        if not candidates:
            raise GatewayError(503, "NO_COMPATIBLE_CREDENTIAL", "No credential matches this conversation", True)

        viable: list[Credential] = []
        for credential in candidates:
            if not await self.redis.exists(self._cooldown_key(credential)) and not await self.redis.exists(
                self._disabled_key(credential)
            ):
                viable.append(credential)
        if not viable:
            raise GatewayError(503, "CREDENTIAL_POOL_UNAVAILABLE", "All Lexiang credentials are cooling down", True)

        # Prefer an existing valid token; then least recently assigned credential.
        now = int(time.time())
        cached: list[Credential] = []
        for credential in viable:
            raw = await self.redis.get(self._token_key(credential))
            if raw and json.loads(raw).get("expires_at", 0) > now:
                cached.append(credential)
        pool = cached or viable
        scores = await self.redis.zmscore("lx:credential:last_used", [credential.id for credential in pool])
        selected = min(zip(pool, scores), key=lambda pair: pair[1] or 0)[0]
        await self.redis.zadd("lx:credential:last_used", {selected.id: time.time()})
        return selected

    async def invalidate(self, credential: Credential) -> None:
        """删除疑似已失效的 token，使下次请求强制重新获取。"""
        await self.redis.delete(self._token_key(credential))

    async def get_token(self, credential: Credential) -> tuple[str, bool]:
        """获取有效 token，返回 `(token, 是否命中缓存)`。

        通过 Redis 分布式锁把并发刷新合并为一次上游请求，避免触发乐享
        token 接口的频率限制。
        """
        now = int(time.time())
        raw = await self.redis.get(self._token_key(credential))
        if raw:
            cached = json.loads(raw)
            if cached["expires_at"] > now:
                return cached["access_token"], True

        lock_key = f"lx:lock:token:{credential.id}"
        acquired = await self.redis.set(lock_key, "1", nx=True, ex=30)
        if not acquired:
            # A different instance is refreshing. Wait briefly rather than consuming rate limit.
            for _ in range(30):
                await asyncio.sleep(0.1)
                raw = await self.redis.get(self._token_key(credential))
                if raw:
                    cached = json.loads(raw)
                    if cached["expires_at"] > int(time.time()):
                        return cached["access_token"], True
            raise GatewayError(503, "TOKEN_REFRESH_IN_PROGRESS", "Token refresh is taking too long", True)

        try:
            # Double-check after obtaining the lock.
            raw = await self.redis.get(self._token_key(credential))
            if raw:
                cached = json.loads(raw)
                if cached["expires_at"] > int(time.time()):
                    return cached["access_token"], True
            return await self._fetch_token(credential)
        finally:
            await self.redis.delete(lock_key)

    async def _fetch_token(self, credential: Credential) -> tuple[str, bool]:
        """实际调用乐享 token 接口，并根据状态码更新凭证健康状态。"""
        try:
            response = await self.client.post(
                TOKEN_URL,
                json={"grant_type": "client_credentials", "app_key": credential.app_key, "app_secret": credential.app_secret},
            )
        except httpx.HTTPError as error:
            raise GatewayError(502, "TOKEN_UPSTREAM_UNAVAILABLE", "Could not obtain Lexiang token", True) from error

        if response.status_code == 429:
            await self.redis.setex(self._cooldown_key(credential), self.settings.credential_cooldown_seconds, "token_rate_limit")
            raise GatewayError(429, "CREDENTIAL_TOKEN_RATE_LIMITED", "Credential token endpoint is rate limited", True)
        if response.status_code in {400, 401}:
            await self.redis.set(self._disabled_key(credential), "invalid_credentials")
            raise GatewayError(503, "CREDENTIAL_INVALID", f"Credential {credential.id} was disabled", False)
        if response.is_error:
            raise GatewayError(502, "TOKEN_UPSTREAM_ERROR", "Lexiang token endpoint returned an error", True)

        payload: dict[str, Any] = response.json()
        token = payload.get("access_token")
        expires_in = int(payload.get("expires_in", 0))
        if not token or expires_in <= self.settings.token_expiry_skew_seconds:
            raise GatewayError(502, "TOKEN_UPSTREAM_INVALID_RESPONSE", "Lexiang returned an unusable token", True)
        expires_at = int(time.time()) + expires_in - self.settings.token_expiry_skew_seconds
        await self.redis.setex(
            self._token_key(credential),
            max(1, expires_in - self.settings.token_expiry_skew_seconds),
            json.dumps({"access_token": token, "expires_at": expires_at}),
        )
        return token, False
