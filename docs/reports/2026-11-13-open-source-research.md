# 开源方案调研（2026-11-13）

> **2026-11-15 状态注记（带入 `integrate/local-work` 时补，正文原样未改）**：本文件成文后，
> 建议清单两条已被远端主线（origin/main）独立兑现——**TTS 走 Windows SAPI**：
> `43e6f31` 落地 `service/tts.py`（System.Speech 合成，schema v6 加 voice 组），
> `fc095c4` 语音默认关闭；**3-B 完成**：`d1a5973` 把后续表 3-B 标为已完成（气泡 + 回复条）。
> 仍然有效：**3-C 隐私红线决策**（§1.3，维持现状不做）、**VRMA 立项建议**（§2.1，需单独立项）、
> **§2 许可证分析**（NOASSERTION/GPL 条目引入前仍须先打开 LICENSE 确认）。

状态：**调研结论，未产生代码改动**。本文件回答「阶段三剩余缺口有无开源方案可参考」，
并与 `docs/stage-3-plan.md` §6 的 3-B ~ 3-F 编号对齐。

## 0. 方法与本文件的信用边界

写作时分两类证据，请勿混读：

- **已核实**：本文件作者在本仓库实际读过源码并记录了 `file:line`。这部分可复核，
  复核方式见各条附注。
- **未核实**：外部仓库的 star 数、最近推送时间、许可证。这些数字由一次调研代理
  通过 GitHub REST API 抓取，但**抓取回执未被会话记录**，本文件无法复现核验，
  只能当作量级参考。**任何一条都不足以作为引入依赖或派生代码的依据。**

外部仓库的「可复用什么」一列**全部是推断**，调研代理未读到任何源码或 README
（尝试抓取 `raw.githubusercontent.com` 时被连接重置）。涉及许可证正文的方案，
标记为 `NOASSERTION` 的，在派生代码前必须先打开 LICENSE 确认。

## 1. 本地现状核实（可复核）

### 1.1 TTS 是预留位，不是故障

`service/commands.py:17` 定义 `RESERVED_COMMANDS = frozenset({'tts.synthesize'})`，
`:18-21` 的 `RESERVED_PREFIXES` 另含 `'tts'`、`'voice'`。分发逻辑在 `:106-109`
判定保留、`:118` 落到 `_not_available`，即 `:82-83`：

```python
raise ProtocolError('not_available', 'This feature is reserved for a later stage.', ...)
```

**结论**：`tts.synthesize` 从未接入过引擎，`not_available` 是设计好的占位返回。
验收清单 #15 记录的「本轮之前即为 not_available」指的就是这个机制。
阶段三 3-D 是「从零开始」，不是「修一个坏掉的功能」。

### 1.2 分档降载已完成，不应再投入（3-F）

`app/scripts/state/fps_governor.gd:12-17` 定义六档
`60 / 30 / 15 / 10 / 5 / 5`，`:19` `LONG_IDLE_SECONDS := 120.0`，
`:20` `DOWNGRADE_HYSTERESIS := 0.8`，`:22` `OCCLUDED_STATES`。

需要补充的是：这套治理**是本轮 `82389e4` 才落地的**（属 `fix/review-hardening`
分支），不是历史遗留。`target_fps()` 与 `update()` 均为纯逻辑，不依赖显示服务器。

**结论**：3-F 在 `stage-3-plan.md` §6 中标为「打磨项」，现已不是缺口。

### 1.3 Agent 状态联动（3-C）与既有隐私红线冲突

`service/desktop/monitor.py:1-6` 的模块声明写明只采集四项信号（前台窗口矩形、
窗口类名、shell 通知态、最后输入 tick），并明确

> Window titles, process names, screen contents, and keyboard or mouse content
> are never read, returned, or logged.

`tests/service/test_desktop_monitor.py:134-138` 更进一步，是一条**源码级断言**：

