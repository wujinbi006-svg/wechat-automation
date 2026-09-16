# 验收矩阵

| Feature | Availability | Background | Confirmation | Evidence | Status |
|---|---|---|---|---|---|
| status | true | true | false | `src/service_api.py` | PASS |
| list_chats | true（provider 依赖） | true | false | `src/providers.py` | PASS |
| current_chat | true（UIA provider 依赖） | true | false | `src/providers.py` | PASS |
| search_chat | 未实现 | true | false | — | NOT_AVAILABLE |
| open_chat | 未实现 | 未确认 | false | — | NOT_AVAILABLE |
| type_message | true（已有实验实现） | false | false | `src/minimal_foreground_input.py` | FOREGROUND_REQUIRED |
| send_message | true | false | false | `api.py` | PASS |
| get_messages | false | true | false | `DatabaseDataProvider` | DATABASE_BLOCKED |
| search_messages | false | true | false | `DatabaseDataProvider` | DATABASE_BLOCKED |
