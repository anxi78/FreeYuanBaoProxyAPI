# yb - 元宝 Bot 守护进程

基于 WebSocket 的元宝 Bot 守护进程，提供 OpenAI 兼容的 HTTP API。

## 功能

- 连接元宝 Bot WebSocket 服务
- 监听群消息，自动回复 @元宝 的请求
- 提供 OpenAI 格式的 HTTP API (`/v1/chat/completions`)
- 支持多轮对话

## 使用

```bash
python yb/yb.py
```

## 配置

编辑 `config.json`：

| 字段 | 说明 |
|------|------|
| APP_ID | 元宝 Bot APP_KEY |
| APP_SECRET | 元宝 Bot APP_SECRET |
| GROUP_CODE | 目标群号 |
| YUANBAO_USER_ID | 元宝 AI 的用户 ID |
| YUANBAO_NICK | 元宝 AI 的昵称 |
| PORT | 监听端口 (默认 5000) |
| debug | true 时打印调试信息 |
| API_KEY | API 鉴权密钥。为空时允许所有请求；设置后需在请求头携带 `Authorization: Bearer <API_KEY>` |

## API

```
GET /v1/models
```
返回模型列表（OpenAI 兼容格式）。

```
POST /v1/chat/completions
Content-Type: application/json
Authorization: Bearer <API_KEY>  # 如果配置了 API_KEY 则需要此头
Content-Type: application/json

{
  "messages": [
    {"role": "system", "content": "你是元宝AI"},
    {"role": "user", "content": "你好"}
  ]
}
```

## License

MIT
