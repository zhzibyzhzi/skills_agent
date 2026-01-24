import json
import os
import shutil
import subprocess
import sys
import time
import uuid
import base64
import hashlib
from collections.abc import Generator
import importlib.util
import re
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.model.message import (
    AssistantPromptMessage,
    PromptMessageTool,
    SystemPromptMessage,
    ToolPromptMessage,
    UserPromptMessage,
)
from dify_plugin.entities.tool import ToolInvokeMessage


def _safe_get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    try:
        return obj[key]  # type: ignore[index]
    except Exception:
        pass
    try:
        return getattr(obj, key)
    except Exception:
        return None


def _safe_join(root: str, relative_path: str) -> str:
    root_abs = os.path.abspath(root)
    joined = os.path.abspath(os.path.join(root_abs, relative_path))
    if os.path.commonpath([root_abs, joined]) != root_abs:
        raise ValueError("path is outside root")
    return joined


def _read_text(path: str, max_chars: int = 12000) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read(max_chars)


def _list_dir(root: str, max_depth: int = 2) -> list[dict[str, Any]]:
    root_abs = os.path.abspath(root)
    entries: list[dict[str, Any]] = []
    root_depth = root_abs.count(os.sep)
    for current_root, dirs, files in os.walk(root_abs):
        depth = current_root.count(os.sep) - root_depth
        if depth > max_depth:
            dirs[:] = []
            continue
        for name in sorted(dirs):
            entries.append(
                {
                    "type": "dir",
                    "path": os.path.join(current_root, name),
                    "relative_path": os.path.relpath(os.path.join(current_root, name), root_abs),
                }
            )
        for name in sorted(files):
            entries.append(
                {
                    "type": "file",
                    "path": os.path.join(current_root, name),
                    "relative_path": os.path.relpath(os.path.join(current_root, name), root_abs),
                }
            )
    return entries


def _parse_frontmatter(content: str) -> dict[str, str]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    data: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            data[key] = value
    return data


def _extract_first_json_object(text: str) -> str | None:
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if len(lines) >= 3 and lines[0].startswith("```"):
            s = "\n".join(lines[1:-1]).strip()
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


def _detect_skills_root(explicit_path: str | None) -> str | None:
    if explicit_path and os.path.isdir(explicit_path):
        return os.path.abspath(explicit_path)

    env_path = os.getenv("SKILLS_ROOT")
    if env_path and os.path.isdir(env_path):
        return os.path.abspath(env_path)

    plugin_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    candidates = [
        os.path.join(plugin_root, "skills")
    ]
    for p in candidates:
        if os.path.isdir(p):
            return os.path.abspath(p)
    return None


ALLOWED_COMMANDS = {"python", "node", "pandoc", "soffice", "pdftoppm"}
TEMP_SESSION_PREFIX = "dify-skill-"


def _cleanup_old_temp_sessions(temp_root: str, *, keep: int, protect_dirs: set[str] | None = None) -> None:
    protect = {os.path.abspath(p) for p in (protect_dirs or set()) if p}
    try:
        entries: list[tuple[float, str]] = []
        for name in os.listdir(temp_root):
            if not isinstance(name, str) or not name.startswith(TEMP_SESSION_PREFIX):
                continue
            path = os.path.join(temp_root, name)
            if not os.path.isdir(path):
                continue
            abs_path = os.path.abspath(path)
            if abs_path in protect:
                continue
            try:
                mtime = os.path.getmtime(abs_path)
            except Exception:
                mtime = 0.0
            entries.append((mtime, abs_path))
        entries.sort(key=lambda x: x[0])
        if keep < 0:
            keep = 0
        excess = len(entries) - keep
        if excess <= 0:
            return
        for _, path in entries[:excess]:
            try:
                _dbg(f"cleanup_temp_session dir={path}")
                for _ in range(2):
                    try:
                        shutil.rmtree(path, ignore_errors=False)
                        break
                    except Exception:
                        time.sleep(0.1)
                else:
                    shutil.rmtree(path, ignore_errors=True)
            except Exception:
                continue
    except Exception:
        return


def _is_safe_module_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+", name or ""))


def _skill_contains_python_module(skill_path: str, module_name: str) -> bool:
    base = (module_name or "").split(".", 1)[0].strip()
    if not base:
        return False
    if not _is_safe_module_name(base):
        return False
    file_candidate = os.path.join(skill_path, base + ".py")
    if os.path.isfile(file_candidate):
        return True
    dir_candidate = os.path.join(skill_path, base)
    if not os.path.isdir(dir_candidate):
        return False
    init_candidate = os.path.join(dir_candidate, "__init__.py")
    if os.path.isfile(init_candidate):
        return True
    for _, _, files in os.walk(dir_candidate):
        if any(str(f).lower().endswith(".py") for f in files):
            return True
    return False


def _ensure_python_module(module_name: str, *, auto_install: bool, cwd: str) -> dict[str, Any]:
    if not module_name or not _is_safe_module_name(module_name):
        return {"ok": False, "error": "invalid module name", "module": module_name}
    if importlib.util.find_spec(module_name) is not None:
        return {"ok": True, "module": module_name}
    if not auto_install:
        return {"ok": False, "error": "python module not found", "module": module_name}

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", module_name, "--no-input", "--disable-pip-version-check"],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        if result.returncode == 0:
            return {"ok": True, "module": module_name, "installed": True}
        return {
            "ok": False,
            "error": "pip install failed",
            "module": module_name,
            "returncode": result.returncode,
            "stdout": (result.stdout or "").strip(),
            "stderr": (result.stderr or "").strip(),
        }
    except Exception as e:
        return {"ok": False, "error": "pip install exception", "module": module_name, "exception": str(e)}

SUMMARY_INPUT_MAX_CHARS = 12000
SUMMARY_KEY_PREFIX = "skill:summary:"
RESUME_KEY_PREFIX = "skill:resume:"


def _get_resume_storage_key(session: Any) -> str:
    candidates = [
        _safe_get(session, "conversation_id"),
        _safe_get(session, "chat_id"),
        _safe_get(session, "task_id"),
        _safe_get(session, "id"),
        _safe_get(session, "session_id"),
        _safe_get(session, "app_run_id"),
    ]
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return RESUME_KEY_PREFIX + c.strip()
    return RESUME_KEY_PREFIX + "global"


def _storage_get_json(storage: Any, key: str) -> dict[str, Any]:
    raw = _storage_get_text(storage, key).strip()
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except Exception:
        return {}


def _storage_set_json(storage: Any, key: str, value: dict[str, Any] | None) -> None:
    if not value:
        _storage_set_text(storage, key, "")
        return
    try:
        _storage_set_text(storage, key, json.dumps(value, ensure_ascii=False))
    except Exception:
        _storage_set_text(storage, key, "")
        return


def _normalize_small_reply(text: str) -> str:
    if not isinstance(text, str):
        return ""
    t = text.strip().lower()
    t = re.sub(r"\s+", "", t)
    t = re.sub(r"[。．\.，,！!？\?；;：:\-—_~`'\"]+", "", t)
    return t