```python
def test_module_never_reaches_for_window_text_or_process_identifiers():
    source = inspect.getsource(monitor_module)
    for token in ('GetWindowText', 'GetWindowThreadProcessId',
                  'QueryFullProcessImageName', 'GetProcessImageFileName', 'OpenProcess'):
        assert token not in source
```

**结论**：要实现「读取本机其他 AI 编码工具的状态」，必须先推翻一条已经固化成
测试的隐私约束。这不是难度问题，是产品价值判断。

> **本轮决定（2026-11-13，已确认）**：维持现状，3-C 不做。隐私声明与上述测试
> 均保持原样，不修改。

### 1.4 聊天气泡（3-B）的窗口条件已就位

`app/scripts/ui/chat_bubble.gd:64-70` 已设置 `borderless`、`always_on_top`、
`unfocusable`、`transparent`、`transparent_bg`、`unresizable`、`mouse_passthrough`，
注释说明其走 `SW_SHOWNOACTIVATE`，不抢焦点。本轮 `9a21524` 已把它接到
`main.gd` 的 `chat.delta` / `chat.done` / `autonomy.bubble` 分发与每帧跟随。

**结论**：3-B 的气泡外壳已完成，剩余是头顶快捷输入这一交互层。

### 1.5 视觉已对齐 OpenAI 兼容格式（3-E）

`service/vision.py:386` 使用 `{'type': 'image_url', 'image_url': {'url': image_data_url(png_bytes)}}`，
`:381` 注释写明是 OpenAI-compatible chat request。本轮 `4b023ab` 已接入命令路由
与主动截屏心跳（心跳钩子在 `dcd9b3d` 修正为注册 `tick`）。

**结论**：任何 OpenAI 兼容的视觉端点都可直接接入，无需改协议层。

### 1.6 记忆层已具备外部方案的核心语义

| 能力 | 位置 |
|---|---|
| FTS5 中文分词检索 | `service/memory/store.py:533`（`tokenize='unicode61'`） |
| Markdown 正文落盘 | `service/memory/store.py:64`（`markdown_document()` 定义；`:428` 为 `write_body()` 调用点） |
| 版本链（新记忆取代旧记忆） | `service/memory/store.py:242`（`supersedes_id` 链遍历） |
| 向量余弦相似度 | `service/memory/embeddings.py:74`（纯 Python 实现） |

**结论**：对外部记忆框架的结论是**借鉴语义、不引入依赖**。引入图数据库或
Python 依赖会破坏「后台服务仅用标准库」这一约束。

### 1.7 VRM 运行时来源

`app/addons/vrm/plugin.cfg` 记录为
`author="AzPepoze (Forked from V-Sekai)"`、`version="2.6.1"`。

**注意**：这不是直接引入上游 `V-Sekai/godot-vrm`，而是第三方 fork。升级或
派生代码前需先确认该 fork 的 `LICENSE`（仓库内存在 `app/addons/vrm/LICENSE`）。

## 2. 外部方案（元数据未核实，仅作量级参考）

| 方案 | ★ | 最近推送 | License | 判断 |
|---|---|---|---|---|
| `mem0ai/mem0` | 68.9k | 2026-11-06 | Apache-2.0 | 只借 ADD/UPDATE/DELETE 操作语义；本地已有等价能力（§1.6） |
| `getzep/graphiti` | 32.8k | 2026-11-11 | Apache-2.0 | 双时态建模可借，但需图数据库，与标准库约束冲突 |
| `letta-ai/letta` | 25.4k | 2026-10-18 | Apache-2.0 | core/archival 分层可借思路 |
| `basicmachines-co/basic-memory` | 4.6k | 2026-11-13 | **AGPL-3.0** | 架构与本项目同构（Markdown 为真相源 + SQLite 索引），但许可有传染性 |
| `OHF-Voice/piper1-gpl` | 6.3k | 2026-11-13 | **GPL-3.0** | 闭源分发前必须评估许可 |
| `hexgrad/kokoro` | 10.1k | 2025-08-06 | Apache-2.0 | 需 torch，与标准库约束冲突；「停更」属推断未核实 |
| `LorisYounger/VPet` | 7.2k | 2026-11-07 | Apache-2.0 | 桌宠状态机与插件接口可借思路（C#/WPF） |
| `malaybaku/VMagicMirror` | 549 | 2026-11-10 | MIT | Windows 桌面 VRM 挂件，C#/Unity，可借思路 |
| `Open-LLM-VTuber` | 14.7k | 2026-05-15 | **NOASSERTION** | 打断式语音交互可借思路；许可需先确认 |
| `screenpipe/screenpipe` | 22.4k | 2026-11-13 | **NOASSERTION** | 全程录屏，与本项目隐私姿态相悖 |
| `Python-UIAutomation-for-Windows` | 3.6k | 2026-06-02 | Apache-2.0 | 若将来做 3-C，这是最实用的读窗口方案 |

