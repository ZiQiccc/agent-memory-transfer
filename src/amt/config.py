"""全局配置。

优先级：显式传参 > 真实环境变量 > ``.env`` 文件 > 配置文件 > 内置默认值。
配置文件默认位于 ``~/.agent-memory-transfer/config/config.yaml``。

凭据（API Key）推荐放在 ``.env`` 里，顺序：

    ① ``AMT_ENV_FILE`` 指定的文件
    ② ``$AMT_HOME/.env``
    ③ 当前工作目录下的 ``.env``

``.env`` 里可以写 ``AMT_LLM_API_KEY`` / ``AMT_LLM_BASE_URL`` / ``AMT_LLM_MODEL``，
也接受更短的 ``api_key`` / ``base_url`` / ``model``（配合中转站时更顺手）。
**真实环境变量只认 ``AMT_LLM_*`` 前缀**，避免与系统里含义不明的 ``MODEL``、
``API_KEY`` 相撞。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal, get_args

import yaml
from pydantic import BaseModel, Field, PrivateAttr

DEFAULT_HOME_DIRNAME = ".agent-memory-transfer"
ENV_HOME = "AMT_HOME"
ENV_FILE = "AMT_ENV_FILE"
"""指定 ``.env`` 文件位置；不设置时按 AMT_HOME → cwd 依次查找。"""

#: ``.env`` 文件里可接受的键名别名（同一字段可写多种写法）。
ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "api_key": ("AMT_LLM_API_KEY", "LLM_API_KEY", "API_KEY"),
    "base_url": ("AMT_LLM_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE", "BASE_URL"),
    "model": ("AMT_LLM_MODEL", "OPENAI_MODEL", "MODEL"),
    "enabled": ("AMT_LLM_ENABLED", "LLM_ENABLED"),
    "timeout": ("AMT_LLM_TIMEOUT", "LLM_TIMEOUT"),
    "max_output_tokens": ("AMT_LLM_MAX_TOKENS", "MAX_OUTPUT_TOKENS"),
}
"""键名别名。**仅对 ``.env`` 生效**：环境变量侧只用 AMT_LLM_*，避免全局串味。"""

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}


def parse_env_file(path: Path) -> dict[str, str]:
    """解析一个 ``.env`` 文件。

    支持：``KEY=value``、``export KEY=value``、``#`` 注释、空行、
    以及单/双引号包裹的值（含引号内的 ``#`` 不算注释）。
    """
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            out[key] = value
    return out


def find_env_file(explicit: Path | str | None = None) -> Path | None:
    """按约定顺序找到第一个存在的 ``.env``。都不存在则返回 None。"""
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    env = os.environ.get(ENV_FILE)
    if env:
        p = Path(env).expanduser()
        return p if p.is_file() else None
    candidates = [default_home() / ".env", Path.cwd() / ".env"]
    for p in candidates:
        if p.is_file():
            return p
    return None


def read_llm_env(explicit: Path | str | None = None) -> tuple[dict[str, str], Path | None, dict[str, str]]:
    """汇总 LLM 相关设置，返回 ``(值, 来源文件, 字段来源)``。

    优先级：真实环境变量 > ``.env`` 文件。``.env`` 里后出现的行覆盖先出现的行。
    """
    values: dict[str, str] = {}
    origin: dict[str, str] = {}

    path = find_env_file(explicit)
    if path is not None:
        raw = parse_env_file(path)
        lower = {k.lower(): k for k in raw}
        for fieldname, aliases in ENV_ALIASES.items():
            for alias in aliases:
                actual = alias if alias in raw else lower.get(alias.lower())
                if actual is None:
                    continue
                value = raw[actual].strip()
                if value:
                    values[fieldname] = value
                    origin[fieldname] = f"{path}"
                    break

    # 真实环境变量优先级更高，且只认 AMT_LLM_* 前缀（取别名表里的第一个）
    for fieldname, aliases in ENV_ALIASES.items():
        name = aliases[0]
        value = (os.environ.get(name) or "").strip()
        if value:
            values[fieldname] = value
            origin[fieldname] = f"环境变量 {name}"

    return values, path, origin


class LLMConfig(BaseModel):
    """LLM Provider 配置。默认关闭——POC 必须能在无 LLM 环境下跑通。

    凭据通常在 ``.env`` 里（``AMT_LLM_API_KEY`` 等）。``.env`` 提供的
    ``api_key`` **只放在私有属性里**，不进 ``model_dump``，
    因此不会被 ``config --init`` 写进配置文件。
    """

    enabled: bool = False
    provider: Literal["openai_compatible"] = "openai_compatible"
    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = "qwen2.5-coder:7b"
    api_key: str | None = None
    api_key_env: str = "AMT_LLM_API_KEY"
    timeout: float = 120.0
    temperature: float = 0.0
    max_output_tokens: int = 4096

    #: 来自 ``.env`` / 环境变量的实际值（api_key 属敏感信息，绝不参与序列化）
    _env_api_key: str | None = PrivateAttr(default=None)
    #: 各字段来源说明，仅用于排查（如 "D:\\proj\\.env" / "环境变量 AMT_LLM_BASE_URL"）
    _origin: dict[str, str] = PrivateAttr(default_factory=dict)

    @property
    def origin(self) -> dict[str, str]:
        """字段来源映射，便于 `amt llm-check` 说明「这个值是谁给的」。"""
        return dict(self._origin)

    def apply_env(self, values: dict[str, str], origin: dict[str, str] | None = None) -> None:
        """把 ``.env`` / 环境变量里的设置应用进来。

        - ``base_url`` / ``model`` / ``enabled`` 直接写进字段：它们不是秘密，
          写进字段可让所有既有调用点自动生效，不必到处取「effective value」。
        - ``api_key`` 只进私有属性，序列化时不会外泄。
        """
        origin = origin or {}
        for name in ("base_url", "model"):
            value = (values.get(name) or "").strip()
            if value:
                setattr(self, name, value.rstrip("/") if name == "base_url" else value)
                self._origin[name] = origin.get(name, "env")

        key = (values.get("api_key") or "").strip()
        if key:
            self._env_api_key = key
            self._origin["api_key"] = origin.get("api_key", "env")

        raw_timeout = (values.get("timeout") or "").strip()
        if raw_timeout:
            try:
                self.timeout = float(raw_timeout)
                self._origin["timeout"] = origin.get("timeout", "env")
            except ValueError:
                pass

        raw_budget = (values.get("max_output_tokens") or "").strip()
        if raw_budget:
            try:
                self.max_output_tokens = max(256, int(raw_budget))
                self._origin["max_output_tokens"] = origin.get("max_output_tokens", "env")
            except ValueError:
                pass

        raw_enabled = (values.get("enabled") or "").strip().lower()
        if raw_enabled:
            self.enabled = raw_enabled in _TRUTHY
            self._origin["enabled"] = origin.get("enabled", "env")
        elif self._env_api_key and values.get("base_url") and values.get("model"):
            # .env 同时给了端点、模型与密钥 —— 这是明确的「请用 LLM」信号，
            # 自动启用，避免用户配好了却因为忘了 enabled 而静默走启发式。
            self.enabled = True
            self._origin["enabled"] = "由 .env 自动启用（base_url + model + api_key 齐全）"

    def resolve_api_key(self) -> str | None:
        """本地模型通常不需要 key，缺失不视为错误。"""
        return (
            self.api_key
            or self._env_api_key
            or os.environ.get(self.api_key_env)
            or None
        )

    def key_source(self) -> str:
        """API Key 的来源描述（绝不返回密钥本身）。"""
        if self.api_key:
            return "命令行 --llm-api-key"
        if self._env_api_key:
            return self._origin.get("api_key", ".env 文件")
        if os.environ.get(self.api_key_env):
            return f"环境变量 {self.api_key_env}"
        return "未设置"


class SecurityConfig(BaseModel):
    redaction_mode: Literal["strict", "balanced", "off"] = "balanced"
    scan_before_extract: bool = True


class CodexConfig(BaseModel):
    home: Path | None = None
    executable: str | None = None
    agents_file: str = "AGENTS.md"
    """Codex 的项目级指令文件。Codex **不支持 @ 导入**，因此 Context 需要内联写入。"""

    def resolved_home(self) -> Path:
        base = self.home or (Path.home() / ".codex")
        return Path(base).expanduser()

    def sessions_dir(self) -> Path:
        return self.resolved_home() / "sessions"


class ClaudeConfig(BaseModel):
    home: Path | None = None
    executable: str | None = None
    memory_dir: str = ".agent-transfer"
    context_file: str = "CLAUDE.md"
    launch: bool = True
    projects_dir: Path | None = None
    """会话目录。默认 ``~/.claude/projects``；Claude 在 WSL 里运行时需要指向 WSL 侧路径。"""

    def resolved_home(self) -> Path:
        base = self.home or (Path.home() / ".claude")
        return Path(base).expanduser()

    def resolved_projects_dir(self) -> Path:
        return Path(self.projects_dir).expanduser() if self.projects_dir else (self.resolved_home() / "projects")


class WorkBuddyConfig(BaseModel):
    """WorkBuddy 的会话数据位置。

    ``~/.workbuddy/projects/<escaped-workspace>/<session-uuid>.jsonl``

    注意：该目录属于 WorkBuddy 的内部数据区。本工具**只在用户显式指定
    workbuddy 作为来源时**读取它，不会主动扫描。
    """

    home: Path | None = None
    projects_dir: Path | None = None
    executable: str | None = None

    def resolved_home(self) -> Path:
        return Path(self.home).expanduser() if self.home else (Path.home() / ".workbuddy")

    def resolved_projects_dir(self) -> Path:
        if self.projects_dir:
            return Path(self.projects_dir).expanduser()
        return self.resolved_home() / "projects"


class CursorConfig(BaseModel):
    """Cursor 的会话数据存在 SQLite 里（VS Code 系的 state.vscdb）。"""

    global_storage: Path | None = None
    """默认 ``%APPDATA%/Cursor/User/globalStorage``（Windows）。"""

    workspace_storage: Path | None = None
    extraction_timeout: float = 10.0

    def resolved_global_storage(self) -> Path:
        if self.global_storage:
            return Path(self.global_storage).expanduser()
        return _default_cursor_dir("globalStorage")

    def resolved_workspace_storage(self) -> Path:
        if self.workspace_storage:
            return Path(self.workspace_storage).expanduser()
        return _default_cursor_dir("workspaceStorage")


def _default_cursor_dir(kind: str) -> Path:
    """按平台给出 Cursor 的 User 目录。找不到时返回一个明显不存在的路径。"""
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Cursor" / "User" / kind
    home = Path.home()
    if os.name == "nt":
        return home / "AppData" / "Roaming" / "Cursor" / "User" / kind
    if os.uname().sysname == "Darwin":  # type: ignore[attr-defined]
        return home / "Library" / "Application Support" / "Cursor" / "User" / kind
    return home / ".config" / "Cursor" / "User" / kind


class GitConfig(BaseModel):
    executable: str | None = None
    timeout: float = 20.0


class ProcessConfig(BaseModel):
    default_timeout: float = 60.0
    detect_timeout: float = 15.0


class AMTConfig(BaseModel):
    home_dir: Path = Field(default_factory=lambda: Path.home() / DEFAULT_HOME_DIRNAME)
    memory_dir: str = ".agent-transfer"
    """任务包在**目标项目**里的存放目录。所有 Target Adapter 共用同一位置，
    因此在不同 Agent 之间来回迁移时复用的是同一个任务包。"""

    config_error: str | None = None
    """配置文件加载失败的原因（若有）。

    为什么把它放进配置对象：早期实现遇到损坏的 YAML 会**静默退回默认值**，
    表现为「用户设了 projects_dir 却不生效，且没有任何提示」——
    排查成本极高。现在任何命令都会在最开始把这条错误显示出来。
    """

    _env_file: Path | None = PrivateAttr(default=None)
    """本次生效的 ``.env`` 文件（若找到）。私有属性，不会被写回配置文件。"""

    @property
    def env_file(self) -> Path | None:
        return self._env_file

    llm: LLMConfig = Field(default_factory=LLMConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    codex: CodexConfig = Field(default_factory=CodexConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    cursor: CursorConfig = Field(default_factory=CursorConfig)
    workbuddy: WorkBuddyConfig = Field(default_factory=WorkBuddyConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    process: ProcessConfig = Field(default_factory=ProcessConfig)

    # ---- 目录 ----
    @property
    def config_dir(self) -> Path:
        return self.home_dir / "config"

    @property
    def sessions_dir(self) -> Path:
        return self.home_dir / "sessions"

    @property
    def memories_dir(self) -> Path:
        return self.home_dir / "memories"

    @property
    def migrations_dir(self) -> Path:
        return self.home_dir / "migrations"

    @property
    def logs_dir(self) -> Path:
        return self.home_dir / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.home_dir / "cache"

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.yaml"

    def ensure_dirs(self) -> None:
        for d in (
            self.home_dir,
            self.config_dir,
            self.sessions_dir,
            self.memories_dir,
            self.migrations_dir,
            self.logs_dir,
            self.cache_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


def default_home() -> Path:
    env = os.environ.get(ENV_HOME)
    if env:
        return Path(env).expanduser()
    return Path.home() / DEFAULT_HOME_DIRNAME


def _unwrap_optional(annotation: Any) -> Any:
    """``WorkBuddyConfig | None`` → ``WorkBuddyConfig``。"""
    args = get_args(annotation)
    for arg in args:
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
    return annotation


def _unknown_config_keys(data: dict[str, Any], model: type[BaseModel], prefix: str = "") -> list[str]:
    """找出配置文件里**不认识**的键（含一层嵌套）。

    为什么要检查：Pydantic 默认忽略未知字段，于是 ``workbuddy: {(拼错的)projectsdir: ...}``
    会被无声丢弃 —— 用户以为设置生效了，实际跑的是默认路径。
    这类拼写错误必须报出来。
    """
    unknown: list[str] = []
    fields = model.model_fields
    for key, value in data.items():
        if key not in fields:
            unknown.append(f"{prefix}{key}")
            continue
        if isinstance(value, dict):
            nested = _unwrap_optional(fields[key].annotation)
            if isinstance(nested, type) and issubclass(nested, BaseModel):
                unknown.extend(_unknown_config_keys(value, nested, prefix=f"{prefix}{key}."))
    return sorted(unknown)


def load_config(path: Path | str | None = None, env_file: Path | str | None = None) -> AMTConfig:
    """读取配置。配置文件不存在时静默使用默认值。

    **配置文件存在但读不动时必须报错**（写进 ``config_error``）：静默退回默认值会
    让用户以为设置生效了，实际却指向了别处 —— 这类「无声失败」排查成本极高。
    覆盖两种情况：YAML 语法/类型错误，以及**无法识别的配置项**（多为拼写错误）。

    ``.env``（凭据）按 ``AMT_ENV_FILE`` → ``$AMT_HOME/.env`` → ``cwd/.env`` 顺序查找，
    其优先级高于配置文件、低于真实环境变量。
    """
    home = default_home()
    target = Path(path).expanduser() if path else (home / "config" / "config.yaml")
    data: dict[str, Any] = {}
    problems: list[str] = []

    if target.is_file():
        try:
            loaded = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                data = loaded
            else:
                problems.append(f"内容不是映射结构（{type(loaded).__name__}）")
        except Exception as exc:
            problems.append(f"YAML 解析失败：{exc}")

    if data:
        unknown = _unknown_config_keys(data, AMTConfig)
        if unknown:
            problems.append(
                "存在无法识别的配置项（会被忽略，多为拼写错误）：" + "、".join(unknown)
            )

    data.setdefault("home_dir", str(home))
    try:
        cfg = AMTConfig.model_validate(data)
    except Exception as exc:
        cfg = AMTConfig(home_dir=home)
        problems.append(f"字段校验失败：{exc}")

    # .env / 环境变量：在配置文件之上生效，但不覆盖命令行显式传参（后者在本函数之后应用）
    values, found_env, origin = read_llm_env(env_file)
    if found_env is not None:
        cfg._env_file = found_env
    if values:
        try:
            cfg.llm.apply_env(values, origin)
        except Exception as exc:  # 防御：凭据异常不应让整条命令失败
            problems.append(f".env 应用失败：{exc}")

    if problems:
        cfg.config_error = "；".join(problems) + f"（配置文件：{target}）"
    return cfg


def write_default_config(cfg: AMTConfig) -> Path:
    """写出**默认配置模板**。

    刻意基于全默认值生成，而不是把当前运行配置 dump 出去：
    否则 ``.env`` 里的中转站地址、乃至任何运行时覆盖都会被固化进配置文件，
    既可能覆盖用户手写的配置，也容易把不该落盘的信息写进去。
    """
    template = AMTConfig(home_dir=cfg.home_dir)
    template.ensure_dirs()
    if template.config_file.is_file():
        return template.config_file
    payload = template.model_dump(mode="json")
    template.config_file.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return template.config_file
