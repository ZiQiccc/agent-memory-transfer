"""交互式启动的**参数投递**策略（跨 Adapter 共用）。

为什么需要单独一个模块
---------------------
启动目标 Agent 时要把初始 Prompt 作为参数传进去。但 Windows 下
Codex / Claude 常以 ``.cmd`` 形式安装，``CreateProcess`` 无法直接执行 ``.cmd``，
必须经 ``cmd.exe /c`` 包装 —— 而 **只要经过 shell，参数就会被 shell 再解析一次**。

实测（``cmd.exe /c shim.cmd "<arg>"``，见 ``tests/test_launch.py`` 的锁定用例）：

| 参数内容 | 结果 |
| --- | --- |
| 多行文本 | **只送到第一行**，其余被当成独立命令 |
| ``%PATH%`` | 变量被展开，26 字变 1473 字 |
| ``&`` / ``|`` | 命令被切断 |
| ``<`` / ``>`` | 整行消失 |
| ``"`` | 参数边界被破坏 |

而初始 Prompt 恰好包含**换行、引号、可能的 ``%`` 与 ``&``**（目标文本来自用户
原话或 LLM 归纳），所以「原样塞进命令行」在 ``.cmd`` 目标上一定是**静默残缺**的。

策略
----
* 目标是**真实可执行文件**（``.exe`` 等，无需 shell）→ 参数不经过任何 shell，
  可以原样传递完整 Prompt。
* 目标是 **``.cmd`` / ``.bat``**（必须 shell 包装）→ 命令行只放一句
  **经过安全化处理的单行摘要**，完整 Prompt 另存为
  ``<memory_dir>/initial-prompt.md``，并在摘要里指路。

两条路径都保证：**完整 Prompt 一定能被目标 Agent 拿到**。
"""

from __future__ import annotations

from pathlib import Path

from amt.context import AppContext

_INITIAL_PROMPT_FILENAME = "initial-prompt.md"


def write_initial_prompt(project_root: Path, memory_dir: str, prompt: str) -> Path:
    """把完整初始 Prompt 落盘，供人工粘贴或目标 Agent 自行读取。"""
    target_dir = Path(project_root) / memory_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / _INITIAL_PROMPT_FILENAME
    path.write_text(prompt, encoding="utf-8")
    return path


def build_launch_command(
    ctx: AppContext,
    *,
    executable: str,
    prompt: str,
    project_root: Path,
) -> tuple[list[str], Path | None, list[str]]:
    """构造交互式启动的 argv，并保证完整 Prompt 可送达。

    返回 ``(argv, prompt_file_or_None, notes)``。
    ``notes`` 用于向用户解释「为什么命令行里放的是摘要」。
    """
    notes: list[str] = []

    if not ctx.shell.needs_shell_wrapper(executable):
        # 真实可执行文件：argv 由 CreateProcess/execve 直接传递，不经 shell 解析
        return ctx.shell.build_command(executable, [prompt]), None, notes

    prompt_file = write_initial_prompt(project_root, ctx.config.memory_dir, prompt)
    summary = _summary_prompt(ctx, prompt, prompt_file)
    notes.append(
        f"目标 CLI 是 {Path(executable).suffix} 包装脚本，必须经 cmd.exe 启动；"
        "cmd 会破坏多行/含 % & | < > 的参数，因此命令行只传单行摘要，"
        f"完整初始 Prompt 已写入 {prompt_file}"
    )
    return ctx.shell.build_command(executable, [summary]), prompt_file, notes


def _summary_prompt(ctx: AppContext, prompt: str, prompt_file: Path) -> str:
    """生成能安全穿过 cmd.exe 的单行摘要。

    模板是固定的（零风险），只有被安全化过的目标文本是变量 ——
    ``ShellResolver.sanitize_argument`` 会把 cmd 元字符换成同形全角字符。
    """
    goal = _extract_goal(prompt)
    parts = ["继续一个已经进行中的开发任务。"]
    if goal:
        parts.append(f"任务目标：{ctx.shell.sanitize_argument(goal, limit=300)}")
    parts.append(
        f"请先读取 {ctx.config.memory_dir}/{_INITIAL_PROMPT_FILENAME} 与"
        f" {ctx.config.memory_dir}/memory.md，理解当前状态后继续；"
        "不要重做已经完成的工作，不要重复已经失败的方案；"
        "真实状态以文件和 Git 为准。"
    )
    return " ".join(parts)


def _extract_goal(prompt: str) -> str:
    """从初始 Prompt 里取出「## 任务目标」一节，用于生成摘要。"""
    marker = "## 任务目标"
    index = prompt.find(marker)
    if index == -1:
        return ""
    rest = prompt[index + len(marker) :]
    stop = rest.find("\n## ")
    if stop != -1:
        rest = rest[:stop]
    return rest.strip()
