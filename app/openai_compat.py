from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any

from .config import Settings
from .exceptions import GatewayError
from .models import ChatRequest, ChatResponse, OpenAIChatCompletionRequest


def to_lexiang_request(
    body: OpenAIChatCompletionRequest,
    settings: Settings,
    api_key: str,
    conversation_hint: str | None,
) -> ChatRequest:
    """把 OpenAI Chat Completions 请求转换为网关原生问答请求。

    取最后一条 user 消息作为乐享问题，并以 API Key 加稳定会话标识生成
    隔离的 conversation_id，防止不同调用方共享乐享上下文。
    """
    user_messages = [message.content for message in body.messages if message.role == "user" and message.content]
    if not user_messages:
        raise GatewayError(400, "INVALID_REQUEST", "messages must include a user message")
    if body.model and body.model != settings.openai_compat_model:
        raise GatewayError(404, "MODEL_NOT_FOUND", f"Unknown model: {body.model}")

    # OpenAI 没有标准会话 ID；OpenClaw 可把稳定频道/用户 ID 放进 user，
    # 其他集成也可以使用 X-Conversation-Id 请求头。
    scope = conversation_hint or body.user
    if scope:
        digest = hashlib.blake2s(f"{api_key}:{scope}".encode(), digest_size=16).hexdigest()
        conversation_id = f"oai_{digest}"
        new_session = False
    else:
        conversation_id = f"oai_{uuid.uuid4().hex}"
        new_session = True

    anonymous_staff_id = None
    if settings.lexiang_staff_id == "system-bot":
        # 乐享要求 system-bot 模式下附带由业务方生成的 16–32 位匿名用户 ID。
        anonymous_staff_id = hashlib.sha256(f"{api_key}:{scope or conversation_id}".encode()).hexdigest()[:32]

    return ChatRequest(
        question=user_messages[-1],
        conversation_id=conversation_id,
        user_id=settings.lexiang_staff_id,
        new_session=new_session,
        stream=False,
        anonymous_staff_id=anonymous_staff_id,
        # 将兼容接口的乐享扩展字段继续传给上游，而非仅接受后忽略。
        qa_mode=body.qa_mode,
        language=body.language,
        skip_faq=body.skip_faq,
        targets=body.targets,
        max_chars=body.max_tokens,
    )


def completion_payload(response: ChatResponse, model: str) -> dict[str, Any]:
    """把乐享的完整回答转换为非流式 OpenAI Chat Completion 响应。"""
    return {
        "id": f"chatcmpl_{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": response.answer},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def completion_chunks(response: ChatResponse, model: str) -> list[dict[str, Any]]:
    """生成 OpenAI SSE 格式的回答块和结束块。

    乐享结果已在此之前完整取得，因此这里是协议兼容的两块输出，而非逐 token 输出。
    """
    completion_id = f"chatcmpl_{uuid.uuid4().hex}"
    created = int(time.time())
    common = {"id": completion_id, "object": "chat.completion.chunk", "created": created, "model": model}
    return [
        {**common, "choices": [{"index": 0, "delta": {"role": "assistant", "content": response.answer}, "finish_reason": None}]},
        {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
