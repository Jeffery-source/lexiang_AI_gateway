# Lexiang AI Gateway

一个面向内部调用者的腾讯乐享 AI 问答代理网关（FastAPI）。调用方只使用本服务自己颁发的 API Key；网关负责缓存 `access_token`、多凭证轮换、限流冷却、多轮会话续接，以及**返回结果中的图片防盗链缓存**，绝不向调用方暴露乐享的 AppSecret 或 access token。

## 功能特性

- **统一网关接入**：调用方只需持有一个网关 API Key，即可调用乐享 AI 问答，无需接触乐享凭证。
- **多凭证池**：多套 AppKey/AppSecret 自动轮换，token 单独缓存、过期提前刷新，429 冷却、400/401 自动禁用。
- **多轮会话续接**：网关将乐享 `session_id` 映射为自己的 `conversation_id`，调用方无需感知乐享会话机制。
- **图片防盗链缓存**：自动提取返回内容中的图片地址，带 Referer 伪装下载到本地，替换为网关自身提供的静态地址，前端不再因防盗链 403 而显示裂图。
- **OpenAI 兼容接口**：支持 `GET /v1/models` 与 `POST /v1/chat/completions`，可直接接入 OpenClaw 等 OpenAI 客户端。
- **原生流式问答**：`POST /v1/chat/stream` 提供 SSE 流式代理。

## 快速启动

需要 Python 3.11+ 与 Redis 7+。

```bash
cp .env.example .env
# 编辑 .env，填入真实 AppKey/AppSecret 和一个随机 GATEWAY_API_KEYS
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
uvicorn app.main:app --reload
```

或者使用 Docker（compose 会一并启动 Redis）：

```bash
cp .env.example .env
# 编辑 .env
docker compose up --build
```

服务默认监听 `http://localhost:8000`。

## 配置说明

所有配置通过环境变量或 `.env` 文件读取：

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis 连接串，用于共享 token、凭证状态与多轮会话 |
| `LEXIANG_CREDENTIALS` | （必填） | 乐享凭证池，格式见下文「凭证配置与行为」 |
| `GATEWAY_API_KEYS` | （必填） | 网关 API Key，建议 32 字节以上随机值；多个用英文逗号分隔 |
| `LEXIANG_STAFF_ID` | `system-bot` | OpenAI 兼容接口调用乐享时使用的成员身份 |
| `OPENAI_COMPAT_MODEL` | `lexiang-ai` | OpenAI 兼容接口暴露的模型名称 |
| `ALLOWED_ORIGINS` | 空 | CORS 白名单，多个用英文逗号分隔；留空不启用 |
| `TOKEN_EXPIRY_SKEW_SECONDS` | `300` | token 距离过期多少秒提前刷新 |
| `CONVERSATION_TTL_SECONDS` | `2592000` | 会话映射在 Redis 中的保存时间（30 天） |
| `CREDENTIAL_COOLDOWN_SECONDS` | `600` | 获取 token 遇 429 后的凭证冷却时间 |
| `UPSTREAM_TIMEOUT_SECONDS` | `90` | 请求乐享上游接口的超时时间 |
| `IMAGE_CACHE_ENABLED` | `true` | 图片缓存总开关；关闭后不下载图片也不挂载 `/images` 路由 |
| `IMAGE_CACHE_ANSWER_ENABLED` | `true` | 是否缓存 `answer` / `reasoning_content` 正文中的图片 |
| `IMAGE_CACHE_ADDITIONAL_ENABLED` | `true` | 是否缓存 `additional_content`（如 `reference_chunks[].content`）中的图片 |
| `IMAGE_CACHE_DIR` | `./cache/images` | 图片本地缓存目录 |
| `IMAGE_BASE_URL` | `http://localhost:8000/images` | 对外暴露的图片服务基础 URL，**生产环境需改为实际可访问域名** |
| `IMAGE_REFERER` | `https://lexiangla.com` | 下载图片时伪装的 Referer 以绕过防盗链，留空则不发送 |

## API 端点

### 健康检查

```bash
curl http://localhost:8000/healthz
# {"status":"ok"}
```

### 原生问答接口

`POST /v1/chat`（非流式）：

```bash
curl http://localhost:8000/v1/chat \
  -H 'Authorization: Bearer <GATEWAY_API_KEYS中的值>' \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"员工帐号","question":"报销流程是什么？","new_session":true}'
```

