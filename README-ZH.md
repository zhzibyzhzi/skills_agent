## Skill Agent

**作者：** lfenghx  
**版本：** 0.0.1  
**类型：** Tool（工具插件）

### 简介

Skill Agent 是一个基于 “Skill 渐进式披露（Progressive Disclosure）” 设计的通用型工具插件：它把本地 `skills/` 目录当作“工具箱”，让大模型在需要时逐步读取技能说明、再按需读取文件/执行脚本，最终生成文本或文件交付。

### 适用场景

- 你希望用“说明书（SKILL.md）+ 文件结构 + 脚本”来约束/增强大模型执行能力
- 你希望输出带有进度提示，并把生成的文件作为工具输出返回
- 你希望把技能封装成可复用的目录（Reference、Scripts 等），而不是把所有逻辑写死在提示词里

### 功能特性

- 渐进式披露：先用技能索引判断，再读取 SKILL.md，再按需读文件/执行命令
- 文件交付：工具结束时会把本次 temp 会话目录中的文件作为文件输出返回
- 受控执行：仅允许执行白名单命令（例如 `python`、`node` 等），避免任意命令执行风险
- 调试友好：提供本地调试入口 `python -m main`，可通过 Dify remote install 调试

### 使用方式（在 Dify 中）

1. 安装依赖

```bash
pip install -r requirements.txt
```

2. 配置调试环境

复制 `.env.example` 为 `.env`，填入你的调试信息：

```env
INSTALL_METHOD=remote
REMOTE_INSTALL_URL=debug.dify.ai:5003
REMOTE_INSTALL_KEY=your-debug-key
```

3. 启动插件

```bash
python -m main
```

4. 在 Dify 中使用

在 Dify 工作流/应用编排中选择该 Tool，并传入参数（见下文）。

### 工具参数

参数定义见 [tools/skill_agent.yaml](tools/skill_agent.yaml)。

- `query`（必填）：用户输入/任务描述
- `model`（必填）：用于驱动 Agent 的大模型选择器
- `max_steps`（可选，默认 8）：单次调用内最多执行多少轮 “思考/调用技能”
- `memory_turns`（可选，默认 6 或工具默认值）：单次调用内保留的最近上下文轮数

### Skill 目录规范

默认从项目根目录下的 `skills/` 加载技能。每个技能为一个文件夹：

```
skills/
  笑话大王/
    SKILL.md
    Reference/
      冷笑话参考.md
    Scripts/
      generate_cold_jokes.py
```

关键约定：

- 每个 skill 必须包含 `SKILL.md`（支持 YAML Frontmatter：`name`、`description`）
- `SKILL.md` 里可以定义触发条件、流程、需要读取的参考文件、需要执行的脚本命令、交付物规范等

### 本地开发与调试

- 入口：`python -m main`
- 插件清单：见 [manifest.yaml](manifest.yaml)
- 隐私政策：见 [PRIVACY.md](PRIVACY.md)

### 作者与联系

- GitHub：lfenghx（仓库：<https://github.com/lfenghx/skill_agent>）
- B 站：元视界\_O凌枫o
- 邮箱：550916599@qq.com
