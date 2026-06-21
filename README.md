# FreeYuanBaoProxyAPI - 元宝 Bot 代理守护进程

基于 WebSocket 的元宝 Bot 代理守护进程，提供 OpenAI 兼容的 HTTP API。  
将用户请求转发到微信群聊 @元宝 AI，收到回复后返回，支持工具调用和多轮对话。

## 功能

- 连接元宝 Bot WebSocket 服务，自动鉴权与心跳保活
- 启动时发送测试消息，确认元宝在线后再启动 HTTP 服务器
- 提供 OpenAI 兼容的 HTTP API
  - `GET /v1/models` — 返回模型列表
  - `POST /v1/chat/completions` — 聊天补全，支持多轮对话
- **工具调用** — 支持 OpenAI 格式的 `tools` 参数，自动让元宝以 JSON 格式返回工具调用
- **工具调用历史** — 支持 `messages` 中包含 `role: assistant` 的 `tool_calls` 和 `role: tool` 的消息，保留完整上下文
- API Key 鉴权（可选）
- 无文件日志，仅输出到控制台

## 使用

```bash
python main.py
```

## 配置

编辑同目录下的 `config.json`：

| 字段 | 说明 |
|------|------|
| APP_ID | 元宝 Bot APP_KEY |
| APP_SECRET | 元宝 Bot APP_SECRET |
| GROUP_CODE | 目标群号 |
| YUANBAO_USER_ID | 元宝 AI 的用户 ID |
| YUANBAO_NICK | 元宝 AI 的昵称 |
| PORT | 监听端口（可选，默认 35500） |
| debug | `true` 时打印调试信息（可选） |
| API_KEY | API 鉴权密钥。为空时不校验；设置后需在请求头携带 `Authorization: Bearer <API_KEY>`（可选） |

## API

### 列出模型

```
GET /v1/models
```

### 聊天补全

```
POST /v1/chat/completions
Content-Type: application/json
Authorization: Bearer <API_KEY>    # 如果配置了 API_KEY 则需要此头
```

#### 普通对话

```json
{
  "model": "yuanbao",
  "messages": [
    {"role": "user", "content": "你好"}
  ]
}
```

#### 多轮对话

```json
{
  "model": "yuanbao",
  "messages": [
    {"role": "user", "content": "杭州今天天气怎么样？"},
    {"role": "assistant", "content": "杭州今天晴，20~28°C"},
    {"role": "user", "content": "那明天呢？"}
  ]
}
```

#### 工具调用（让元宝返回工具调用）

```json
{
  "model": "yuanbao",
  "messages": [
    {"role": "user", "content": "杭州今天天气怎么样？"}
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "获取城市天气",
        "parameters": {
          "type": "object",
          "properties": {
            "city": {"type": "string", "description": "城市名"}
          },
          "required": ["city"]
        }
      }
    }
  ],
  "tool_choice": "auto"
}
```

响应中会包含 `finish_reason: "tool_calls"` 和 `message.tool_calls` 字段。

#### 携带外部工具调用历史

```json
{
  "model": "yuanbao",
  "messages": [
    {"role": "user", "content": "根据测试结果输出杭州天气"},
    {
      "role": "assistant",
      "tool_calls": [{
        "id": "call_hangzhou_weather",
        "type": "function",
        "function": {
          "name": "get_weather",
          "arguments": "{\"city\":\"杭州\"}"
        }
      }]
    },
    {
      "role": "tool",
      "tool_call_id": "call_hangzhou_weather",
      "content": "杭州 今日晴，20~28°C"
    }
  ]
}
```

代理会将工具调用历史作为上下文发送给元宝，元宝会根据结果生成回复。

## License

MIT