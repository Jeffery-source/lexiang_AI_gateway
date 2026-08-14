from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class Target(BaseModel):
    """限定乐享问答检索范围的知识库目标。"""

    type: Literal["space", "team", "team_code", "kb_entry"]
    id: str = Field(min_length=1)


class ChatRequest(BaseModel):
    """本网关原生 `/v1/chat` 接口的请求参数。"""

    question: str = Field(min_length=1, max_length=1024)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    new_session: bool = False
    stream: bool = False
    anonymous_staff_id: str | None = Field(default=None, min_length=16, max_length=32)
    skip_faq: bool = False
    qa_mode: str = "normal"
    max_chars: int | None = Field(default=None, gt=0)
    language: Literal["zh-CN", "en", "auto"] = "zh-CN"
    targets: list[Target] | None = Field(default=None, max_length=20)

    @field_validator("conversation_id")
    @classmethod
    def strip_conversation_id(cls, value: str | None) -> str | None:
        """去掉会话 ID 首尾空格，避免同一会话被意外拆分为多个键。"""
        return value.strip() if value else value


class ChatResponse(BaseModel):
    """本网关原生问答接口的统一响应结构。"""

    conversation_id: str
    session_id: str
    answer: str
    answer_source: str | None = None
    reasoning_content: str | None = None
    additional_content: dict[str, Any] | None = None
    request_id: str | None = None
    cached_token: bool


class OpenAIMessage(BaseModel):
    """OpenAI Chat Completions 请求中的单条消息。"""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None


class OpenAIChatCompletionRequest(BaseModel):
    """本服务支持的 OpenAI Chat Completions 请求字段及乐享扩展字段。

    `qa_mode`、`language`、`skip_faq` 和 `targets` 不是 OpenAI 标准字段，
    但会被网关透传到乐享 AI 问答接口。
    """

    model: str | None = None
    messages: list[OpenAIMessage] = Field(min_length=1)
    stream: bool = False
    user: str | None = Field(default=None, max_length=128)
    max_tokens: int | None = Field(default=None, gt=0)
    qa_mode: str = "normal"
    language: Literal["zh-CN", "en", "auto"] = "zh-CN"
    skip_faq: bool = False
    targets: list[Target] | None = Field(default=None, max_length=20)
