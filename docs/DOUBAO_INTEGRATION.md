# Doubao Tool Runtime 接入说明

## 地址

本机启动：

```powershell
cd __ROOT__
python -m uvicorn api:app --host 127.0.0.1 --port 8010
```

健康检查：`GET http://127.0.0.1:8010/health`

## 工具发现与调用

先请求 `GET /tools/list`，再调用：

```json
POST /tools/call
{
  "name": "wechat.current_chat",
  "arguments": {},
  "session_id": "doubao-local-001"
}
```

所有调用应复用同一个 `session_id`。返回包含 `success`、`tool`、`data`、`error`、`duration_ms`、`foreground_required`、`focus_changed` 和 `session`。

工具包括：`wechat.status`、`wechat.list_chats`、`wechat.current_chat`、`wechat.search_chat`、`wechat.open_chat`、`wechat.read_messages`、`wechat.type_message`、`wechat.draft_message`、`wechat.confirm_message`、`wechat.send_message`、`wechat.reply_draft`。

`type_message` 可能要求微信前台；读取工具通常无需前台。工具客户端不需要了解 UIA、HWND、PID 或内部驱动。

## 发送边界

`wechat.send_message` 已接入受控发送流程，但白名单仅为 `文件传输助手` / `filehelper`，风险为 high 且需要前台。不得扩大白名单；发送前必须存在已确认草稿和匹配的 `confirmation_id`。本阶段不要主动执行新的真实发送。

数据库读取仍为 `DATABASE_UNAVAILABLE`。常见错误码包括 `TOOL_NOT_FOUND`、`INVALID_ARGUMENT`、`PROVIDER_UNAVAILABLE`、`TOOL_ERROR`、`RECIPIENT_NOT_ALLOWED`、`SEND_CONFIRMATION_REQUIRED`、`SEND_CONFIRMATION_MISMATCH`、`SEND_ALREADY_CONSUMED`。
