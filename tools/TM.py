from __future__ import annotations

import mimetypes
import os
import re
import shutil
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import Any

from dify_plugin import Tool
from dify_plugin.entities.tool import ToolInvokeMessage


def get_skills_dir(explicit_path: str | None = None) -> Path:
    if explicit_path:
        p = Path(explicit_path)
        if p.is_dir():
            return p

    env_path = os.getenv("SKILLS_ROOT")
    if env_path and os.path.isdir(env_path):
        return Path(env_path)

    root = Path(__file__).resolve().parent.parent
    skills_dir = root / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    return skills_dir


def list_skills_sorted(root_path: str | None = None) -> list[Path]:
    skills_dir = get_skills_dir(root_path)
    if not skills_dir.exists():
        return []
    folders = [p for p in skills_dir.iterdir() if p.is_dir()]
    folders.sort(key=lambda p: p.stat().st_ctime)
    return folders


class TMTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        command = str(tool_parameters.get("command", "")).strip()
        skills_root = str(tool_parameters.get("skills_root") or "").strip() or None

        if command in ("查看技能", "查看 技能", "查看"):
            skills = list_skills_sorted(skills_root)
            if not skills:
                yield self.create_text_message(f"❌当前目录（{get_skills_dir(skills_root)}）下没有已存入的技能包。\n")
                return
            lines = [f"{idx + 1}. {p.name}" for idx, p in enumerate(skills)]
            yield self.create_text_message(f"📂技能目录：{get_skills_dir(skills_root)}\n" + "\n".join(lines))
            return

        if command in ("新增技能", "存入技能", "保存技能"):
            yield self.create_text_message(
                "⚠️注意：本插件已配置为使用本地挂载的技能目录。\n"
                f"当前目录：{get_skills_dir(skills_root)}\n"
                "请直接在文件系统中将技能文件夹放入该目录即可，无需通过此工具导入 ZIP 包。\n"
            )
            return

        m_del = re.match(r"^删除技能(\d+)$", command)
        if m_del:
            idx = int(m_del.group(1))
            skills = list_skills_sorted(skills_root)
            if idx < 1 or idx > len(skills):
                yield self.create_text_message("❌技能序号无效或超出范围。请先使用“查看技能”确认序号。\n")
                return
            target = skills[idx - 1]
            try:
                shutil.rmtree(target, ignore_errors=False)
            except Exception as e:
                yield self.create_text_message(f"❌删除失败：{e}\n")
                return
            yield self.create_text_message(f"✅已删除技能{idx}：{target.name}\n")
            skills = list_skills_sorted(skills_root)
            if not skills:
                yield self.create_text_message("😑当前技能列表为空。\n")
            else:
                lines = [f"{i + 1}. {p.name}" for i, p in enumerate(skills)]
                yield self.create_text_message("👓当前技能列表：\n" + "\n".join(lines))
            return

        m_dl = re.match(r"^下载技能(\d+)$", command)
        if m_dl:
            idx = int(m_dl.group(1))
            skills = list_skills_sorted(skills_root)
            if idx < 1 or idx > len(skills):
                yield self.create_text_message("❌技能序号无效或超出范围。请先使用“查看技能”确认序号。\n")
                return
            target = skills[idx - 1]

            try:
                with tempfile.TemporaryDirectory(prefix="skill-zip-") as td:
                    tmp_dir = Path(td)
                    zip_path = tmp_dir / f"{target.name}.zip"
                    shutil.make_archive(str(zip_path.with_suffix("")), "zip", root_dir=target.parent, base_dir=target.name)
                    blob = zip_path.read_bytes()
            except Exception as e:
                yield self.create_text_message(f"❌读取文件失败：{e}\n")
                return

            mime_type, _ = mimetypes.guess_type(f"{target.name}.zip")
            if not mime_type:
                mime_type = "application/zip"

            yield self.create_text_message(f"⬇️开始下载技能{idx}：{target.name}.zip\n")
            yield self.create_blob_message(
                blob=blob,
                meta={
                    "mime_type": mime_type,
                    "filename": f"{target.name}.zip",
                },
            )
            return

        yield self.create_text_message("😑未识别的技能管理命令。支持：查看技能、新增技能、删除技能N、下载技能N。\n")
        return
