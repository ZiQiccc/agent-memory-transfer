"""Cursor 探测。

Cursor 的数据位置（Windows）::

    %APPDATA%/Cursor/User/globalStorage/state.vscdb     会话与 AI 对话
    %APPDATA%/Cursor/User/workspaceStorage/*/state.vscdb 工作区状态

Cursor 作为 **Source** 可用（能读出对话），但作为 **Target** 需要慎重：
Cursor 没有官方的上下文注入入口，可行手段只有「项目规则文件 + 剪贴板/UI 自动化」，
属于技术架构 §36 里优先级最低的两档。因此本实现**如实标注
``target_supported=False``** —— 不提供一个假装能用的 Target Adapter。
"""

from __future__ import annotations

from pathlib import Path

from amt.context import AppContext
from amt.core.models import AgentInstallation, DetectionResult


class CursorDetector:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------
    def global_db(self) -> Path:
        return self.ctx.config.cursor.resolved_global_storage() / "state.vscdb"

    def workspace_dbs(self) -> list[Path]:
        root = self.ctx.config.cursor.resolved_workspace_storage()
        if not root.is_dir():
            return []
        return [p for p in root.glob("*/state.vscdb") if p.is_file()]

    def databases(self) -> list[Path]:
        out: list[Path] = []
        if self.global_db().is_file():
            out.append(self.global_db())
        out.extend(self.workspace_dbs())
        return out

    def executable(self) -> str | None:
        return self.ctx.process.find_executable("cursor")

    # ------------------------------------------------------------------
    def detect(self) -> DetectionResult:
        dbs = self.databases()
        exe = self.executable()
        evidence: list[str] = []
        if exe:
            evidence.append(f"可执行文件：{exe}")
        for db in dbs:
            evidence.append(f"数据库：{db}")

        installed = exe is not None or bool(dbs)
        runtime_available = False
        if dbs:
            try:
                from amt.adapters.cursor.parser import read_composers

                runtime_available = any(
                    read_composers(db) for db in dbs[:1]
                )
            except Exception:
                runtime_available = False

        if not installed:
            detail = "未检测到 Cursor（既无 cursor 可执行文件，也无 state.vscdb）"
        elif not dbs:
            detail = "已安装 Cursor，但未找到 state.vscdb"
        elif not runtime_available:
            detail = "找到 state.vscdb，但其中没有可读的 Composer 会话"
        else:
            detail = f"就绪：可读取 {len(dbs)} 个 Cursor 数据库中的 Composer 会话"

        return DetectionResult(
            agent="cursor",
            installed=installed,
            runtime_available=runtime_available,
            detail=detail,
            evidence=evidence,
        )

    def installation(self) -> AgentInstallation:
        detection = self.detect()
        notes = [
            "来源支持：读取 globalStorage/state.vscdb 的 composerData 与 bubbleId",
            "**不提供 Target**：Cursor 无官方上下文注入入口，只靠规则文件/UI 自动化不可靠，"
            "因此如实标注为不支持（见技术架构 §36 的注入优先级）",
        ]
        if not detection.runtime_available:
            notes.append("当前库中没有可读会话")
        return AgentInstallation(
            agent="cursor",
            display_name="Cursor",
            installed=detection.installed,
            runtime_available=detection.runtime_available,
            executable=self.executable(),
            data_dir=str(self.global_db()),
            source_supported=True,
            target_supported=False,
            notes=notes,
        )
