# Gateway 常驻运行说明

## 启动

在项目目录执行：

```powershell
.\scripts\start_gateway.ps1
```

等价命令：

```powershell
python -m uvicorn api:app --host 127.0.0.1 --port 8010
```

## 运行接口

- `GET /health`：仅检查 Gateway 进程是否能响应。
- `GET /status`：返回微信进程、UIA 连接、监听线程、重连次数、最近成功时间和错误。
- `GET /tools/list`、`POST /tools/call`：保留原有工具协议。
- `WS /ws/events`：接收 `message.new` 与 `connection.changed` 事件。
- `scripts/test_event_stream.py`：WebSocket 参考客户端。
- `scripts/long_run_test.py`：长稳资源与状态采样器。

Gateway 启动后会运行后台 Supervisor。它不阻塞 Uvicorn 请求循环，会定期检查微信进程和 UIA 连接，连接异常时按串行锁执行重连。

消息监听复用当前 UIA 可见消息读取能力。由于微信 UIA 当前不提供稳定消息 ID，事件去重使用 `chat/timestamp/sender/type/content` 指纹，并标记 `dedupe=fingerprint`。无法从 UIA 可靠获取的字段使用 `null`。

本阶段不注册 Windows Service、不修改启动项，也不会自动杀进程或清理微信数据。微信进程退出时 Gateway 只报告 `wechat_process=false` 并等待恢复。

WinSW 配置位于 `service/WeChatGateway.xml`，安装脚本默认只做校验；实际安装必须由管理员明确执行。
