"""项目状态采集器（技术架构 §7 / 实现plan §十四）。

设计约束：
1. **不扫描整个项目**——只做标记文件识别 + 顶层结构 + Git 状态。
2. Memory 只描述状态，真实代码归文件系统（技术架构 §3.3）。
3. 所有 IO 失败一律降级，不抛异常。
"""

from __future__ import annotations

import json
import os
import platform
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from amt.core.models import GitContext
from amt.services.git import GitService
from amt.services.shell import ShellResolver, IS_WINDOWS

_NOISE_DIRS = {
    ".git", ".idea", ".vscode", ".venv", "venv", "node_modules", "__pycache__",
    "dist", "build", "target", "out", ".next", ".nuxt", ".cache", ".mypy_cache",
    ".pytest_cache", ".gradle", ".mvn", "logs", "log", ".DS_Store", "coverage",
}

#: 标记文件 → (语言, 框架/工具)
_MARKERS: dict[str, tuple[list[str], list[str]]] = {
    "pom.xml": (["Java"], ["Maven"]),
    "build.gradle": (["Java", "Kotlin"], ["Gradle"]),
    "build.gradle.kts": (["Kotlin"], ["Gradle"]),
    "settings.gradle": ([], ["Gradle"]),
    "package.json": (["JavaScript"], ["Node.js"]),
    "tsconfig.json": (["TypeScript"], []),
    "requirements.txt": (["Python"], []),
    "pyproject.toml": (["Python"], []),
    "setup.py": (["Python"], []),
    "Pipfile": (["Python"], []),
    "go.mod": (["Go"], []),
    "Cargo.toml": (["Rust"], []),
    "composer.json": (["PHP"], []),
    "Gemfile": (["Ruby"], []),
    "angular.json": (["TypeScript"], ["Angular"]),
    "next.config.js": ([], ["Next.js"]),
    "next.config.mjs": ([], ["Next.js"]),
    "vite.config.ts": ([], ["Vite"]),
    "vite.config.js": ([], ["Vite"]),
    "vue.config.js": ([], ["Vue"]),
    "nuxt.config.ts": ([], ["Nuxt"]),
    "Dockerfile": ([], ["Docker"]),
    "docker-compose.yml": ([], ["Docker Compose"]),
    "docker-compose.yaml": ([], ["Docker Compose"]),
}

#: 项目约定文件（可能包含编码规范 / 架构约束）
_CONVENTION_FILES = ("AGENTS.md", "CLAUDE.md", ".cursorrules", "CONTRIBUTING.md")

_MAX_READ = 4000


def read_text_safe(path: str | Path, limit: int = _MAX_READ) -> str | None:
    """多编码安全读取，失败返回 None。"""
    p = Path(path)
    if not p.is_file():
        return None
    for enc in ("utf-8", "utf-8-sig", "gbk", "cp936", "latin-1"):
        try:
            return p.read_text(encoding=enc)[:limit]
        except (UnicodeDecodeError, LookupError):
            continue
    try:
        return p.read_bytes()[:limit].decode("utf-8", errors="replace")
    except OSError:
        return None


class ProjectState(BaseModel):
    """采集到的项目真实状态。这是「Memory 之外的真实事实来源」。"""

    model_config = ConfigDict(extra="ignore")

    cwd: str = ""
    exists: bool = False
    is_git_repo: bool = False
    operating_system: str = ""
    shell: str | None = None

    project_name: str = ""
    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    marker_files: list[str] = Field(default_factory=list)
    structure_summary: str = ""
    important_modules: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)

    git: GitContext = Field(default_factory=GitContext)
    tracked_changes: list[str] = Field(default_factory=list)
    untracked_files: list[str] = Field(default_factory=list)

    notes: list[str] = Field(default_factory=list)


