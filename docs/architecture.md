# Pet3D 整体框架与扩展指南

更新日期：2026-11-03。适用范围：`feat/framework-overall` 分支。聊天与人格本轮只搭框架，不实现功能；3D 动作与阶段二保持一致，不在本文范围内。

## 1. 分层与文件地图

```
app/main.tscn
└─ app/scripts/main.gd                 应用启动、服务/窗口协调、设置保存
   └─ pet_window.gd                    透明置顶窗口、拖动、穿透、托盘、贴边
      ├─ drag_state.gd / hit_regions.gd / screen_math.gd / skin_bounds.gd
      ├─ character_view.gd             角色模型与动作适配（阶段二）
      │  └─ animation/action_controller.gd、expression_controller.gd、mouth_driver.gd
      └─ ui/
         ├─ settings_window.gd         称呼、显示器、大小
         ├─ chat_window.gd             聊天骨架（未接入时控件禁用）
         └─ action_preview.gd          动作/表情预览入口（--review）

app/scripts/service_client.gd          stdio 传输、握手、超时、一次自动重启
app/scripts/protocol_decoder.gd        JSON-lines 增量解码
app/scripts/chat/chat_session.gd       聊天请求边界（未来替换为真实生成）
app/scripts/app_settings.gd            应用侧设置默认值与几何计算

service/main.py                        服务进程入口（--data-dir）
service/runtime.py                     读循环、生命周期、日志
service/commands.py                    命令注册表与 hello 能力握手
service/features/chat.py               聊天命令契约占位
service/features/__init__.py           能力表 FEATURES
service/persona.py                     人格目录与提示词边界
service/settings.py                    设置校验、原子写、schema 迁移
service/database.py                    SQLite 初始化（聊天/记忆空表）
service/locking.py                     数据目录单实例锁
```

数据目录默认 `%LOCALAPPDATA%/Pet3D`：`settings.json`、`pet.db`。应用与后台服务通过 stdin/stdout 的 UTF-8 JSON-lines 通信，stdout 只允许协议帧。

## 2. 启动与请求流

1. `main.gd` 获取单实例锁、配置 Windows 原生桥、启动后台服务。
2. 客户端发送 `hello {protocol_version:1}`；服务返回 `ready`：
   - `settings`、`warnings`
   - `features`：能力表（当前 `chat.available=false`）
   - `personas`：公开人格列表（id + display_name，不含 prompt）
3. 应用恢复窗口位置、创建设置/聊天窗口。
4. 设置修改走 `settings.update`，服务校验后原子写盘再回 `result`。
5. 聊天请求走 `chat.send`，会话层校验后发往服务；服务当前返回 `not_available`，窗口显示“未接入”。

## 3. 当前能力矩阵

| 能力 | 状态 | 入口 |
|---|---|---|
| 透明窗口、拖动、穿透、贴边、托盘、热键 | 已实现 | `pet_window.gd`、`native/` |
| 设置持久化、损坏备份、旧角色迁移 | 已实现 | `service/settings.py` |
| 设置 schema v1 → v2（新增 `persona_id`） | 已实现 | `settings.py`、`app_settings.gd` |
| 人格目录与公开列表 | 框架就绪 | `service/persona.py`、`hello.personas` |
| 聊天命令契约与窗口骨架 | 框架就绪，未实现生成 | `service/features/chat.py`、`chat_session.gd`、`chat_window.gd` |
| 记忆读写 | 未实现 | `memory.*` 保留命令；`pet.db` 空表 |
| TTS | 未实现 | `tts.synthesize` 保留；`tts.cancel` 返回 cancelled |
| 桌面行为/物理 | 未实现 | 阶段三；动作优先级已预留 `autonomous`/`chat` |
| 多角色 | 未实现 | 阶段二明确暂缓 |

## 4. 扩展点

### 4.1 新增服务命令

