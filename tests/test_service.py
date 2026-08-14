import json

import httpx
import pytest
import respx
from fakeredis.aioredis import FakeRedis

from app.config import Settings
from app.credentials import TOKEN_URL
from app.models import ChatRequest, OpenAIChatCompletionRequest, OpenAIMessage
from app.openai_compat import to_lexiang_request
from app.service import QA_URL, LexiangService


@pytest.fixture
def settings() -> Settings:
    return Settings(
        lexiang_credentials="one:key-1:secret-1:default,two:key-2:secret-2:default",
        gateway_api_keys="test-key",
        token_expiry_skew_seconds=60,
    )


@pytest.mark.asyncio
async def test_uses_cached_token_and_persists_session(settings: Settings) -> None:
    redis = FakeRedis(decode_responses=True)
    async with httpx.AsyncClient() as client:
        service = LexiangService(redis, client, settings)
        await redis.setex(
            "lx:credential:one:token", 3600, json.dumps({"access_token": "cached", "expires_at": 4_102_444_800})
        )
        with respx.mock(assert_all_called=True) as router:
            route = router.post(QA_URL).respond(
                200,
                json={"code": 0, "request_id": "r1", "data": {"content": "answer", "session_id": "s" * 40}},
            )
            response = await service.chat(ChatRequest(question="hello", user_id="staff-1"))
        assert route.called
        assert response.cached_token is True
        assert response.answer == "answer"
        conversation = json.loads(await redis.get(f"lx:conversation:{response.conversation_id}"))
        assert conversation["session_id"] == "s" * 40


@pytest.mark.asyncio
async def test_token_rate_limit_falls_back_to_next_credential(settings: Settings) -> None:
    redis = FakeRedis(decode_responses=True)
    async with httpx.AsyncClient() as client:
        service = LexiangService(redis, client, settings)
        with respx.mock(assert_all_called=True) as router:
            # First token call is 429; fallback credential then returns a valid token.
            router.post(TOKEN_URL).side_effect = [
                httpx.Response(429),
                httpx.Response(200, json={"access_token": "fresh", "expires_in": 7200}),
            ]
            router.post(QA_URL).respond(200, json={"code": 0, "data": {"content": "ok", "session_id": "x" * 40}})
            response = await service.chat(ChatRequest(question="hello", user_id="staff-1"))
        assert response.answer == "ok"
        assert await redis.exists("lx:credential:one:cooldown") == 1


@pytest.mark.asyncio
async def test_expired_token_refreshes_and_continues_existing_session(settings: Settings) -> None:
    redis = FakeRedis(decode_responses=True)
    await redis.setex(
        "lx:conversation:c1", 3600, json.dumps({"session_id": "s" * 40, "user_id": "staff-1", "credential_group": "default"})
    )
    async with httpx.AsyncClient() as client:
        service = LexiangService(redis, client, settings)
        with respx.mock(assert_all_called=True) as router:
            router.post(TOKEN_URL).side_effect = [
                httpx.Response(200, json={"access_token": "old", "expires_in": 7200}),
                httpx.Response(200, json={"access_token": "new", "expires_in": 7200}),
            ]
            qa_route = router.post(QA_URL)
            qa_route.side_effect = [
                httpx.Response(401, json={"code": 41}),
                httpx.Response(200, json={"code": 0, "data": {"content": "continued", "session_id": "s" * 40}}),
            ]
            response = await service.chat(ChatRequest(conversation_id="c1", question="next", user_id="staff-1"))
        assert response.answer == "continued"
        assert json.loads(qa_route.calls[1].request.content)["session_id"] == "s" * 40


def test_openai_request_is_mapped_to_a_stable_lexiang_conversation(settings: Settings) -> None:
    request = OpenAIChatCompletionRequest(
        model="lexiang-ai",
        user="openclaw-channel-42",
        messages=[OpenAIMessage(role="system", content="be helpful"), OpenAIMessage(role="user", content="where is the policy?")],
        qa_mode="reasoning-glm-5.2",
        language="auto",
        skip_faq=True,
        targets=[{"type": "team", "id": "team-42"}],
    )
    mapped = to_lexiang_request(request, settings, "test-key", None)
    repeated = to_lexiang_request(request, settings, "test-key", None)
    assert mapped.question == "where is the policy?"
    assert mapped.conversation_id == repeated.conversation_id
    assert mapped.user_id == "system-bot"
    assert mapped.anonymous_staff_id and len(mapped.anonymous_staff_id) == 32
    assert mapped.qa_mode == "reasoning-glm-5.2"
    assert mapped.language == "auto"
    assert mapped.skip_faq is True
    assert mapped.targets and mapped.targets[0].id == "team-42"
