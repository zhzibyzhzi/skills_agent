## Skill Agent

**Author:** lfenghx  
**Version:** 0.0.1  
**Type:** Tool

### Overview

Skill Agent is a general-purpose tool plugin built around “Skill progressive disclosure”. It treats the local `skills/` directory as a toolbox: the LLM first decides which skill may help (using metadata), then reads `SKILL.md` on-demand, and only reads files / runs scripts when necessary. Generated artifacts are returned as tool file outputs.

### Key Features

- Progressive disclosure workflow (metadata → `SKILL.md` → files/scripts)
- File delivery: returns files generated in the temp session directory
- Controlled execution: only whitelisted commands are allowed
- Local debugging via `python -m main` + Dify remote install

### Quickstart

1. Install dependencies

```bash
pip install -r requirements.txt
```

2. Configure `.env`

Copy `.env.example` to `.env` and set:

```env
INSTALL_METHOD=remote
REMOTE_INSTALL_URL=debug.dify.ai:5003
REMOTE_INSTALL_KEY=your-debug-key
```

3. Run

```bash
python -m main
```

### Tool Parameters

See [tools/skill_agent.yaml](tools/skill_agent.yaml).

- `query` (required): user task description
- `model` (required): model selector used to drive the agent
- `max_steps` (optional, default 8): max reasoning/tool steps per invocation
- `memory_turns` (optional): how many recent turns to keep in-context

### Skill Folder Convention

By default, skills are loaded from `skills/`:

```
skills/
  JokeMaster/
    SKILL.md
    Reference/
    Scripts/
```

Notes:

- Each skill must include `SKILL.md` (YAML frontmatter supported: `name`, `description`)
- `SKILL.md` should define trigger conditions, step-by-step workflow, required reads, commands to run, and deliverables

### Links & Contact

- Repository: <https://github.com/lfenghx/skill_agent>
- GitHub: lfenghx
- Bilibili: 元视界\_O凌枫o
- Email: 550916599@qq.com