def _is_allow_reply(text: str) -> bool:
    t = _normalize_small_reply(text)
    if not t:
        return False
    if any(x in t for x in ("不允许", "不同意", "不可以", "不要", "拒绝", "取消")):
        return False
    if t in {"允许", "同意", "可以", "好的", "好", "ok", "okay", "yes", "y", "sure"}:
        return True
    if "允许" in t or "同意" in t:
        return True
    return False


def _is_deny_reply(text: str) -> bool:
    t = _normalize_small_reply(text)
    if not t:
        return False
    return any(x in t for x in ("不允许", "不同意", "不可以", "不要", "拒绝", "取消"))

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_skill_metadata",
            "description": "读取指定技能包的SKILL.md与元数据",
            "parameters": {
                "type": "object",
                "properties": {"skill_name": {"type": "string"}},
                "required": ["skill_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_skill_files",
            "description": "列出指定技能包内的文件结构",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string"},
                    "max_depth": {"type": "integer", "default": 2},
                },
                "required": ["skill_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_skill_file",
            "description": "读取技能包内的文件内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string"},
                    "relative_path": {"type": "string"},
                    "max_chars": {"type": "integer", "default": 12000},
                },
                "required": ["skill_name", "relative_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_skill_command",
            "description": "在技能包目录内执行命令（限定可执行程序）",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string"},
                    "command": {"type": "array", "items": {"type": "string"}},
                    "cwd_relative": {"type": "string"},
                    "auto_install": {"type": "boolean", "default": False},
                },
                "required": ["skill_name", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_session_context",
            "description": "获取本次会话的技能目录与临时目录信息",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_temp_file",
            "description": "将文本写入 temp 会话目录（相对路径）",
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["relative_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_temp_file",
            "description": "读取 temp 会话目录文件内容（相对路径）",
            "parameters": {
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string"},
                    "max_chars": {"type": "integer", "default": 12000},
                },
                "required": ["relative_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_temp_files",
            "description": "列出 temp 会话目录文件结构",
            "parameters": {
                "type": "object",
                "properties": {"max_depth": {"type": "integer", "default": 4}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_temp_command",
            "description": "在 temp 会话目录内执行命令（限定可执行程序）",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "array", "items": {"type": "string"}},
                    "cwd_relative": {"type": "string"},
                    "auto_install": {"type": "boolean", "default": False},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_temp_file",
            "description": "标记 temp 会话文件为最终交付文件（不复制）",
            "parameters": {
                "type": "object",
                "properties": {
                    "temp_relative_path": {"type": "string"},
                    "workspace_relative_path": {"type": "string"},
                    "overwrite": {"type": "boolean", "default": False},
                },
                "required": ["temp_relative_path", "workspace_relative_path"],
            },
        },
    },
]

_PROMPT_MESSAGE_TOOLS: list[PromptMessageTool] | None = None


def _build_prompt_message_tools() -> list[PromptMessageTool]:
    global _PROMPT_MESSAGE_TOOLS
    if _PROMPT_MESSAGE_TOOLS is not None:
        return _PROMPT_MESSAGE_TOOLS

    tools: list[PromptMessageTool] = []
    for schema in TOOL_SCHEMAS:
        if not isinstance(schema, dict):
            continue
        function_info = schema.get("function")
        if not isinstance(function_info, dict):
            continue
        name = function_info.get("name")
        description = function_info.get("description")
        parameters = function_info.get("parameters")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(description, str):
            description = ""
        if not isinstance(parameters, dict):
            parameters = {}
        if "type" not in parameters:
            parameters["type"] = "object"
        if "properties" not in parameters or not isinstance(parameters.get("properties"), dict):
            parameters["properties"] = {}
        if "required" not in parameters or not isinstance(parameters.get("required"), list):
            parameters["required"] = []
        tools.append(PromptMessageTool(name=name.strip(), description=description, parameters=parameters))

    _PROMPT_MESSAGE_TOOLS = tools
    return tools


def _extract_tool_calls(response: Any) -> list[Any]:
    message = _safe_get(response, "message") or response
    tool_calls = _safe_get(message, "tool_calls") or []
    if isinstance(tool_calls, list):
        return tool_calls
    return []


def _parse_tool_call(tool_call: Any) -> tuple[str | None, str | None, dict[str, Any]]:
    call_id = _safe_get(tool_call, "id")
    function_info = _safe_get(tool_call, "function") or {}
    name = _safe_get(function_info, "name")
    raw_args = _safe_get(function_info, "arguments") or "{}"
    if isinstance(raw_args, dict):
        return call_id, name, raw_args
    if not isinstance(raw_args, str):
        return call_id, name, {}
    try:
        parsed = json.loads(raw_args)
        return call_id, name, parsed if isinstance(parsed, dict) else {}
    except Exception:
        return call_id, name, {}


class _AgentRuntime:

    def __init__(
        self,
        *,
        skills_root: str | None,
        session_dir: str,
        max_steps: int,
        memory_turns: int,
    ) -> None:
        self.skills_root = skills_root
        self.session_dir = session_dir
        self.max_steps = max_steps
        self.memory_turns = memory_turns
        self._skill_metadata_cache: dict[str, dict[str, Any]] = {}

    def load_skills_index(self) -> dict[str, Any]:
        if not self.skills_root:
            return {"root": None, "skills": []}
        skills: list[dict[str, Any]] = []
        for folder in sorted(os.listdir(self.skills_root)):
            path = os.path.join(self.skills_root, folder)
            if not os.path.isdir(path):
                continue
            skill_md = os.path.join(path, "SKILL.md")
            meta: dict[str, str] = {}
            if os.path.isfile(skill_md):
                meta = _parse_frontmatter(_read_text(skill_md, 4000))
            skills.append(
                {
                    "name": meta.get("name") or folder,
                    "folder": folder,
                    "description": meta.get("description") or "",
                }
            )
        return {"root": self.skills_root, "skills": skills}

    def get_skill_metadata(self, skill_name: str) -> dict[str, Any]:
        if not self.skills_root:
            return {"error": "skills_root not found"}
        cached = self._skill_metadata_cache.get(skill_name)
        if isinstance(cached, dict) and cached.get("skill") == skill_name:
            return {
                "skill": skill_name,
                "metadata": cached.get("metadata") or {},
                "cached": True,
                "skill_md_path": cached.get("skill_md_path") or "",
                "note": "skill_md 已在本轮缓存到 temp，为节省 token 此处不重复输出；如需原文请 read_temp_file(skill_md_path)。",
            }
        path = _safe_join(self.skills_root, skill_name)
        skill_md = os.path.join(path, "SKILL.md")
        if not os.path.isfile(skill_md):
            return {"error": "SKILL.md not found", "skill": skill_name}
        content = _read_text(skill_md, 12000)
        meta = _parse_frontmatter(content)
        safe_folder = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", (skill_name or "").strip())
        if not safe_folder:
            safe_folder = "skill"
        safe_folder = safe_folder[:60]
        skill_md_path = f"_skill_cache/{safe_folder}/SKILL.md"
        try:
            self.write_temp_file(skill_md_path, content)
        except Exception:
            skill_md_path = ""
        result = {"skill": skill_name, "metadata": meta, "skill_md": content, "skill_md_path": skill_md_path}
        self._skill_metadata_cache[skill_name] = {"skill": skill_name, "metadata": meta, "skill_md_path": skill_md_path}
        return result

    def list_skill_files(self, skill_name: str, max_depth: int = 2) -> dict[str, Any]:
        if not self.skills_root:
            return {"error": "skills_root not found"}
        skill_path = _safe_join(self.skills_root, skill_name)
        return {"skill": skill_name, "entries": _list_dir(skill_path, max_depth=max_depth)}

    def read_skill_file(self, skill_name: str, relative_path: str, max_chars: int = 12000) -> dict[str, Any]:
        if not self.skills_root:
            return {"error": "skills_root not found"}
        skill_path = _safe_join(self.skills_root, skill_name)
        file_path = _safe_join(skill_path, relative_path)
        if not os.path.isfile(file_path):
            return {"error": "file not found", "path": relative_path}
        return {"path": file_path, "content": _read_text(file_path, max_chars)}

    def write_temp_file(self, relative_path: str, content: str) -> dict[str, Any]:
        os.makedirs(self.session_dir, exist_ok=True)
        path = _safe_join(self.session_dir, relative_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content or "")
        return {"path": path, "bytes": len((content or "").encode("utf-8"))}

    def read_temp_file(self, relative_path: str, max_chars: int = 12000) -> dict[str, Any]:
        os.makedirs(self.session_dir, exist_ok=True)
        path = _safe_join(self.session_dir, relative_path)
        if not os.path.isfile(path):
            return {"error": "file not found", "relative_path": relative_path}
        return {"path": path, "content": _read_text(path, max_chars)}

    def list_temp_files(self, max_depth: int = 4) -> dict[str, Any]:
        os.makedirs(self.session_dir, exist_ok=True)
        return {"session_dir": self.session_dir, "entries": _list_dir(self.session_dir, max_depth=max_depth)}

    def get_session_context(self) -> dict[str, Any]:
        return {
            "skills_root": self.skills_root,
            "session_dir": self.session_dir,
        }

    def run_skill_command(
        self,
        *,
        skill_name: str,
        command: list[str],
        cwd_relative: str | None = None,
        auto_install: bool = False,
    ) -> dict[str, Any]:
        if not self.skills_root:
            return {"error": "skills_root not found"}
        if not command:
            return {"error": "command must be a non-empty list"}
        skill_path = _safe_join(self.skills_root, skill_name)
        exe = command[0]
        if exe == "python":
            if "-m" in command:
                module_index = command.index("-m") + 1
                if module_index < len(command):
                    module_name = command[module_index]
                    if not _skill_contains_python_module(skill_path, str(module_name)):
                        return {
                            "error": "no_executable_found",
                            "skill": skill_name,
                            "reason": "python -m module not found in skill folder",
                            "module": str(module_name),
                        }
                    module_check = _ensure_python_module(str(module_name), auto_install=auto_install, cwd=self.session_dir)
                    if not module_check.get("ok"):
                        return module_check
            command = [sys.executable] + command[1:]
        elif exe not in ALLOWED_COMMANDS:
            return {"error": f"command not allowed: {exe}"}
        cwd = skill_path if not cwd_relative else _safe_join(skill_path, cwd_relative)
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        return {"returncode": result.returncode, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}

    def run_temp_command(
        self, *, command: list[str], cwd_relative: str | None = None, auto_install: bool = False
    ) -> dict[str, Any]:
        if not command:
            return {"error": "command must be a non-empty list"}
        exe = command[0]
        if exe == "python":
            if "-m" in command:
                module_index = command.index("-m") + 1
                if module_index < len(command):
                    module_name = command[module_index]
                    module_check = _ensure_python_module(str(module_name), auto_install=auto_install, cwd=self.session_dir)
                    if not module_check.get("ok"):
                        return module_check
            command = [sys.executable] + command[1:]
        elif exe not in ALLOWED_COMMANDS:
            return {"error": f"command not allowed: {exe}"}
        os.makedirs(self.session_dir, exist_ok=True)
        cwd = self.session_dir if not cwd_relative else _safe_join(self.session_dir, cwd_relative)
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        return {"returncode": result.returncode, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}

    def export_temp_file(
        self,
        *,
        temp_relative_path: str,
        workspace_relative_path: str,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        os.makedirs(self.session_dir, exist_ok=True)
        src = _safe_join(self.session_dir, temp_relative_path)
        if not os.path.isfile(src):
            return {"error": "source file not found", "temp_relative_path": temp_relative_path}
        return {
            "source": src,
            "relative_path": temp_relative_path,
            "bytes": os.path.getsize(src),
            "note": "export_temp_file does not copy files; tool marks final output only",
            "requested_name": workspace_relative_path,
            "overwrite": overwrite,
        }


def _get_summary_storage_key(session: Any) -> str:
    candidates = [
        _safe_get(session, "conversation_id"),
        _safe_get(session, "chat_id"),
        _safe_get(session, "task_id"),
        _safe_get(session, "id"),
        _safe_get(session, "session_id"),
        _safe_get(session, "app_run_id"),
    ]
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return SUMMARY_KEY_PREFIX + c.strip()
    return SUMMARY_KEY_PREFIX + "global"


def _storage_get_text(storage: Any, key: str) -> str:
    try:
        val = storage.get(key)
        if not val:
            return ""
        if isinstance(val, bytes):
            return val.decode("utf-8", errors="ignore")
        if isinstance(val, str):
            return val
        return ""
    except Exception:
        return ""


def _storage_set_text(storage: Any, key: str, text: str) -> None:
    try:
        storage.set(key, (text or "").encode("utf-8"))
    except Exception:
        return


def _guess_mime_type(filename: str) -> str:
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if name.endswith(".xls"):
        return "application/vnd.ms-excel"
    if name.endswith(".csv"):
        return "text/csv"
    if name.endswith(".json"):
        return "application/json"
    if name.endswith(".txt"):
        return "text/plain"
    if name.endswith(".md"):
        return "text/markdown"
    if name.endswith(".png"):
        return "image/png"
    if name.endswith(".jpg") or name.endswith(".jpeg"):
        return "image/jpeg"
    if name.endswith(".pdf"):
        return "application/pdf"
    if name.endswith(".zip"):
        return "application/zip"
    return "application/octet-stream"


def _shorten_text(value: Any, max_len: int = 500) -> str:
    try:
        s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    except Exception:
        s = str(value)
    s = s.replace("\r", "\\r").replace("\n", "\\n")
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


def _model_brief(model_config: Any) -> str:
    if isinstance(model_config, dict):
        provider = model_config.get("provider")
        model = model_config.get("model")
        mode = model_config.get("mode")
        return f"provider={provider!s} model={model!s} mode={mode!s}"
    provider = _safe_get(model_config, "provider")
    model = _safe_get(model_config, "model")
    mode = _safe_get(model_config, "mode")
    return f"provider={provider!s} model={model!s} mode={mode!s}"


def _dbg(msg: str) -> None:
    try:
        print(f"[skill][debug] {msg}", flush=True)
    except Exception:
        return


def _coerce_content_item_to_dict(item: Any) -> dict[str, Any] | None:
    if item is None:
        return None
    if isinstance(item, dict):
        return item
    try:
        dumped = item.model_dump()  # type: ignore[attr-defined]
        if isinstance(dumped, dict):
            return dumped
    except Exception:
        pass
    try:
        item_type = getattr(item, "type", None)
        if item_type:
            result: dict[str, Any] = {"type": item_type}
            for k in ("data", "format", "base64_data", "url", "mime_type", "filename", "detail"):
                v = getattr(item, k, None)
                if v not in (None, ""):
                    result[k] = v
            return result
    except Exception:
        pass
    return None


def _split_message_content(content: Any) -> tuple[str, list[dict[str, Any]]]:
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if isinstance(content, list) or isinstance(content, tuple):
        text_parts: list[str] = []
        nontext_parts: list[dict[str, Any]] = []
        for item in content:
            item_dict = _coerce_content_item_to_dict(item)
            if not item_dict:
                continue
            item_type = item_dict.get("type")
            if item_type == "text":
                data = item_dict.get("data")
                if isinstance(data, str) and data:
                    text_parts.append(data)
            else:
                nontext_parts.append(item_dict)
        return "".join(text_parts), nontext_parts
    return "", [{"type": "unknown", "value": str(content)}]


def _invoke_llm(
    llm: Any,
    *,
    model_config: Any,
    prompt_messages: list[Any],
    tools: list[Any] | None,
) -> tuple[str, list[Any], Any, int]:
    nontext_content: list[dict[str, Any]] = []
    tool_calls_all: list[Any] = []
    text_parts: list[str] = []
    chunks_count = 0

    try:
        response = llm.invoke(
            model_config=model_config,
            prompt_messages=prompt_messages,
            tools=tools,
            stream=True,
        )
    except TypeError:
        response = llm.invoke(
            model_config=model_config,
            prompt_messages=prompt_messages,
            stream=True,
        )

    if _safe_get(response, "message") is not None:
        msg = _safe_get(response, "message") or {}
        content = _safe_get(msg, "content")
        text, parts = _split_message_content(content)
        if parts:
            nontext_content.extend(parts)
        tool_calls = _safe_get(msg, "tool_calls") or []
        if isinstance(tool_calls, list):
            tool_calls_all.extend(tool_calls)
        return text.strip(), tool_calls_all, nontext_content, chunks_count

    try:
        for chunk in response:
            chunks_count += 1
            delta = _safe_get(chunk, "delta") or {}
            msg = _safe_get(delta, "message") or {}
            content = _safe_get(msg, "content")
            t, parts = _split_message_content(content)
            if t:
                text_parts.append(t)
            if parts:
                nontext_content.extend(parts)
            tc = _safe_get(msg, "tool_calls") or []
            if isinstance(tc, list) and tc:
                tool_calls_all.extend(tc)
    except Exception as e:
        return "", [], {"error": "stream_parse_failed", "exception": str(e)}, chunks_count

    return "".join(text_parts).strip(), tool_calls_all, nontext_content, chunks_count


class SkillAgentTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        model = tool_parameters.get("model")
        query = tool_parameters.get("query")
        max_steps = int(tool_parameters.get("max_steps") or 8)
        memory_turns = int(tool_parameters.get("memory_turns") or 10)
        system_prompt = tool_parameters.get("system_prompt") or "你是一个xxxx"
        skills_root = _detect_skills_root(tool_parameters.get("skills_root"))

        if not query or not isinstance(query, str):
            yield self.create_text_message("❌缺少 query 参数\n")
            return

        storage = self.session.storage
        summary_key = _get_summary_storage_key(self.session)
        resume_key = _get_resume_storage_key(self.session)
        resume_state = _storage_get_json(storage, resume_key)
        resume_pending = bool(resume_state.get("pending"))
        is_resuming = False

        plugin_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        temp_root = os.path.join(plugin_root, "temp")
        os.makedirs(temp_root, exist_ok=True)
        session_dir = os.path.join(temp_root, f"dify-skill-{uuid.uuid4().hex[:8]}-")
        resume_context = ""
        if resume_pending and _is_deny_reply(query):
            _storage_set_json(storage, resume_key, None)
            yield self.create_text_message("🤝已收到你的拒绝，本次不会在 temp 目录创建脚本继续执行。\n")
            return
        if resume_pending and _is_allow_reply(query):
            candidate = str(resume_state.get("session_dir") or "").strip()
            if candidate:
                session_dir = candidate
                os.makedirs(session_dir, exist_ok=True)
                original_query_for_resume = str(resume_state.get("original_query") or "").strip()
                if original_query_for_resume:
                    query = original_query_for_resume
                is_resuming = True
                _storage_set_json(storage, resume_key, None)
                resume_context = (
                    "\n\n[续跑授权]\n"
                    + "用户已明确允许你在 temp 会话目录中自行创建脚本、必要时安装依赖，并继续上一轮未完成的生成。\n"
                    + "请直接基于当前 temp 会话目录中的中间产物继续推进，优先生成最终可交付文件。\n"
                )
        os.makedirs(session_dir, exist_ok=True)
        if not is_resuming:
            _cleanup_old_temp_sessions(temp_root, keep=4, protect_dirs={session_dir})

        runtime = _AgentRuntime(
            skills_root=skills_root,
            session_dir=session_dir,
            max_steps=max_steps,
            memory_turns=memory_turns,
        )

        existing_summary = _storage_get_text(storage, summary_key).strip()

        skills_index = runtime.load_skills_index()
        try:
            skills_count = len(skills_index.get("skills") or []) if isinstance(skills_index, dict) else 0
        except Exception:
            skills_count = 0
        _dbg(
            "start "
            + _model_brief(model)
            + f" session_dir={session_dir} skills_root={skills_root!s} skills_count={skills_count} "
            + f"query_len={len(query)}"
        )
        system_content = (
            system_prompt.strip()
            + ("\n\n对话摘要（自动生成）：\n" + existing_summary if existing_summary else "")
            + "\n\n你是一个使用 Skills 文件夹作为“工具箱”的通用型 Agent。\n"
            + "你必须遵循渐进式披露流程：\n"
            + "1) 只根据技能元数据（name/description）判断可能相关的技能\n"
            + "2) 触发时才调用 get_skill_metadata 读取 SKILL.md（说明文档）\n"
            + "3) 只有在需要更深信息时，才调用 list_skill_files / read_skill_file\n"
            + "4) 只有在明确需要执行脚本/命令时，才调用 run_skill_command\n"
            + "5) 执行前必须先确认技能包内确实存在可执行入口（脚本/模块等），不要猜测模块名；如果缺少可执行入口，则先交付当前可交付产物，并询问用户是否允许你在 temp 目录中自行创建脚本后再尝试生成。\n"
            + "补充规则：如果用户请求中已经明确给出具体类型/参数，则视为已确认，不要重复追问，直接进入对应分支执行。\n"
            + "补充规则：同一轮内如已获取过某技能的 skill_md，请勿重复调用 get_skill_metadata；可 read_temp_file(skill_md_path)。\n"
            + "你必须把实现过程中的中间产物写入 temp 会话目录（脚本、草稿、生成物等）：\n"
            + "- 写文本：write_temp_file\n"
            + "- 运行命令生成文件：run_temp_command\n"
            + "对任何“有明确交付物”的请求，你必须在同一轮内推进直到：生成可交付文件，或给出明确失败原因。\n"
            + "本工具会在结束时把 temp 目录里的所有文件自动作为文件输出返回给用户。\n\n"
            + "可用动作：\n"
            + "- get_session_context()\n"
            + "- get_skill_metadata(skill_name)\n"
            + "- list_skill_files(skill_name, max_depth)\n"
            + "- read_skill_file(skill_name, relative_path, max_chars)\n"
            + "- run_skill_command(skill_name, command, cwd_relative, auto_install)\n"
            + "- write_temp_file(relative_path, content)\n"
            + "- read_temp_file(relative_path, max_chars)\n"
            + "- list_temp_files(max_depth)\n"
            + "- run_temp_command(command, cwd_relative, auto_install)\n"
            + "- export_temp_file(temp_relative_path, workspace_relative_path, overwrite)  # 不复制，仅标记交付名\n\n"
            + "如果模型支持 function call，请直接发起工具调用；若不支持，则用 JSON 协议响应：\n"
            + '{"type":"tool","name":"get_skill_metadata","arguments":{"skill_name":"xxx"}}\n'
            + '或 {"type":"final","content":"...","files":[{"path":"relative","mime_type":"...","filename":"..."}]}\n\n'
            + "技能索引（用于判断是否需要调用技能）：\n"
            + json.dumps(skills_index, ensure_ascii=False)
            + (resume_context or "")
        )

        messages: list[Any] = [
            SystemPromptMessage(content=system_content),
            UserPromptMessage(content=query),
        ]

        def compact() -> None:
            if memory_turns <= 0:
                return
            keep = 1 + memory_turns * 4
            if len(messages) > keep:
                system_msg = messages[0]
                tail = messages[-(keep - 1) :]
                messages[:] = [system_msg, *tail]

        final_text: str | None = None
        final_file_meta: dict[str, dict[str, str]] = {}
        empty_responses = 0
        saved_asset_fingerprints: set[str] = set()
        resume_saved = False
        final_text_already_streamed = False

        def stream_text_to_user(text: str, chunk_size: int = 8) -> Generator[ToolInvokeMessage]:
            s = (text or "").strip()
            if not s:
                return
            step = max(1, int(chunk_size))
            for i in range(0, len(s), step):
                yield self.create_text_message(s[i : i + step])

        def persist_llm_assets(parts: Any) -> list[str]:
            if not parts or not isinstance(parts, list):
                return []
            saved: list[str] = []
            out_dir = _safe_join(session_dir, "llm_assets")
            os.makedirs(out_dir, exist_ok=True)
            for i, item in enumerate(parts):
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "")
                if item_type not in {"image", "document", "audio", "video"}:
                    continue
                mime = str(item.get("mime_type") or "")
                filename = str(item.get("filename") or "").strip()
                url = str(item.get("url") or item.get("data") or "").strip()
                b64 = str(item.get("base64_data") or "").strip()
                raw: bytes | None = None
                if b64:
                    try:
                        raw = base64.b64decode(b64, validate=False)
                    except Exception:
                        raw = None
                if raw is None and url.startswith("data:") and ";base64," in url:
                    try:
                        header, payload = url.split(";base64,", 1)
                        if not mime and header.startswith("data:"):
                            mime = header[5:]
                        raw = base64.b64decode(payload, validate=False)
                    except Exception:
                        raw = None
                if raw is None:
                    continue
                try:
                    fp = hashlib.sha1(raw).hexdigest()
                    key = f"{item_type}|{mime}|{fp}"
                except Exception:
                    key = f"{item_type}|{mime}|{len(raw)}"
                if key in saved_asset_fingerprints:
                    continue
                saved_asset_fingerprints.add(key)
                if not filename:
                    ext = ""
                    if mime:
                        if "png" in mime:
                            ext = ".png"
                        elif "jpeg" in mime or "jpg" in mime:
                            ext = ".jpg"
                        elif "pdf" in mime:
                            ext = ".pdf"
                        elif "json" in mime:
                            ext = ".json"
                        elif "text" in mime or "markdown" in mime:
                            ext = ".txt"
                    filename = f"{item_type}-{i+1}{ext or ''}"
                dst = _safe_join(out_dir, filename)
                if os.path.exists(dst):
                    base, ext = os.path.splitext(filename)
                    dst = _safe_join(out_dir, f"{base}-{fp[:8] if 'fp' in locals() else uuid.uuid4().hex[:8]}{ext}")
                try:
                    with open(dst, "wb") as f:
                        f.write(raw)
                    saved.append(os.path.relpath(dst, session_dir))
                except Exception:
                    continue
            return saved

        def invoke_llm_live(
            *, prompt_messages: list[Any], tools: list[Any] | None
        ) -> Generator[ToolInvokeMessage, None, tuple[str, list[Any], Any, int, bool]]:
            nontext_content: list[dict[str, Any]] = []
            tool_calls_all: list[Any] = []
            text_parts: list[str] = []
            chunks_count = 0
            streamed_any = False
            saw_tool_calls = False
            typing_chunk = 6

            def emit_typing(text: str) -> Generator[ToolInvokeMessage, None, None]:
                nonlocal streamed_any
                if not text:
                    return
                tagged = "\n【Agent】\n" + text.strip() + "\n\n"
                step = max(1, int(typing_chunk))
                for i in range(0, len(tagged), step):
                    yield self.create_text_message(tagged[i : i + step])
                    streamed_any = True
            
            def should_emit_user_text(text: str) -> bool:
                if not text:
                    return False
                json_text = _extract_first_json_object(text)
                if not json_text:
                    return True
                try:
                    obj = json.loads(json_text)
                except Exception:
                    return True
                if not isinstance(obj, dict):
                    return True
                t = obj.get("type")
                return t not in {"tool", "final"}

            try:
                try:
                    response = self.session.model.llm.invoke(
                        model_config=model,
                        prompt_messages=prompt_messages,
                        tools=tools,
                        stream=True,
                    )
                except TypeError:
                    response = self.session.model.llm.invoke(
                        model_config=model,
                        prompt_messages=prompt_messages,
                        stream=True,
                    )

                if _safe_get(response, "message") is not None:
                    msg = _safe_get(response, "message") or {}
                    content = _safe_get(msg, "content")
                    text, parts = _split_message_content(content)
                    if parts:
                        nontext_content.extend(parts)
                    tool_calls = _safe_get(msg, "tool_calls") or []
                    if isinstance(tool_calls, list):
                        tool_calls_all.extend(tool_calls)
                        if tool_calls:
                            saw_tool_calls = True
                    if text:
                        text_parts.append(text)
                    combined_text = "".join(text_parts).strip()
                    if combined_text and not saw_tool_calls and should_emit_user_text(combined_text):
                        yield from emit_typing(combined_text)
                    return combined_text, tool_calls_all, nontext_content, chunks_count, streamed_any

                for chunk in response:
                    chunks_count += 1
                    delta = _safe_get(chunk, "delta") or {}
                    msg = _safe_get(delta, "message") or {}
                    content = _safe_get(msg, "content")
                    t, parts = _split_message_content(content)
                    if parts:
                        nontext_content.extend(parts)
                    tc = _safe_get(msg, "tool_calls") or []
                    if isinstance(tc, list) and tc:
                        tool_calls_all.extend(tc)
                        if not saw_tool_calls:
                            saw_tool_calls = True
                    if t:
                        text_parts.append(t)
                combined_text = "".join(text_parts).strip()
                if combined_text and not saw_tool_calls and should_emit_user_text(combined_text):
                    yield from emit_typing(combined_text)
                return combined_text, tool_calls_all, nontext_content, chunks_count, streamed_any
            except Exception as e:
                return "", [], {"error": "stream_parse_failed", "exception": str(e)}, chunks_count, streamed_any

        try:
            for step_idx in range(max_steps):
                compact()
                _dbg(f"step={step_idx+1}/{max_steps} messages={len(messages)}")
                try:
                    res_text, tool_calls, nontext, chunks, streamed_any = yield from invoke_llm_live(
                        prompt_messages=messages,
                        tools=_build_prompt_message_tools(),
                    )
                except Exception as e:
                    msg = str(e)
                    if "NameResolutionError" in msg or "Failed to resolve" in msg:
                        yield self.create_text_message(
                            "❌ LLM 调用失败：无法解析模型服务域名（DNS/网络问题）。\n"
                            "当前报错信息：\n"
                            + msg
                            + "\n\n请检查：\n"
                            + "1) 运行插件的环境是否能访问公网/是否需要代理\n"
                            + "2) DNS 是否可用（能否解析 dashscope.aliyuncs.com 等域名）\n"
                            + "3) Dify 的模型供应商（通义）网络出站是否被限制\n"
                        )
                    else:
                        yield self.create_text_message("❌ LLM 调用失败：\n" + msg)
                    return

                _dbg(
                    f"llm_return content_len={len(res_text)} tool_calls={len(tool_calls)} chunks={chunks} "
                    f"nontext={_shorten_text(nontext, 200) if nontext else ''}"
                )
                if nontext:
                    saved_assets = persist_llm_assets(nontext)
                    if saved_assets:
                        _dbg(f"nontext_assets_saved={len(saved_assets)} paths={_shorten_text(saved_assets, 300)}")
                if tool_calls:
                    empty_responses = 0
                    messages.append(AssistantPromptMessage(content=res_text or "", tool_calls=tool_calls))
                    forced_text: str | None = None
                    for tc in tool_calls:
                        call_id, name, arguments = _parse_tool_call(tc)
                        tool_name = str(name or "")
                        _dbg(f"tool_call name={tool_name} id={call_id!s} args={_shorten_text(arguments, 400)}")

                        if tool_name == "get_skill_metadata":
                            yield self.create_text_message(
                                f"✅正在查看技能《{str(arguments.get('skill_name') or '')}》说明书…\n"
                            )
                        elif tool_name == "list_skill_files":
                            yield self.create_text_message(
                                f"✅正在查看技能《{str(arguments.get('skill_name') or '')}》文件结构…\n"
                            )
                        elif tool_name == "read_skill_file":
                            yield self.create_text_message(
                                f"✅正在读取技能《{str(arguments.get('skill_name') or '')}》文件：{str(arguments.get('relative_path') or '')}…\n"
                            )
                        elif tool_name == "run_skill_command":
                            cmd = arguments.get("command") if isinstance(arguments.get("command"), list) else []
                            yield self.create_text_message(
                                f"✅正在执行技能《{str(arguments.get('skill_name') or '')}》命令：{_shorten_text(cmd, 160)}…\n"
                            )
                        elif tool_name == "write_temp_file":
                            yield self.create_text_message(
                                f"✅正在按说明书写入临时文件：{str(arguments.get('relative_path') or '')}…\n"
                            )
                        elif tool_name == "read_temp_file":
                            yield self.create_text_message(
                                f"✅正在读取临时文件：{str(arguments.get('relative_path') or '')}…\n"
                            )
                        elif tool_name == "list_temp_files":
                            yield self.create_text_message("✅正在查看临时目录文件…\n")
                        elif tool_name == "run_temp_command":
                            cmd = arguments.get("command") if isinstance(arguments.get("command"), list) else []
                            yield self.create_text_message(f"✅正在执行临时命令：{_shorten_text(cmd, 160)}…\n")
                        elif tool_name == "export_temp_file":
                            yield self.create_text_message(
                                f"✅正在标记交付文件：{str(arguments.get('temp_relative_path') or '')}…\n"
                            )

                        if tool_name == "get_skill_metadata":
                            result = runtime.get_skill_metadata(str(arguments.get("skill_name") or ""))
                        elif tool_name == "list_skill_files":
                            result = runtime.list_skill_files(
                                str(arguments.get("skill_name") or ""),
                                int(arguments.get("max_depth") or 2),
                            )
                        elif tool_name == "read_skill_file":
                            result = runtime.read_skill_file(
                                str(arguments.get("skill_name") or ""),
                                str(arguments.get("relative_path") or ""),
                                int(arguments.get("max_chars") or 12000),
                            )
                        elif tool_name == "run_skill_command":
                            result = runtime.run_skill_command(
                                skill_name=str(arguments.get("skill_name") or ""),
                                command=arguments.get("command") if isinstance(arguments.get("command"), list) else [],
                                cwd_relative=(
                                    str(arguments.get("cwd_relative")) if arguments.get("cwd_relative") else None
                                ),
                                auto_install=bool(arguments.get("auto_install") or False),
                            )
                            if isinstance(result, dict) and result.get("error") == "no_executable_found":
                                skill = str(result.get("skill") or arguments.get("skill_name") or "")
                                module = str(result.get("module") or "")
                                forced_text = (
                                    f"当前技能“{skill}”的说明文档要求生成文件，但技能包内未找到可执行入口（例如脚本或 Python 模块）。\n"
                                    f"本次尝试的入口为 python -m {module}，但在技能目录中不存在，因此无法继续生成目标文件。\n\n"
                                    "我已先按技能说明生成了可交付的中间产物（例如设计哲学 .md）。\n"
                                    "你是否允许我在 temp 目录中自行创建可执行脚本，并在需要时安装依赖后，再尝试生成最终文件？"
                                )
                                _storage_set_json(
                                    storage,
                                    resume_key,
                                    {
                                        "pending": True,
                                        "session_dir": session_dir,
                                        "original_query": query,
                                        "reason": "no_executable_found",
                                        "skill": skill,
                                        "module": module,
                                        "created_at": int(time.time()),
                                    },
                                )
                                resume_saved = True
                                _dbg(
                                    "resume_state_saved "
                                    + _shorten_text(
                                        {"session_dir": session_dir, "skill": skill, "module": module, "pending": True},
                                        300,
                                    )
                                )
                        elif tool_name == "get_session_context":
                            result = runtime.get_session_context()
                        elif tool_name == "write_temp_file":
                            result = runtime.write_temp_file(
                                str(arguments.get("relative_path") or ""),
                                str(arguments.get("content") or ""),
                            )
                        elif tool_name == "read_temp_file":
                            result = runtime.read_temp_file(
                                str(arguments.get("relative_path") or ""),
                                int(arguments.get("max_chars") or 12000),
                            )
                        elif tool_name == "list_temp_files":
                            result = runtime.list_temp_files(int(arguments.get("max_depth") or 4))
                        elif tool_name == "run_temp_command":
                            result = runtime.run_temp_command(
                                command=arguments.get("command") if isinstance(arguments.get("command"), list) else [],
                                cwd_relative=(
                                    str(arguments.get("cwd_relative")) if arguments.get("cwd_relative") else None
                                ),
                                auto_install=bool(arguments.get("auto_install") or False),
                            )
                        elif tool_name == "export_temp_file":
                            temp_rel = str(arguments.get("temp_relative_path") or "")
                            workspace_rel = str(arguments.get("workspace_relative_path") or "")
                            result = runtime.export_temp_file(
                                temp_relative_path=temp_rel,
                                workspace_relative_path=workspace_rel,
                                overwrite=bool(arguments.get("overwrite") or False),
                            )
                            out_name = os.path.basename(workspace_rel) if workspace_rel else ""
                            if temp_rel and out_name:
                                final_file_meta[temp_rel] = {
                                    **(final_file_meta.get(temp_rel) or {}),
                                    "filename": out_name,
                                }
                        else:
                            result = {"error": f"unknown tool: {tool_name}"}

                        _dbg(f"tool_result name={tool_name} result={_shorten_text(result, 700)}")
                        messages.append(
                            ToolPromptMessage(
                                tool_call_id=str(call_id or ""),
                                name=tool_name,
                                content=json.dumps(result, ensure_ascii=False),
                            )
                        )
                    if forced_text:
                        final_text = forced_text
                        break
                    if step_idx >= max_steps - 1:
                        try:
                            has_files = any(
                                e.get("type") == "file"
                                for e in _list_dir(session_dir, max_depth=2)
                                if isinstance(e, dict)
                            )
                        except Exception:
                            has_files = False
                        if final_file_meta or has_files:
                            final_text = "已生成文件。"
                            break
                    continue

                json_text = _extract_first_json_object(res_text)
                action: dict[str, Any] | None = None
                if json_text:
                    try:
                        action = json.loads(json_text)
                    except Exception:
                        action = None
                _dbg(f"json_protocol detected={bool(action)} snippet={_shorten_text(json_text or '', 200)}")

                if not res_text and not action and not nontext:
                    empty_responses += 1
                    _dbg(f"empty_response_count={empty_responses}")
                    if empty_responses < 3:
                        messages.append(
                            UserPromptMessage(
                                content='你刚才没有输出任何内容。请继续完成任务：如果支持函数调用请调用工具；否则请输出 JSON：{"type":"final","content":"...","files":[...]}'
                            )
                        )
                        continue
                    final_text = "模型连续返回空响应，未生成任何结果。"
                    break

                if not action or action.get("type") == "final":
                    if action and action.get("type") == "final":
                        final_text = str(action.get("content") or "")
                        _dbg(f"final_json content_len={len(final_text)}")
                        files = action.get("files") or []
                        if isinstance(files, list):
                            for f in files:
                                if not isinstance(f, dict):
                                    continue
                                rel = f.get("path")
                                if not rel or not isinstance(rel, str):
                                    continue
                                meta: dict[str, str] = {}
                                if f.get("mime_type"):
                                    meta["mime_type"] = str(f.get("mime_type"))
                                if f.get("filename"):
                                    meta["filename"] = str(f.get("filename"))
                                final_file_meta[rel] = meta
                    else:
                        final_text = res_text
                        _dbg(f"final_text content_len={len(final_text)}")
                        if streamed_any and final_text:
                            final_text_already_streamed = True
                    break

                if action.get("type") != "tool":
                    final_text = res_text
                    _dbg(f"final_non_tool type={action.get('type')!s} content_len={len(final_text)}")
                    break

                name = str(action.get("name") or "")
                arguments = action.get("arguments") or {}
                if not isinstance(arguments, dict):
                    arguments = {}

                _dbg(f"json_tool name={name} args={_shorten_text(arguments, 400)}")
                messages.append(AssistantPromptMessage(content=json.dumps(action, ensure_ascii=False)))

                if name == "get_skill_metadata":
                    yield self.create_text_message(f"✅正在查看技能《{str(arguments.get('skill_name') or '')}》说明书…\n")
                elif name == "list_skill_files":
                    yield self.create_text_message(f"✅正在查看技能《{str(arguments.get('skill_name') or '')}》文件结构…\n")
                elif name == "read_skill_file":
                    yield self.create_text_message(
                        f"✅正在读取技能《{str(arguments.get('skill_name') or '')}》文件：{str(arguments.get('relative_path') or '')}…\n"
                    )
                elif name == "run_skill_command":
                    cmd = arguments.get("command") if isinstance(arguments.get("command"), list) else []
                    yield self.create_text_message(
                        f"✅正在执行技能《{str(arguments.get('skill_name') or '')}》命令：{_shorten_text(cmd, 160)}…\n"
                    )
                elif name == "write_temp_file":
                    yield self.create_text_message(f"✅正在按说明书写入临时文件：{str(arguments.get('relative_path') or '')}…\n")
                elif name == "read_temp_file":
                    yield self.create_text_message(f"✅正在读取临时文件：{str(arguments.get('relative_path') or '')}…\n")
                elif name == "list_temp_files":
                    yield self.create_text_message("✅正在查看临时目录文件…\n")
                elif name == "run_temp_command":
                    cmd = arguments.get("command") if isinstance(arguments.get("command"), list) else []
                    yield self.create_text_message(f"✅正在执行临时命令：{_shorten_text(cmd, 160)}…\n")
                elif name == "export_temp_file":
                    yield self.create_text_message(f"✅正在标记交付文件：{str(arguments.get('temp_relative_path') or '')}…\n")

                if name == "get_skill_metadata":
                    result = runtime.get_skill_metadata(str(arguments.get("skill_name") or ""))
                elif name == "list_skill_files":
                    result = runtime.list_skill_files(
                        str(arguments.get("skill_name") or ""),
                        int(arguments.get("max_depth") or 2),
                    )
                elif name == "read_skill_file":
                    result = runtime.read_skill_file(
                        str(arguments.get("skill_name") or ""),
                        str(arguments.get("relative_path") or ""),
                        int(arguments.get("max_chars") or 12000),
                    )
                elif name == "run_skill_command":
                    result = runtime.run_skill_command(
                        skill_name=str(arguments.get("skill_name") or ""),
                        command=arguments.get("command") if isinstance(arguments.get("command"), list) else [],
                        cwd_relative=(str(arguments.get("cwd_relative")) if arguments.get("cwd_relative") else None),
                        auto_install=bool(arguments.get("auto_install") or False),
                    )
                elif name == "get_session_context":
                    result = runtime.get_session_context()
                elif name == "write_temp_file":
                    result = runtime.write_temp_file(
                        str(arguments.get("relative_path") or ""),
                        str(arguments.get("content") or ""),
                    )
                elif name == "read_temp_file":
                    result = runtime.read_temp_file(
                        str(arguments.get("relative_path") or ""),
                        int(arguments.get("max_chars") or 12000),
                    )
                elif name == "list_temp_files":
                    result = runtime.list_temp_files(int(arguments.get("max_depth") or 4))
                elif name == "run_temp_command":
                    result = runtime.run_temp_command(
                        command=arguments.get("command") if isinstance(arguments.get("command"), list) else [],
                        cwd_relative=(str(arguments.get("cwd_relative")) if arguments.get("cwd_relative") else None),
                        auto_install=bool(arguments.get("auto_install") or False),
                    )
                elif name == "export_temp_file":
                    temp_rel = str(arguments.get("temp_relative_path") or "")
                    workspace_rel = str(arguments.get("workspace_relative_path") or "")
                    result = runtime.export_temp_file(
                        temp_relative_path=temp_rel,
                        workspace_relative_path=workspace_rel,
                        overwrite=bool(arguments.get("overwrite") or False),
                    )
                    out_name = os.path.basename(workspace_rel) if workspace_rel else ""
                    if temp_rel and out_name:
                        final_file_meta[temp_rel] = {**(final_file_meta.get(temp_rel) or {}), "filename": out_name}
                else:
                    result = {"error": f"unknown tool: {name}"}

                _dbg(f"json_tool_result name={name} result={_shorten_text(result, 700)}")
                messages.append(
                    AssistantPromptMessage(
                        content="TOOL_RESULT\n" + json.dumps({"name": name, "result": result}, ensure_ascii=False)
                    )
                )
            else:
                try:
                    has_files = any(
                        e.get("type") == "file" for e in _list_dir(session_dir, max_depth=2) if isinstance(e, dict)
                    )
                except Exception:
                    has_files = False
                if final_file_meta or has_files:
                    final_text = "已生成文件。"
                else:
                    final_text = f"❌超过最大执行轮数 max_steps={max_steps}，仍未得到最终结果"
        finally:
            if not resume_saved and not is_resuming and resume_pending:
                _storage_set_json(storage, resume_key, None)
            temp_files_text = ""
            try:
                temp_entries = _list_dir(session_dir, max_depth=10)
                rel_paths = [
                    str(e.get("relative_path"))
                    for e in temp_entries
                    if e.get("type") == "file" and isinstance(e.get("relative_path"), str)
                ]
                if rel_paths:
                    temp_files_text = "\n\n[temp_files]\n" + "\n".join(rel_paths)
                _dbg(f"temp_files_count={len(rel_paths)}")
            except Exception:
                temp_files_text = ""

            summary_input = (f"[user]\n{query}\n\n[assistant]\n{final_text or ''}" + temp_files_text).strip()
            if len(summary_input) > SUMMARY_INPUT_MAX_CHARS:
                summary_input = summary_input[-SUMMARY_INPUT_MAX_CHARS:]

            summary_system = SystemPromptMessage(
                content="你是一个会话摘要器。你将把新对话内容合并进已有摘要，输出中文摘要，要求简洁、结构化，保留关键事实、已生成文件路径、失败原因与待办。只输出摘要正文，不要加多余解释。"
            )
            summary_user = UserPromptMessage(
                content="已有摘要：\n"
                + (existing_summary or "(空)")
                + "\n\n新增对话内容：\n"
                + (summary_input or "(空)")
            )
            try:
                summary_response = self.session.model.llm.invoke(
                    model_config=model,
                    prompt_messages=[summary_system, summary_user],
                    stream=False,
                )
                summary_text = str(_safe_get(_safe_get(summary_response, "message"), "content") or "").strip()
                if summary_text:
                    _storage_set_text(storage, summary_key, summary_text)
            except Exception:
                pass

            files_to_send: list[tuple[str, str, str, str]] = []
            try:
                entries = _list_dir(session_dir, max_depth=10)
                for e in entries:
                    if e.get("type") != "file":
                        continue
                    rel = e.get("relative_path")
                    path = e.get("path")
                    if not rel or not isinstance(rel, str) or not path or not isinstance(path, str):
                        continue
                    rel_norm = rel.replace("\\", "/").lstrip("/")
                    if rel_norm.startswith("_skill_cache/"):
                        continue
                    filename = os.path.basename(rel)
                    meta_override = final_file_meta.get(rel) or {}
                    mime_type = meta_override.get("mime_type") or _guess_mime_type(filename)
                    out_name = meta_override.get("filename") or filename
                    files_to_send.append((rel, path, mime_type, out_name))
            except Exception:
                files_to_send = []

            if final_text and final_text.strip():
                if not final_text_already_streamed:
                    yield from stream_text_to_user(final_text)
            elif files_to_send:
                yield from stream_text_to_user("已生成文件。")
            else:
                yield from stream_text_to_user("未生成任何文本或文件输出。")

            yielded: set[str] = set()
            yielded_fingerprints: set[str] = set()
            for rel, path, mime_type, out_name in files_to_send:
                if rel in yielded:
                    continue
                yielded.add(rel)
                try:
                    with open(path, "rb") as fp:
                        content = fp.read()
                    try:
                        content_fp = hashlib.sha1(content).hexdigest()
                    except Exception:
                        content_fp = str(len(content))
                    fingerprint_key = f"{out_name}|{mime_type}|{content_fp}"
                    if fingerprint_key in yielded_fingerprints:
                        continue
                    yielded_fingerprints.add(fingerprint_key)
                    yield self.create_blob_message(blob=content, meta={"mime_type": mime_type, "filename": out_name})
                except Exception:
                    continue
            _dbg(f"temp_retained session_dir={session_dir}")
