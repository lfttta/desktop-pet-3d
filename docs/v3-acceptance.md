# V3 验收记录

分支：`feat/v3-core`，基线提交 `aea2f33`。

## 已实现

### 聊天

- 流式回复、停止生成、连续对话。
- `chat.send` 的 `accepted` 帧同时返回 `request_id` 和 `conversation_id`，停止后继续发消息仍留在同一会话。
- 历史记录自动滚动到底部。

### 人格与记忆

- 人格目录、系统提示词、边界规则从 `data/persona/godette.json` 读取。
- 记忆候选、人工新增、纠正（旧版本 superseded）、版本链、归档/恢复。
- 列表同时展示 active、archived、superseded 等状态。
- 后台压缩任务、冲突检测与处理、可选向量索引。
- 记忆正文以 Markdown 文件保存，SQLite 保存元数据。

### 表达、状态、主动行为

- 台词库开关、冷却与每日上限、仅删除式润色。
- 表情/语气风格分档写入提示词。
- 情绪状态建议的应用与边界校验。
- 主动消息的预算、静默时段、频率限制（默认关闭）。

### 成长、用量、诊断

- 从 preference/relationship/feedback 记忆和纠正记录收集证据，达到阈值后生成候选。
- 候选审批、回滚、核心特质锁定。
- 用量面板：今日/本月/按模型/按天，预算与价格配置状态。
- 诊断导出 zip：manifest、脱敏设置、诊断信息、日志；不包含 API Key。

### 设置

- 设置 schema v4，新增 memory/expression/autonomy/growth/privacy 分组。
- v1/v2/v3 设置迁移；设置文件损坏时先备份再恢复默认。
- 数据库结构升级前备份，失败时回滚。
- API Key 使用 DPAPI 保存在用户数据目录，不进入仓库、设置文件、日志或诊断包。

## 测试结果

- Python：`315 passed, 2 skipped`。
- Godot headless：
  - `test_app_settings` 8 项
  - `test_chat_framework` 18 项
  - `test_protocol` 7 项
  - `test_service_client` 12 项
  - `test_v3_core` 105 项
  - `test_v3_panels` 87 项
  - 全部 0 失败。
- 真实服务链路：
  - DeepSeek 流式回复正常。
  - 生成中取消后无迟到增量；取消后下一条消息继续同一会话；历史中只有一条完整助手回复。
  - 正常退出时 Godot 与后台 Python 服务均退出，数据目录锁可被下一次启动重新获取。
  - 记忆新增 → 纠正 → 列表 → 详情/版本 → 归档 → 恢复链路通过。
  - 后台压缩任务非阻塞；静默时段内任务可完成。
  - 成长链：会话数与偏好记忆达标后生成候选，审批、历史、回滚、核心特质锁定通过。

## 尚未完成 / 占位

- 角色动作与骨骼：未改动，沿用现有动画系统。
- 向量检索：代码与开关已接入，但当前 DeepSeek 端点不提供 `/v1/embeddings`，需要支持 embedding 的兼容服务；未配置时 `memory.index.*` 返回 `unavailable`。
- 主动行为、成长、压缩默认关闭，需在设置中开启。
- 打包与分发未做。
- 停止按钮的物理点击未做自动化；`ChatSession.cancel()` 的流 ID 绑定、迟到增量丢弃、后续会话延续由 headless 测试覆盖，按钮直接调用该方法。

## 本地运行

```bat
run-dev.cmd
```

使用 `%LOCALAPPDATA%\Pet3D-v3-dev` 作为开发数据目录；`PET3D_PYTHON` 指向本地 Python 环境。