请求字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `question` | string | 必填，问题内容（≤1024 字） |
| `user_id` | string | 必填，调用方身份标识 |
| `conversation_id` | string | 可选，上一轮响应返回的会话 ID，带回即可续聊 |
| `new_session` | bool | 可选，是否强制开启新会话 |
| `qa_mode` | string | 可选，乐享问答模式，默认 `normal` |
| `language` | string | 可选，`zh-CN` / `en` / `auto`，默认 `zh-CN` |
| `skip_faq` | bool | 可选，是否跳过 FAQ 命中，默认 `false` |
| `targets` | array | 可选，限定检索范围的知识库目标（`space`/`team`/`team_code`/`kb_entry`） |
| `max_chars` | int | 可选，答案最大字符数 |
| `anonymous_staff_id` | string | 可选，匿名身份（16–32 位） |

响应中的 `conversation_id` 由网关生成；下一轮问题带回它即可：

```json
{"conversation_id":"c_xxx","user_id":"员工帐号","question":"需要哪些材料？"}
```

响应结构（`ChatResponse`）：

```json
{
  "conversation_id": "c_xxx",
  "session_id": "乐享会话ID",
  "answer": "回答正文（含 markdown）",
  "answer_source": null,
  "reasoning_content": "思考过程（如启用推理模式）",
  "additional_content": {"reference_chunks": [{"content": "引用片段", "title": "文档名"}]},
  "request_id": null,
  "cached_token": true
}
```

`POST /v1/chat/stream`（SSE 流式）：请求体与 `/v1/chat` 相同，网关代理乐享 SSE 并在响应头 `X-Conversation-Id` 返回网关会话 ID；最后一个 SSE 事件中的 `session_id` 会被保存，下一轮仍带 `conversation_id` 即可。

### OpenAI 兼容接口

服务提供 OpenAI Chat Completions 兼容端点，调用方可把 `GATEWAY_API_KEYS` 中的值当作 API Key 使用：

```bash
curl http://localhost:8000/v1/chat/completions \
  -H 'Authorization: Bearer <GATEWAY_API_KEYS中的值>' \
  -H 'Content-Type: application/json' \
  -d '{"model":"lexiang-ai","user":"my-local-user","messages":[{"role":"user","content":"报销流程是什么？"}]}'
```

支持 `GET /v1/models` 与 `stream: true`。流式响应使用 OpenAI SSE 格式，但当前版本在乐享完整回答生成后一次输出答案，而非逐 token 转发。

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

OpenAI 标准没有会话 ID。为维持乐享多轮会话，请在请求 `user` 字段传入稳定的调用方/频道 ID；也可使用非标准请求头 `X-Conversation-Id`。服务以调用方 API Key 加该标识生成隔离会话；二者都未提供时，每个请求开启新会话。该兼容端点还支持乐享扩展字段：`qa_mode`（默认 `normal`）、`language`（默认 `zh-CN`）、`skip_faq`（默认 `false`）和 `targets`，例如：

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

## 图片防盗链缓存

乐享返回的 `answer`、`reasoning_content`、`additional_content`（包括 `reference_chunks[].content` 等任意嵌套结构）中包含图片地址，这些图片通常带防盗链，前端直接访问会 403。网关在返回前会：

1. **提取**：识别 markdown `![](url)`、HTML `<img src>` 和以图片后缀结尾的裸 URL 三种形式。
2. **下载**：并发下载所有图片，请求头携带 `Referer: <IMAGE_REFERER>` 和浏览器 User-Agent 以绕过防盗链；以 URL 的 SHA-256 前 16 位作为文件名（自动保留扩展名），已缓存的不重复下载。
3. **替换**：将文本中的原始 URL 全部替换为 `IMAGE_BASE_URL/<hash>.<ext>`，例如 `http://localhost:8000/images/977fb8ec61343f8f.png`。
4. **服务**：网关挂载 `/images` 静态路由，直接对外提供本地缓存图片。

### 独立开关

`answer`（含 `reasoning_content`）与 `additional_content` 的图片缓存分别由 `IMAGE_CACHE_ANSWER_ENABLED` 和 `IMAGE_CACHE_ADDITIONAL_ENABLED` 控制，可按需只缓存其中一部分：

```env
IMAGE_CACHE_ANSWER_ENABLED=true        # 缓存 answer 正文图片
IMAGE_CACHE_ADDITIONAL_ENABLED=false   # 不缓存 reference_chunks 中的图片
```

### 流式接口行为

`/v1/chat/stream` 会先缓冲全部 SSE 行，完成图片下载与 URL 替换后再统一转发，因此相比逐行转发牺牲了首字节实时性，但能保证图片 URL 不被截断、防盗链图片正常展示。非流式接口与 OpenAI 兼容接口（内部调用完整 `chat()`）不受影响。

> **注意**：`IMAGE_BASE_URL` 默认是 `http://localhost:8000/images`，生产部署时必须改为前端可访问的域名地址，否则替换后的图片链接无法访问。

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
- 图片缓存目录（默认 `./cache/images`）属运行时数据，应挂载持久化卷并定期清理；`IMAGE_BASE_URL` 需指向对前端可达的域名。
