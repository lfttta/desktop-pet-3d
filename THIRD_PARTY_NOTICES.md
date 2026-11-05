# 第三方来源与本地验证范围

2026-11-03 单角色版本仅装配 Godette；下列其他角色条目保留为历史来源记录，不代表本包包含这些模型。`app/scripts/animation/test_audio.gd` 运行时生成本项目原创的三秒测试音及振幅包络，不使用录音、下载音频或新增音频二进制文件。

- Godot Engine 4.7.2，MIT，https://godotengine.org/license/ 。导出模板使用本机已有安装。
- AzPepoze/godot-vrm v2.6.1：VRM 导入、MToon 与弹簧骨，https://github.com/AzPepoze/godot-vrm/tree/v2.6.1 。保留 addons 下许可证。插件从旧验证工程复制，原生 DLL 保持原版本。三个图标从同标签 Git LFS 媒体地址恢复，并逐一核对原指针内 SHA-256。
- MToon shader：V-Sekai/Godot-MToon-Shader，许可证保留于 `addons/mtoon/LICENSE`。
- Godette：原模型 SirRichard94，VRM 适配 Lyuma（2021），CC BY 3.0。https://github.com/SirRichard94/low-poly-godette 。本工程使用已有 V-Sekai 样例副本，并由共享 FBX 动作生成动画适配缓存。
- Alicia Solid VRM：© DWANGO Co., Ltd.，模型元数据指向 https://3d.nicovideo.jp/alicia/ 与专用规则页面。阶段二仅用于本地技术适配；公开再分发前须单独确认规则。
- 士兵 GLB：用户本地提供的 Blender 导出素材，阶段二构建会烘焙根缩放以修复 Godot 蒙皮显示。当前未提供可核验的公开再分发授权，因此只用于本地技术验证。
- 本地 GLB 与动作：用户提供的 `D:\danjigame\模型all` 目录；逐文件来源、内嵌版权声明与摘要见 `docs/reports/stage-0/assets.md`。FBX 转换产物只用于本轮本地技术验证。
- 原生 Windows 桥接：本项目新增代码；若使用第三方接口头文件，其来源和许可位于 `native/`。

旧原型路径：`D:\danjigame\3D-pet\work-pet`，用于参考和读取已有插件/样例；本轮未修改该工程。旧 Python 桌宠仅作设计参考，阶段 0 未引入其 Qt 呈现或后台服务。

- MerZlin/dsh-pet-indesktop：MIT License，Copyright (c) 2026 Merzlin。本项目在服务端会话、记忆与角色状态的交互设计上参考并移植了该项目的部分思路；原始许可证文本与版权声明保留在本条目中。

MIT License

Copyright (c) 2026 Merzlin

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
