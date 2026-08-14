from __future__ import annotations

import hmac
import json
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import Depends, FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from redis.asyncio import Redis

from .config import Settings, get_settings
from .exceptions import GatewayError
from .models import ChatRequest, ChatResponse, OpenAIChatCompletionRequest
from .openai_compat import completion_chunks, completion_payload, to_lexiang_request
from .service import LexiangService


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用启动时建立 Redis/HTTP 连接，关闭时统一释放资源。"""
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    await redis.ping()
    app.state.redis = redis
    app.state.http = httpx.AsyncClient(timeout=settings.upstream_timeout_seconds)
    app.state.settings = settings
    try:
        yield
    finally:
        await app.state.http.aclose()
        await redis.aclose()


app = FastAPI(title="Lexiang AI Gateway", version="0.1.0", lifespan=lifespan)


@app.exception_handler(GatewayError)
async def gateway_error_handler(_: Request, error: GatewayError) -> JSONResponse:
    """将内部 GatewayError 转成统一且不泄露敏感信息的 JSON 错误响应。"""
    return JSONResponse(
        status_code=error.status_code,
        content={"error": {"code": error.code, "message": error.detail, "retryable": error.retryable}},
    )


def get_service(request: Request) -> LexiangService:
    """从应用共享状态构造一次请求所需的乐享服务对象。"""
    return LexiangService(request.app.state.redis, request.app.state.http, request.app.state.settings)


async def require_api_key(
    request: Request, authorization: str | None = Header(default=None),
) -> str:
    """校验 Bearer 网关 API Key，并返回原始 key 供会话隔离计算使用。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise GatewayError(401, "UNAUTHORIZED", "A gateway API key is required")
    provided = authorization.removeprefix("Bearer ")
    keys = request.app.state.settings.api_keys
    if not any(hmac.compare_digest(provided, expected) for expected in keys):
        raise GatewayError(401, "UNAUTHORIZED", "Invalid gateway API key")
    return provided


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, str]:
    """健康检查：确认 Web 服务和 Redis 都可响应。"""
    await request.app.state.redis.ping()
    return {"status": "ok"}


@app.post("/v1/chat", response_model=ChatResponse, dependencies=[Depends(require_api_key)])
async def chat(body: ChatRequest, service: LexiangService = Depends(get_service)) -> ChatResponse:
    """提供本网关原生的非流式问答接口。"""
    if body.stream:
        raise GatewayError(400, "USE_STREAM_ENDPOINT", "Use POST /v1/chat/stream for streaming")
    return await service.chat(body)


@app.post("/v1/chat/stream", dependencies=[Depends(require_api_key)])
async def stream_chat(body: ChatRequest, service: LexiangService = Depends(get_service)) -> StreamingResponse:
    """提供本网关原生 SSE 代理接口，直接转发乐享流式事件。"""
    body.stream = True
    conversation_id, events = await service.stream(body)
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={"X-Conversation-Id": conversation_id, "Cache-Control": "no-cache"},
    )


@app.get("/v1/models", dependencies=[Depends(require_api_key)])
async def list_models(request: Request) -> dict[str, object]:
    """返回 OpenAI 客户端可发现的唯一兼容模型。"""
    model = request.app.state.settings.openai_compat_model
    return {"object": "list", "data": [{"id": model, "object": "model", "owned_by": "lexiang"}]}


@app.post("/v1/chat/completions", response_model=None)
async def openai_chat_completions(
    body: OpenAIChatCompletionRequest,
    request: Request,
    x_conversation_id: str | None = Header(default=None),
    api_key: str = Depends(require_api_key),
    service: LexiangService = Depends(get_service),
) -> Response:
    """OpenAI Chat Completions 兼容端点，供 OpenClaw 等客户端调用。"""
    settings: Settings = request.app.state.settings
    lexiang_request = to_lexiang_request(body, settings, api_key, x_conversation_id)
    response = await service.chat(lexiang_request)
    model = settings.openai_compat_model
    headers = {"X-Lexiang-Conversation-Id": response.conversation_id}
    if not body.stream:
        return JSONResponse(completion_payload(response, model), headers=headers)

    async def events() -> AsyncIterator[bytes]:
        """把完整回答封装为 OpenAI 兼容 SSE 事件并在末尾发送 [DONE]。"""
        # 上游回答已完整生成；这里兼容 OpenAI SSE 事件格式，但并非逐 token 代理。
        for chunk in completion_chunks(response, model):
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={**headers, "Cache-Control": "no-cache"},
    )


settings_for_cors = get_settings()
if settings_for_cors.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings_for_cors.cors_origins,
        allow_credentials=False,
        allow_methods=["POST", "GET"],
        allow_headers=["Authorization", "Content-Type"],
    )
