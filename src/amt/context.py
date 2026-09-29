"""应用上下文：一次性装配所有服务，供 Adapter / Orchestrator / CLI 共享。

这样 Adapter 只依赖「上下文 + 自己的 agent 私有格式」，不再各自初始化
进程管理、Git、安全扫描等横切能力。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from amt.config import AMTConfig, load_config
from amt.services.filesystem import ProjectStateCollector
from amt.services.git import GitService
from amt.services.process import ProcessManager
from amt.services.security import SecretScanner
from amt.services.shell import ShellResolver


@dataclass
class AppContext:
    config: AMTConfig
    process: ProcessManager = field(init=False)
    shell: ShellResolver = field(init=False)
    git: GitService = field(init=False)
    collector: ProjectStateCollector = field(init=False)
    scanner: SecretScanner = field(init=False)

    def __post_init__(self) -> None:
        self.shell = ShellResolver()
        self.process = ProcessManager(
            shell=self.shell,
            default_timeout=self.config.process.default_timeout,
        )
        self.git = GitService(
            manager=self.process,
            executable=self.config.git.executable,
            timeout=self.config.git.timeout,
        )
        self.collector = ProjectStateCollector(git=self.git, shell=self.shell)
        self.scanner = SecretScanner(self.config.security.redaction_mode)


def build_context(config: AMTConfig | None = None) -> AppContext:
    return AppContext(config=config or load_config())
