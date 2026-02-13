## Skill Agent

**Author:** lfenghx  
**Version:** 0.0.4  
**Type:** Tool (Plugin)

### Introduction

Skill Agent is a general-purpose tool plugin based on “Skill Progressive Disclosure”. It treats the local (or mounted) `skills/` directory as a toolbox, so the model can read the skill manual on demand, then read files / run scripts only when necessary, and finally deliver text or files.

### Use Cases

- You want to integrate Skills and constrain/strengthen the model using “manual (SKILL.md) + file structure + scripts”
- You want progress messages and to return generated files as tool outputs
- You want to package capabilities as reusable skill folders (Reference, Scripts, etc.) instead of hard-coding everything in prompts
- **(New)** You want to manage skill codes directly in the local file system instead of uploading zip packages repeatedly.

### Features

- **Local Mount Support**: Directly read the local directory mounted by the Dify container, support hot update of skill codes without re-importing.
- Progressive disclosure: skill index → read `SKILL.md` → read files / run commands as needed
- File delivery: all files in the temp session directory are returned when the agent finishes
- Free execution: the agent can execute commands such as reading/writing files and running scripts
- Controllable memory: configurable memory turns and max step depth

### Tool Parameters

This plugin provides two tools:

- “Skill Manager”: manages the local skills directory (list/delete/download skills).
  > **Note**: ZIP upload function has been removed in this version. Please manage skill folders directly in the mounted directory.
- “agent_skill”: a general agent that can execute skills that have been stored
  ![alt text](_assets/image-1.png)

### How to Use (in Dify)

Step 1: Install this plugin directly from the Marketplace  
Step 2: **Configure Skills Directory**
In Dify's `docker-compose.yaml` or environment variables, configure the `SKILLS_ROOT` environment variable for the plugin service, pointing to your mounted skills directory (e.g., `/app/skills`).
Or manually specify "Skills Root Path" in the plugin tool parameters.

Step 3: Build your workflow as shown below  
![alt text](_assets/image-2.png)  
Step 4: Manage skills  
Create or modify skills directly in your local folder, then use the "List Skills" command of the "Skill Manager" tool in Dify to refresh the list.
Step 5: Chat with Skill Agent  
![alt text](_assets/image-4.png)

Video tutorial: https://www.bilibili.com/video/BV1iszkBCEes

### Skill Standard

- Every skill must include `SKILL.md` (YAML frontmatter supported: `name`, `description`)
- `SKILL.md` can define trigger conditions, workflow, required reference reads, commands to run, and deliverable specs

### Changelog

- 0.0.4:
  1. **Refactor Skill Management**: Removed ZIP upload function, fully switched to local directory mount mode.
  2. Support specifying the skills root directory via environment variable `SKILLS_ROOT` or tool parameters.
- 0.0.3:
  1. Support agent streaming output
  2. Support interactive, multi-turn conversations across turns
  3. Support file memory (no need to re-upload repeatedly)
  4. Support running Node.js scripts as skills
  5. Improve skill_agent runtime stability
- 0.0.2: Support agent file upload and parsing; support automatic dependency installation
- 0.0.1: Implement skill management and a general agent that works with progressive disclosure

### FAQ

1. Installation issues  
   If installation fails with network access available, try switching Dify's pip mirror for better dependency download performance. In intranet environments, install via an offline package (contact the author).

2. File transfer issues  
   If uploading/downloading files fails (e.g., incorrect URL, download timeout), check whether Dify's `.env` has `Files_url` set correctly and whether it matches your Dify address.

3. No output from skill_agent  
   This is usually due to the model. Make sure your model and provider plugin support function calling. The author recommends DeepSeek-V3.1 and reports good test results.

4. Skill invocation issues  
   The more complete your skill is, the more smoothly the agent can invoke it. Ensure your skill materials and scripts are not missing. For Node.js-script skills, install a Node.js runtime in Dify’s `plugin_daemon` container first.