class ProjectStateCollector:
    def __init__(self, git: GitService | None = None, shell: ShellResolver | None = None) -> None:
        self.shell = shell or ShellResolver()
        self.git = git or GitService()

    # ------------------------------------------------------------------
    def collect(self, cwd: str | Path | None) -> ProjectState:
        state = ProjectState(
            operating_system=f"{platform.system()} {platform.release()} ({platform.machine()})",
            shell=self.shell.detect().type.value,
        )
        if not cwd:
            state.notes.append("未提供项目目录")
            return state

        root = Path(str(cwd)).expanduser()
        state.cwd = str(root)
        if not root.is_dir():
            state.notes.append(f"项目路径不存在：{root}")
            return state
        state.exists = True
        state.project_name = root.name

        self._detect_stack(root, state)
        self._summarize_structure(root, state)
        self._read_conventions(root, state)

        if self.git.available:
            state.git = self.git.collect(root)
            state.is_git_repo = self.git.is_repository(root)
            lines = self.git.status_lines(root)
            state.tracked_changes, state.untracked_files = self.git.parse_status(lines)
        else:
            state.notes.append("未找到 git 可执行文件，Git 状态不可用")
            state.tracked_changes = []
            state.untracked_files = []

        return state

    # ------------------------------------------------------------------
    def _detect_stack(self, root: Path, state: ProjectState) -> None:
        languages: list[str] = []
        frameworks: list[str] = []
        markers: list[str] = []

        for marker, (langs, fws) in _MARKERS.items():
            if (root / marker).is_file():
                markers.append(marker)
                languages.extend(langs)
                frameworks.extend(fws)

        # 根据依赖清单细化框架识别
        pkg = root / "package.json"
        if pkg.is_file():
            frameworks.extend(self._frameworks_from_package_json(pkg))

        # 没有标记文件时按扩展名粗判（仅顶层，不做全量扫描）
        if not languages:
            languages.extend(self._guess_languages(root))

        state.marker_files = markers[:20]
        state.languages = _dedupe(languages)
        state.frameworks = _dedupe(frameworks)

    @staticmethod
    def _frameworks_from_package_json(path: Path) -> list[str]:
        text = read_text_safe(path, limit=20000)
        if not text:
            return []
        try:
            data = json.loads(text)
        except Exception:
            return []
        deps: dict = {}
        for key in ("dependencies", "devDependencies"):
            value = data.get(key)
            if isinstance(value, dict):
                deps.update(value)
        known = {
            "react": "React",
            "vue": "Vue",
            "next": "Next.js",
            "nuxt": "Nuxt",
            "@angular/core": "Angular",
            "svelte": "Svelte",
            "express": "Express",
            "@nestjs/core": "NestJS",
            "vite": "Vite",
            "webpack": "Webpack",
            "typescript": "TypeScript",
            "element-ui": "Element UI",
            "antd": "Ant Design",
            "axios": "Axios",
        }
        return [name for pkg_name, name in known.items() if pkg_name in deps]

    @staticmethod
    def _guess_languages(root: Path) -> list[str]:
        ext_map = {
            ".java": "Java", ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript",
            ".js": "JavaScript", ".jsx": "JavaScript", ".go": "Go", ".rs": "Rust",
            ".cs": "C#", ".php": "PHP", ".rb": "Ruby", ".kt": "Kotlin", ".vue": "Vue",
        }
        found: list[str] = []
        try:
            for entry in list(root.iterdir())[:200]:
                if entry.is_file():
                    lang = ext_map.get(entry.suffix.lower())
                    if lang:
                        found.append(lang)
        except OSError:
            return []
        return _dedupe(found)

    def _summarize_structure(self, root: Path, state: ProjectState) -> None:
        entries: list[str] = []
        modules: list[str] = []
        try:
            children = sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError as exc:
            state.notes.append(f"无法读取项目目录：{exc}")
            return
        for child in children:
            name = child.name
            if name in _NOISE_DIRS or name.startswith("."):
                continue
            if child.is_dir():
                entries.append(f"{name}/")
                if len(modules) < 12:
                    modules.append(name)
            else:
                entries.append(name)
            if len(entries) >= 40:
                break
        state.structure_summary = "、".join(entries) if entries else "（空目录或无法读取）"
        state.important_modules = modules

    def _read_conventions(self, root: Path, state: ProjectState) -> None:
        for name in _CONVENTION_FILES:
            path = root / name
            if not path.is_file():
                continue
            text = read_text_safe(path, limit=2000)
            if not text:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # 去掉原本的列表/引用标记，避免在 Memory 里渲染成「- - 内容」
                line = re.sub(r"^(?:[-*+•]|\d+[.)、])\s*", "", line).strip()
                if len(line) < 12:
                    continue
                state.constraints.append(_clip(line, 240))
                if len(state.constraints) >= 10:
                    break
            if state.constraints:
                state.notes.append(f"项目约定来源：{name}")
                break


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def default_os_label() -> str:
    return "Windows" if IS_WINDOWS else os.name
