"""Memory Validator（技术架构 §23 / 实现plan §十三）。

LLM 输出之后必须做**确定性校验**，不能直接信任模型：

    Pydantic              结构与类型（由模型层保证）
    → Required Field Check 必填字段非空
    → Reference Check      Memory 声称修改的文件必须在项目中真实存在
    → Project Path Check   项目路径可达
    → Git State Check      Memory 的 Git 描述与真实 Git 状态一致（真实状态优先）
    → Consistency Check    已完成工作不得同时出现在待办中

校验结果只产生 error / warning，**不自动删除内容**——由人工决定取舍。
"""

from __future__ import annotations

from pathlib import Path

from amt.core.models import CanonicalMemory, ValidationReport
from amt.services.filesystem import ProjectState


class MemoryValidator:
    def validate(self, memory: CanonicalMemory, project: ProjectState) -> ValidationReport:
        errors: list[str] = []
        warnings: list[str] = []
        checks: list[str] = []

        self._required_fields(memory, errors, checks)
        self._project_path(memory, project, errors, warnings, checks)
        self._file_references(memory, project, warnings, checks)
        self._git_consistency(memory, project, warnings, checks)
        self._attempt_completeness(memory, warnings, checks)
        self._action_consistency(memory, warnings, checks)

        return ValidationReport(errors=errors, warnings=warnings, checks=checks)

    # ------------------------------------------------------------------
    @staticmethod
    def _required_fields(memory: CanonicalMemory, errors: list[str], checks: list[str]) -> None:
        checks.append("必填字段：task.title / task.goal / metadata")
        if not memory.task.title.strip():
            errors.append("task.title 为空：无法让目标 Agent 判断这是什么任务")
        if not memory.task.goal.strip() or memory.task.goal == "unknown":
            errors.append("task.goal 为空或 unknown：目标 Agent 无法知道要达成什么")
        if not memory.metadata.memory_id.strip():
            errors.append("metadata.memory_id 为空")
        if not memory.metadata.source_agent.strip():
            errors.append("metadata.source_agent 为空")

    @staticmethod
    def _project_path(
        memory: CanonicalMemory,
        project: ProjectState,
        errors: list[str],
        warnings: list[str],
        checks: list[str],
    ) -> None:
        checks.append("项目路径可达性")
        path = memory.project.path or project.cwd
        if not path:
            errors.append("project.path 为空：无法核对真实项目状态")
            return
        if not Path(path).is_dir():
            warnings.append(f"项目路径不存在：{path}（Memory 仍可用，但无法核对代码状态）")

    @staticmethod
    def _file_references(
        memory: CanonicalMemory,
        project: ProjectState,
        warnings: list[str],
        checks: list[str],
    ) -> None:
        checks.append("引用检查：modified_files 是否真实存在")
        root = Path(memory.project.path or project.cwd or "")
        if not root or not root.is_dir():
            warnings.append("项目路径不可用，跳过文件引用检查")
            return

        missing: list[str] = []
        for file in memory.implementation.modified_files:
            if file.status == "deleted":
                continue
            if _resolve(file.path, root) is None:
                missing.append(file.path)

        if missing:
            preview = "、".join(missing[:5])
            more = f" 等 {len(missing)} 个" if len(missing) > 5 else ""
            warnings.append(
                f"Memory 声明修改了以下文件，但项目中未找到：{preview}{more}"
                "（可能已删除、被重命名，或路径为会话内的临时路径）"
            )

    @staticmethod
    def _git_consistency(
        memory: CanonicalMemory,
        project: ProjectState,
        warnings: list[str],
        checks: list[str],
    ) -> None:
        checks.append("Git 状态检查：Memory 描述 vs 真实 Git")
        if not project.is_git_repo:
            warnings.append("当前目录不是 Git 仓库，无法校验 Git 状态")
            return

        real_changes = {p.replace("\\", "/") for p in project.tracked_changes}
        claimed = {p.replace("\\", "/") for p in memory.git.changed_files}
        if real_changes and claimed and not (real_changes & claimed):
            warnings.append(
                "Memory 记录的改动文件与当前 Git 工作区不一致（真实文件优先）："
                f"真实 {sorted(real_changes)[:4]}，Memory {sorted(claimed)[:4]}"
            )
        if real_changes and not claimed:
            warnings.append(
                f"Git 工作区有 {len(real_changes)} 个未提交改动，但 Memory 未记录任何改动文件"
            )

    @staticmethod
    def _attempt_completeness(memory: CanonicalMemory, warnings: list[str], checks: list[str]) -> None:
        checks.append("失败尝试完整性：result 非空")
        empty = [a.action for a in memory.failed_attempts() if not a.result.strip()]
        if empty:
            warnings.append(f"{len(empty)} 条失败尝试缺少 result 描述：{'、'.join(empty[:3])}")

    @staticmethod
    def _action_consistency(memory: CanonicalMemory, warnings: list[str], checks: list[str]) -> None:
        checks.append("一致性检查：已完成工作不得重复出现在 next_actions")
        completed = " ".join(memory.implementation.completed).lower()
        if not completed:
            return
        duplicated = [
            action.action
            for action in memory.next_actions
            if action.action.strip() and action.action.strip().lower()[:16] in completed
        ]
        if duplicated:
            warnings.append(f"以下待办与已完成工作重复：{'、'.join(duplicated[:3])}")


def _resolve(path_text: str, root: Path) -> Path | None:
    """在项目根内解析文件引用（兼容绝对 / 相对 / 反斜杠路径）。"""
    if not path_text:
        return None
    cleaned = path_text.strip().strip('"').replace("\\", "/")

    candidates: list[Path] = []
    raw = Path(cleaned)
    if raw.is_absolute():
        candidates.append(raw)
    candidates.append(root / cleaned)
    # 允许 Memory 里写的是项目内的相对片段（如 src/service/LoginService.java）
    parts = [p for p in cleaned.split("/") if p and p not in (".", "..")]
    if len(parts) > 1:
        candidates.append(root.joinpath(*parts))

    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate
        except OSError:
            continue
    return None
