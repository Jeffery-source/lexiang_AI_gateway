# Lexiang AI Gateway

一个面向内部调用者的腾讯乐享 AI 问答代理服务。调用方只使用本服务的 API Key；服务端负责缓存 `access_token`、多凭证轮换、限流冷却与多轮会话续接，绝不向调用方暴露乐享的 AppSecret 或 access token。

## 启动

需要 Python 3.11+ 与 Redis 7+。

```bash
cp .env.example .env
# 编辑 .env，填入真实 AppKey/AppSecret 和一个随机 GATEWAY_API_KEYS
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
uvicorn app.main:app --reload
```

或者使用 Docker：

```bash
cp .env.example .env
# 编辑 .env
docker compose up --build
```

## 调用

```bash
curl http://localhost:8000/v1/chat \
  -H 'Authorization: Bearer <GATEWAY_API_KEYS中的值>' \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"员工帐号","question":"报销流程是什么？","new_session":true}'
```

响应中的 `conversation_id` 由网关生成；下一轮问题带回它即可：

```json
{"conversation_id":"c_xxx","user_id":"员工帐号","question":"需要哪些材料？"}
```

流式问答使用 `POST /v1/chat/stream`，请求体相同。网关将乐享的 SSE 原样转发，并在响应头 `X-Conversation-Id` 返回网关会话 ID；最后一个 SSE 事件中的 `session_id` 会被保存，下一轮仍带 `conversation_id` 即可。

## OpenAI / OpenClaw 兼容接口

服务也提供 OpenAI Chat Completions 兼容端点，调用方可把 `GATEWAY_API_KEYS` 中的值当作 API Key 使用：

```bash
curl http://localhost:8000/v1/chat/completions \
  -H 'Authorization: Bearer <GATEWAY_API_KEYS中的值>' \
  -H 'Content-Type: application/json' \
  -d '{"model":"lexiang-ai","user":"my-local-user","messages":[{"role":"user","content":"报销流程是什么？"}]}'
```

支持 `GET /v1/models` 和 `stream: true`。流式响应使用 OpenAI SSE 格式，但当前版本在乐享完整回答生成后一次输出答案，而不是逐 token 转发。

OpenClaw 可将它注册为自定义 OpenAI-compatible provider：

```json
{
  "models": {
    "mode": "merge",
    "providers": {
      "lexiang": {
        "baseUrl": "http://localhost:8000/v1",
        "apiKey": "<GATEWAY_API_KEYS中的值>",
        "api": "openai-completions",
        "models": [{
          "id": "lexiang-ai",
          "name": "腾讯乐享知识库 AI",
          "reasoning": false,
          "input": ["text"],
          "contextWindow": 32768,
          "maxTokens": 4096,
          "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
        }]
      }
    }
  }
}
```

OpenAI 标准没有会话 ID。为维持乐享多轮会话，请在请求 `user` 字段传入稳定的调用方/频道 ID；也可使用非标准请求头 `X-Conversation-Id`。服务以调用方 API Key 加该标识生成隔离会话。未提供二者时，每个请求会开启新会话。

该兼容端点还支持以下乐享扩展字段：`qa_mode`（默认 `normal`）、`language`（默认 `zh-CN`）、`skip_faq`（默认 `false`）和 `targets`。例如：

```json
{
  "model": "lexiang-ai",
  "messages": [{"role": "user", "content": "请分析报销流程"}],
  "user": "my-channel",
  "qa_mode": "reasoning-glm-5.2",
  "language": "zh-CN",
  "skip_faq": true,
  "targets": [{"type": "team", "id": "团队ID"}]
}
```

## 凭证配置与行为

`LEXIANG_CREDENTIALS` 的格式为：

```text
id:app_key:app_secret:credential_group,id:app_key:app_secret:credential_group
```

同一 `credential_group` 的凭证必须拥有相同的 AI 权限和知识授权范围。已有会话只会在该组内切换，保证续聊时可访问相同知识范围。

- 每个凭证单独缓存 token，默认提前 5 分钟失效；Redis 锁防止并发刷新风暴。
- token 获取接口返回 429 时，该凭证冷却 10 分钟并自动尝试下一凭证。
- token 获取接口返回 400/401 时，该凭证被禁用，需人工修正配置后清理 Redis 中 `lx:credential:<id>:disabled`。
- 问答接口 401 时，缓存 token 被删除并在可用凭证上重试；会话的 `session_id` 被保留。
- 问答接口 429 是模型或 AI Token 配额耗尽，服务返回 503，不进行凭证池盲目重试。

## 运行测试

```bash
pytest
```

## 生产注意事项

- `user_id` 会作为上游的 `x-staff-id` 发送。应由认证后的身份映射得到，不能信任公网客户端随意指定的值。
- OpenAI 兼容接口使用 `LEXIANG_STAFF_ID` 作为上游身份。保留 `system-bot` 时只能查询公开知识；如需查询私有知识，必须配置拥有对应权限的真实乐享成员帐号。
- 使用 HTTPS、反向代理和你们自己的 API Key/JWT；按调用者增加限流与审计。
- AppSecret 适合从 Vault、Kubernetes Secret 等注入环境变量，不应写入 `.env` 以外的版本控制文件。