三个 `NOASSERTION`（`V-Sekai/godot-vrm`、`Open-LLM-VTuber`、`screenpipe`）的
LICENSE 正文均未打开确认。

### 2.1 唯一读过官方文档的一条：VRM Animation（`.vrma`）

[vrm.dev/en/vrma](https://vrm.dev/en/vrma/) 说明 VRM Animation 由 glTF animation
与 `VRMC_vrm_animation` 扩展构成，覆盖 humanoid 骨骼、expression 权重、gaze 节点；
同一份 `.vrma` 可套用到任意 VRM 模型。

对照本项目现状：动作按角色烘焙进 AnimationLibrary，`humanoid_core.json` 只有
mode 没有长度，长度需从烘焙库合并（见 `app/scripts/pet_window.gd` 的
`_behavior_action_table()`）。

**判断**：改为 `.vrma` 可让换角色不必重做动画，并能接入社区动作。这是本轮调研中
**最值得立项**的一条，但属于大改，不应顺手实施。

## 3. 建议清单（按投入产出比）

1. **头顶快捷输入（3-B 收尾）** — 气泡外壳与 HWND 条件已就位（§1.4），只差交互层。
2. **TTS 走 Windows SAPI（ctypes，零依赖）** — 无 GPL 传染，与既有
   `ctypes.WinDLL` 用法一致。**选型前必须实机验证中文语音质量**，这一点本轮未验。
3. **记忆层不引依赖** — 已有核心能力（§1.6），只借鉴语义。
4. **分档降载停止投入** — 已完成（§1.2）。
5. **Agent 状态联动暂缓** — 隐私红线冲突（§1.3），本轮已决定不做。
6. **VRMA 动作库迁移** — 高杠杆，需单独立项评估。

关于 TTS 选型需要纠正一点：调研代理给出的「SAPI 优于 piper 是因为风格一致性」
理由过弱。真实理由是 **GPL-3.0 传染风险 + 零依赖**；而 SAPI 的中文语音可用性、
经 Python ctypes 调用的可行性**均未验证**，是选型前必须先做的实测。

## 4. 本轮不做的事

- **不修改** `service/desktop/monitor.py` 的隐私声明
- **不修改** `tests/service/test_desktop_monitor.py` 的禁词断言
- **不引入**任何新的 Python 第三方依赖到后台服务
- **不引入** GPL / AGPL 代码
- **不迁移**到 VRMA（需立项）

## 5. 事实与推断的边界

| 内容 | 状态 |
|---|---|
| §1 全部 `file:line` 引用 | 已核实（作者直接读取源码） |
| §2 表格的 star / 推送时间 / 许可证 | **未核实**（抓取回执未记录，无法复现） |
| §2 「可复用什么」的细则 | **推断**（未读任何外部源码或 README） |
| VRMA 规范描述 | 已读官方文档，但当时抓取回执同样未记录 |
| SAPI 中文可用性 | **未验证** |
| `kokoro` 是否停更 | 未核实（`pushed_at` 是事实，「停更」是推断） |
