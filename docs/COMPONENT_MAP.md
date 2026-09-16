# Component Map — 组件清单

## 源码

| 文件 | 行数 | 职责 | 平面 |
|---|---|---|---|
| `api.py` | 161 | FastAPI HTTP/WS 入口 | Control |
| `src/service_api.py` | 1130 | WeChatService 服务总线 + 能力清单 + 动作编排 | Control |
| `src/providers.py` | 403 | WeChatDataProvider 抽象 + Mock/Imported/UIA/Database 实现 | Data |
| `src/database_adapter.py` | 679 | 数据库发现 + SQLCipher 解密 + WAL 合并 | Data |
| `src/key_resolver.py` | 398 | 密钥解析 + 验证 + 缓存 + 发现协调器 | Data |
| `src/uia_service.py` | 1349 | UIA 门面 + 前台安全 + ReplicaDriver | Media/Lifecycle |
| `src/gateway_supervisor.py` | 383 | EventHub + 轮询监督器 + 去重 | Event |
| `src/interactive_ipc.py` | 95 | Agent IPC 代理 | Lifecycle |
| `src/minimal_foreground_input.py` | 127 | 前台输入工具 | Media |
| `src/errors.py` | 14 | 异常层级 | Cross |
| `interactive_agent.py` | 290 | Session 1 Agent 主进程 | Lifecycle |
| `openclaw-adapter/index.mjs` | 489 | OpenClaw 插件 | Integration |

## 服务

| 文件 | 用途 |
|---|---|
| `service/WeChatGateway.xml` | WinSW 配置 (id=WeChatGateway, :8010) |
| `service/WeChatGateway.exe` | WinSW 可执行 |
| `service/WinSW.exe` | WinSW 运行时 |

## 脚本

| 文件 | 用途 |
|---|---|
| `scripts/install_service.ps1` | 安装 WinSW 服务 |
| `scripts/uninstall_service.ps1` | 卸载 WinSW 服务 |
| `scripts/start_gateway.ps1` | 手动启动 Gateway |
| `scripts/start_interactive_agent.ps1` | 启动 Interactive Agent |
| `scripts/install_interactive_agent_task.ps1` | 安装 Agent 计划任务 |
| `scripts/service_preflight.ps1` | 服务预检 |
| `scripts/service_status.ps1` | 服务状态 |
| `scripts/interactive_agent_watchdog.py` | Agent 看门狗 |
| `scripts/long_run_test.py` | 长时间运行测试 (SOAK) |
| `scripts/run_live_e2e.py` | 实测 E2E |
| `scripts/run_live_read_messages.py` | 实测消息读取 |
| `scripts/run_live_regression.py` | 回归测试 |
| `scripts/run_background_type_experiment.py` | 后台输入实验 |
| `scripts/scan_send_button.py` | 发送按钮扫描 |
| `scripts/test_event_stream.py` | 事件流测试 |
| `scripts/test_gateway_recovery.py` | 恢复测试 |
| `scripts/uia_navigation_benchmark.py` | UIA 导航基准 |
| `scripts/diagnostics/*.py` | 诊断脚本 (key_chain, key_memory, foreground_monitor, database_key_status) |
| `scripts/diagnostics/*.ps1` | 诊断脚本 (database_inventory, full_tree_scan, observe_foreground, test_chat_input) |

## 测试

| 文件 | 层级 | 用途 |
|---|---|---|
| `tests/test_providers.py` | UNIT | Mock/Imported provider |
| `tests/test_control_plane.py` | INTEGRATION | 能力清单、控制执行、合成事件、状态契约 |
| `tests/test_tool_gateway.py` | INTEGRATION | 工具调用路由 |
| `tests/test_foreground_safety.py` | INTEGRATION | 前台安全策略 |
| `tests/test_database_plane.py` | RUNTIME | 数据库发现与状态 |
| `tests/test_uia_reliability.py` | RUNTIME | UIA 连接可靠性 |
| `tests/manual/test_database_read.py` | LIVE | 数据库读取实测 |
| `tests/manual/test_live_uia.py` | LIVE | UIA 实测 |
| `tests/manual/test_wechat_e2e.py` | LIVE | 端到端实测 |

## 文档

| 文件 | 用途 |
|---|---|
| `docs/ARCHITECTURE.md` | 本架构文档 |
| `docs/RUNTIME_MODEL.md` | 运行时模型 |
| `docs/COMPONENT_MAP.md` | 本组件清单 |
| `docs/CURRENT_STATUS.md` | 当前能力状态 |
| `docs/BASELINE.md` | 突破前基线 |
| `docs/WECHAT_COMPATIBILITY.md` | 版本兼容性 |
| `docs/CAPABILITIES.md` | 能力清单 (历史) |
| `docs/ACCEPTANCE_MATRIX.md` | 验收矩阵 (历史) |
| `docs/GATEWAY_RUNTIME.md` | Gateway 运行时 (历史) |
| `docs/DATABASE_KEY_RUNBOOK.md` | 数据库密钥操作手册 |
| `docs/WECHAT_DATABASE_*.md` | 数据库研究文档 (历史) |
| `docs/WECHAT_KEY_*.md` | 密钥研究文档 (历史) |
| `docs/UIA_BACKGROUND_CAPABILITY.md` | UIA 后台能力 (历史) |
| `docs/WECHATAUTO_REPLICA_RESEARCH.md` | 参考项目研究 |
| `docs/evidence/*.json` | 实测证据文件 |

## 配置

| 文件/环境变量 | 用途 |
|---|---|
| `pytest.ini` | pytest 配置 (testpaths=tests, markers) |
| `.gitignore` | 忽略 work/, logs/, __pycache__/, *.wechat-key 等 |
| `WECHAT_FILES_BASE` | 微信数据目录覆盖 |
| `WECHAT_ACCOUNT_DIR` | 微信账号目录覆盖 |
| `WECHAT_DB_KEY` | 显式数据库密钥 (64 hex) |
| `WECHAT_DB_KEY_FILE` | 密钥文件路径 |
| `WECHAT_DB_KEYS_FILE` | 多库密钥映射文件 |
| `WECHAT_DB_WORKDIR` | 解密工作目录 |
| `WECHAT_KEY_CACHE` | 密钥缓存文件路径 |
| `WECHAT_AGENT_IPC_PORT` | Agent IPC 端口 (默认 18010) |
| `WECHAT_AGENT_IPC_TOKEN` | Agent IPC 令牌 |
| `WECHAT_ENABLE_SEND` | 启用真实发送 |
| `WECHAT_TEST_MODE` | 测试模式 (合成事件注入) |
| `WECHAT_ACTUATOR_MODE` | 执行器模式 (synthetic) |
| `WECHAT_UIA_MODE` | UIA 模式 (interactive/legacy) |