1. 在 `service/features/<feature>.py` 写 `handler(request, store) -> (event, payload)`，用 `ProtocolError` 表达错误。
2. 在 `service/commands.py` 的 `HANDLERS` 注册；需要取消语义的另注册 `*.cancel`。
3. 在 `service/features/__init__.py` 的 `FEATURES` 增加能力项；`hello` 会自动带出。
4. 若命令属于保留域（当前 `memory`、`tts`），从 `RESERVED_COMMANDS`/保留前缀中移除。
5. 在 `tests/service/test_framework.py` 或 `test_process.py` 加契约测试；实际实现后，把命令从 `not_available` 参数化测试移出。

### 4.2 实现聊天

- 只替换 `service/features/chat.py` 的 `send`：保留现有文本/会话 ID 校验，接入模型客户端。
- 超时、取消、错误码沿用 `ProtocolError`；`chat.cancel` 需要记录在途请求。
- 当前协议是“一请求一回复”。流式输出需要新增事件帧（同一请求 ID 多条消息），并同步修改 `service_client.gd` 的 pending 路由；不要用多条 `result` 冒充流式。
- **API Key 不写入 `settings.json`**：`settings.update` 明确拒绝 `api_key`。密钥应放入独立的密钥存储或系统凭据，具体方案在实现前决定。
- `ChatSession` 的 `not_available` / `invalid_request` / `result` 映射保持不变；实现后窗口无需重写。

### 4.3 实现人格

- 在 `service/persona.py` 增加条目；`display_name` 可公开，`prompt` 不进入 `hello`。
- 用户选择通过 `settings.update {"persona_id": ...}` 持久化，取值受 `ALLOWED_PERSONA_IDS` 校验。
- 提示词组装放在服务端聊天实现里，使用 `resolve(persona_id)['prompt']`；应用只显示名称。
- 若增加人格设置界面，沿用 `settings_window.gd` 的信号/保存路径，不另建配置系统。

### 4.4 新增设置字段

1. 提升 `service/settings.py` 的 `SCHEMA_VERSION`，在 `DEFAULTS` 增加字段。
2. 在 `migrate_document()` 增加上一版本 → 新版本的迁移；不要依赖“缺字段即损坏”。
3. `validate_patch` 增加类型/范围校验；`PATCH_KEYS` 自动包含新字段。
4. 更新 `app/scripts/app_settings.gd` 的默认值。
5. 测试：旧版本文件迁移后旧值不变、新字段默认值正确、非法值拒绝、未来版本备份恢复。

### 4.5 使用数据库

`service/database.py` 已有 `conversations/messages/memories` 空表，但没有读写代码。新增持久化前先加 `user_version` 迁移（当前只接受 0/1），再建 repository；不要把设置复制进 SQLite。

### 4.6 客户端与 UI 可用性

- 新功能先发布能力（`FEATURES`），应用通过 `service_client.capabilities_received` 获取。
- 能力不可用时禁用控件并显示“未接入”，不要返回假数据或空壳成功。
- 传输层只负责帧与超时；请求语义放在 `app/scripts/chat/` 这类会话类中。

## 5. 测试与验收

```bat
.\.venv\Scripts\python.exe -B -m pytest tests/service -q -p no:cacheprovider --basetemp=%TEMP%\pet3d-framework
"D:\danjigame\GODDOT  WORD\Godot_v4.7.2-stable_win64_console.exe" --headless --path app --script res://tests/test_protocol.gd
... test_app_settings.gd / test_windows.gd / test_service_client.gd / test_chat_framework.gd
git diff --check
```

聚焦测试通过不等于整包验收；整包构建见 `docs/stage-2-plan.md` 与 `tools/build_stage2.py`。新增 Godot 测试需同步加入 `build_stage2.py` 的测试清单。

## 6. 分支与合并

- 实现分支从 `main` 开，独立 worktree，命名 `feat/<范围>-<内容>`；不要直接提交 `main`。
- 热点文件：`app/project.godot`、`app/main.tscn`、`app/scripts/main.gd`、`app/export_presets.cfg`、`docs/stage-2-plan.md`。两个分支都要改时，先合并 A，B rebase 后再改。
- 本分支 `feat/framework-overall` 未合并、未推送；集成者按 `AGENTS.md` 第 6 节复核后合并。
