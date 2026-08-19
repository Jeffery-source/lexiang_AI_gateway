from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx
from redis.asyncio import Redis

from .config import Credential, Settings
from .credentials import CredentialPool
from .exceptions import GatewayError
from .image_cache import ImageCache
from .models import ChatRequest, ChatResponse

QA_URL = "https://lxapi.lexiangla.com/cgi-bin/v1/ai/qa"


@dataclass(slots=True)
class Conversation:
    """网关保存的会话状态：外部 ID、乐享 session_id 和凭证权限组。"""

    id: str
    session_id: str | None
    group: str | None


class LexiangService:
    """协调会话、凭证池及乐享 AI 问答接口调用的核心服务。"""

    def __init__(self, redis: Redis, client: httpx.AsyncClient, settings: Settings, image_cache: ImageCache | None = None):
        """初始化服务，并创建使用同一 Redis/HTTP 客户端的凭证池。"""
        self.redis = redis
        self.client = client
        self.settings = settings
        self.pool = CredentialPool(redis, client, settings)
        self.image_cache = image_cache

    @staticmethod
    def _conversation_key(conversation_id: str) -> str:
        """生成会话状态在 Redis 中的键名。"""
        return f"lx:conversation:{conversation_id}"

    async def _load_conversation(self, request: ChatRequest) -> Conversation:
        """读取会话状态；不存在时准备一个新会话，并校验会话所属用户。"""
        conversation_id = request.conversation_id or f"c_{uuid.uuid4().hex}"
        if request.new_session:
            return Conversation(conversation_id, None, None)
        raw = await self.redis.get(self._conversation_key(conversation_id))
        if not raw:
            return Conversation(conversation_id, None, None)
        data = json.loads(raw)
        if data["user_id"] != request.user_id:
            raise GatewayError(403, "CONVERSATION_FORBIDDEN", "Conversation belongs to another user")
        return Conversation(conversation_id, data["session_id"], data.get("credential_group"))

    async def _save_conversation(self, conversation: Conversation, user_id: str, session_id: str, group: str) -> None:
        """保存乐享 session_id，令下一次相同 conversation_id 能够继续上下文。"""
        await self.redis.setex(
            self._conversation_key(conversation.id),
            self.settings.conversation_ttl_seconds,
            json.dumps({"session_id": session_id, "user_id": user_id, "credential_group": group}),
        )

    @staticmethod
    def _payload(request: ChatRequest, session_id: str | None) -> dict[str, Any]:
        """把网关请求转换为乐享 AI 问答接口的 JSON 请求体。"""
        payload: dict[str, Any] = {
            "query": request.question,
            "stream": request.stream,
            "skip_faq": request.skip_faq,
            "new_session": request.new_session or session_id is None,
            "qa_mode": request.qa_mode,
            "language": request.language,
        }
        if session_id and not payload["new_session"]:
            payload["session_id"] = session_id
        if request.anonymous_staff_id:
            payload["anonymous_staff_id"] = request.anonymous_staff_id
        if request.max_chars:
            payload["max_chars"] = request.max_chars
        if request.targets:
            payload["targets"] = [target.model_dump() for target in request.targets]
        return payload

    @staticmethod
    def _headers(token: str, user_id: str) -> dict[str, str]:
        """构造乐享要求的鉴权和成员身份请求头。"""
        return {"Authorization": f"Bearer {token}", "x-staff-id": user_id, "Content-Type": "application/json; charset=utf-8"}

    async def chat(self, request: ChatRequest) -> ChatResponse:
        """执行一次非流式问答，并处理凭证切换、token 续期和会话保存。

        收到 401 时会先刷新原凭证的 token 并携带同一 session_id 重试，
        使单凭证部署也可以继续旧会话；token 获取限流才会切换凭证。
        """
        conversation = await self._load_conversation(request)
        attempted: set[str] = set()
        refreshed_after_401: set[str] = set()
        last_error: GatewayError | None = None

        # 每套凭证最多做一次 401 刷新恢复；token 获取 429 才切换另一套凭证。
        for _ in range(len(self.settings.credentials) * 2):
            credential = await self.pool.select(conversation.group, attempted)
            try:
                token, cached_token = await self.pool.get_token(credential)
            except GatewayError as error:
                last_error = error
                attempted.add(credential.id)
                if error.retryable:
                    continue
                raise
            try:
                response = await self.client.post(
                    QA_URL,
                    headers=self._headers(token, request.user_id),
                    json=self._payload(request, conversation.session_id),
                )
            except httpx.HTTPError as error:
                raise GatewayError(502, "QA_UPSTREAM_UNAVAILABLE", "Lexiang AI Q&A is unavailable", True) from error

            if response.status_code == 401:
                await self.pool.invalidate(credential)
                # 会话属于乐享而不属于某个 token。优先刷新原凭证，单凭证部署也可续聊。
                if credential.id in refreshed_after_401:
                    attempted.add(credential.id)
                    continue
                refreshed_after_401.add(credential.id)
                try:
                    token, cached_token = await self.pool.get_token(credential)
                    response = await self.client.post(
                        QA_URL,
                        headers=self._headers(token, request.user_id),
                        json=self._payload(request, conversation.session_id),
                    )
                except GatewayError as error:
                    last_error = error
                    attempted.add(credential.id)
                    if error.retryable:
                        continue
                    raise
                except httpx.HTTPError as error:
                    raise GatewayError(502, "QA_UPSTREAM_UNAVAILABLE", "Lexiang AI Q&A is unavailable", True) from error
                if response.status_code == 401:
                    attempted.add(credential.id)
                    continue
            if response.status_code == 429:
                raise GatewayError(503, "QA_CAPACITY_EXHAUSTED", "Lexiang AI quota is exhausted; retry later", True)
            if response.status_code == 403:
                raise GatewayError(403, "QA_PERMISSION_DENIED", "Credential lacks access to the requested knowledge", False)
            if response.is_error:
                raise GatewayError(502, "QA_UPSTREAM_ERROR", "Lexiang AI Q&A returned an error", True)

            body: dict[str, Any] = response.json()
            if body.get("code") != 0 or not body.get("data"):
                raise GatewayError(502, "QA_UPSTREAM_INVALID_RESPONSE", body.get("message", "Invalid upstream response"), True)
            data = body["data"]
            session_id = data.get("session_id")
            if not session_id:
                raise GatewayError(502, "QA_SESSION_MISSING", "Lexiang response did not include a session id", True)
            await self._save_conversation(conversation, request.user_id, session_id, credential.group)
            chat_response = ChatResponse(
                conversation_id=conversation.id,
                session_id=session_id,
                answer=data.get("content", ""),
                answer_source=data.get("answer_source"),
                reasoning_content=data.get("reasoning_content"),
                additional_content=data.get("additional_content"),
                request_id=body.get("request_id"),
                cached_token=cached_token,
            )
            # 图片防盗链处理：下载图片到本地并替换 URL（answer 与 additional_content 可独立开关）
            if self.image_cache:
                chat_response = await self.image_cache.process_response(
                    chat_response,
                    process_answer=self.settings.image_cache_answer_enabled,
                    process_additional=self.settings.image_cache_additional_enabled,
                )
            return chat_response
        raise last_error or GatewayError(503, "CREDENTIAL_POOL_UNAVAILABLE", "No usable credential", True)

    async def stream(self, request: ChatRequest) -> tuple[str, AsyncIterator[bytes]]:
        """代理乐享 SSE 流，并在结束事件中获得 session_id 后保存会话状态。"""
        conversation = await self._load_conversation(request)
        credential = await self.pool.select(conversation.group)
        token, _ = await self.pool.get_token(credential)
        upstream = await self.client.send(
            self.client.build_request("POST", QA_URL, headers=self._headers(token, request.user_id), json=self._payload(request, conversation.session_id)),
            stream=True,
        )
        if upstream.status_code == 401:
            await upstream.aclose()
            await self.pool.invalidate(credential)
            raise GatewayError(401, "UPSTREAM_TOKEN_INVALID", "Retry the streaming request")
        if upstream.status_code != 200:
            await upstream.aclose()
            raise GatewayError(502, "QA_UPSTREAM_ERROR", "Lexiang AI Q&A stream could not be started", True)

        async def iterator() -> AsyncIterator[bytes]:
            """逐行转发上游 SSE，同时从完成事件提取 session_id。

            若启用了图片缓存，会先缓冲全部行、完成图片下载和 URL 替换后再转发，
            确保图片地址不会被截断且防盗链图片可正常展示。
            """
            final_session_id: str | None = None
            try:
                buffered_lines: list[str] = []
                async for line in upstream.aiter_lines():
                    buffered_lines.append(line)
                    if line.startswith("data:"):
                        try:
                            event = json.loads(line[5:])
                            if event.get("session_id"):
                                final_session_id = event["session_id"]
                        except json.JSONDecodeError:
                            pass

                # 图片防盗链处理：缓冲完成后统一替换 URL（answer 与 additional_content 可独立开关）
                if self.image_cache and final_session_id is not None:
                    buffered_lines = await self.image_cache.process_stream_lines(
                        buffered_lines,
                        process_answer=self.settings.image_cache_answer_enabled,
                        process_additional=self.settings.image_cache_additional_enabled,
                    )

                for line in buffered_lines:
                    yield (line + "\n").encode()

                if final_session_id:
                    await self._save_conversation(conversation, request.user_id, final_session_id, credential.group)
            finally:
                await upstream.aclose()

        return conversation.id, iterator()
