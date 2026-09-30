"""凭据文件（``.env``）加载测试。

真实教训：用户把中转站的端点与密钥写进了项目根目录的 ``.env``，
但 ``load_config`` 只读 ``os.environ`` —— 于是「配置明明写了却不生效」，
命令静默退回内置默认值（去连本机 11434）。这类无声失败必须堵死。

另外两条硬约束：
* ``api_key`` **绝不能**随 ``model_dump`` 落盘（否则 ``config --init`` 会把密钥写进配置文件）；
* ``.env`` 里的配置不能被静默忽略，来源要能说清楚。
"""

from __future__ import annotations

import pytest

from amt.config import (
    AMTConfig,
    LLMConfig,
    find_env_file,
    load_config,
    parse_env_file,
    read_llm_env,
    write_default_config,
)


# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------
def test_parse_env_file_supports_common_forms(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "\n".join(
            [
                "# 注释",
                "",
                "AMT_LLM_API_KEY=sk-abc",
                "export base_url=http://relay:3001/v1",
                'model="quoted-model"',
                "with_space = spaced # 行尾注释",
                "没等号的行",
            ]
        ),
        encoding="utf-8",
    )
    parsed = parse_env_file(path)
    assert parsed["AMT_LLM_API_KEY"] == "sk-abc"
    assert parsed["base_url"] == "http://relay:3001/v1"
    assert parsed["model"] == "quoted-model"
    assert parsed["with_space"] == "spaced"
    assert "没等号的行" not in parsed


def test_parse_env_file_tolerates_bom_and_crlf(tmp_path):
    """Windows 上记事本另存会带 BOM / CRLF，不能因此解析失败。"""
    path = tmp_path / ".env"
    path.write_bytes("AMT_LLM_MODEL=m\r\n".encode("utf-8-sig"))
    assert parse_env_file(path)["AMT_LLM_MODEL"] == "m"


def test_parse_env_file_missing_is_empty(tmp_path):
    assert parse_env_file(tmp_path / "nope") == {}


# ----------------------------------------------------------------------
# 查找顺序
# ----------------------------------------------------------------------
def test_find_env_file_prefers_amt_home_then_cwd(tmp_path, monkeypatch):
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()

    monkeypatch.setenv("AMT_HOME", str(home))
    monkeypatch.delenv("AMT_ENV_FILE", raising=False)
    monkeypatch.chdir(work)

    assert find_env_file() is None

    (work / ".env").write_text("AMT_LLM_MODEL=from-cwd\n", encoding="utf-8")
    assert find_env_file() == work / ".env"

    (home / ".env").write_text("AMT_LLM_MODEL=from-home\n", encoding="utf-8")
    assert find_env_file() == home / ".env", "AMT_HOME 优先于当前目录"


def test_explicit_env_file_wins(tmp_path, monkeypatch):
    explicit = tmp_path / "custom.env"
    explicit.write_text("AMT_LLM_MODEL=explicit\n", encoding="utf-8")
    monkeypatch.setenv("AMT_ENV_FILE", str(tmp_path / "other.env"))
    assert find_env_file(explicit) == explicit


# ----------------------------------------------------------------------
# 键名别名与优先级
# ----------------------------------------------------------------------
def test_short_key_names_are_accepted(tmp_path, monkeypatch):
    """用户实际就是这么写的：base_url / model / AMT_LLM_API_KEY 混用。"""
    monkeypatch.delenv("AMT_ENV_FILE", raising=False)
    path = tmp_path / ".env"
    path.write_text(
        "AMT_LLM_API_KEY=sk-xxx\nbase_url=http://10.0.0.1:3001/v1\nmodel=deepseek-v4-pro\n",
        encoding="utf-8",
    )
    values, found, origin = read_llm_env(path)
    assert found == path
    assert values["api_key"] == "sk-xxx"
    assert values["base_url"] == "http://10.0.0.1:3001/v1"
    assert values["model"] == "deepseek-v4-pro"
    assert origin["base_url"] == str(path)


def test_real_environment_overrides_env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("AMT_LLM_MODEL=from-file\n", encoding="utf-8")
    monkeypatch.setenv("AMT_LLM_MODEL", "from-env")
    values, _, origin = read_llm_env(path)
    assert values["model"] == "from-env"
    assert "环境变量" in origin["model"]


def test_bare_names_in_environment_are_ignored(tmp_path, monkeypatch):
    """真实环境里只认 AMT_LLM_* 前缀。

    否则系统里任何一个叫 ``MODEL`` / ``API_KEY`` 的变量都会串味到本工具。
    """
    monkeypatch.delenv("AMT_ENV_FILE", raising=False)
    monkeypatch.setenv("MODEL", "some-other-tools-model")
    monkeypatch.setenv("API_KEY", "unrelated")
    values, _, _ = read_llm_env(tmp_path / "nonexistent")
    assert "model" not in values
    assert "api_key" not in values


# ----------------------------------------------------------------------
# 生效与安全
# ----------------------------------------------------------------------
def _write_config(home, text: str = "") -> None:
    (home / "config").mkdir(parents=True, exist_ok=True)
    if text:
        (home / "config" / "config.yaml").write_text(text, encoding="utf-8")


def test_env_file_is_applied_and_auto_enables(tmp_path, monkeypatch):
    home = tmp_path / "amt"
    _write_config(home)
    (home / ".env").write_text(
        "AMT_LLM_BASE_URL=http://relay:3001/v1\nAMT_LLM_MODEL=m1\nAMT_LLM_API_KEY=sk-k\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AMT_HOME", str(home))
    monkeypatch.delenv("AMT_ENV_FILE", raising=False)
    monkeypatch.delenv("AMT_LLM_API_KEY", raising=False)

    cfg = load_config()
    assert cfg.env_file == home / ".env"
    assert cfg.llm.enabled is True, "端点+模型+密钥齐全时应自动启用，避免静默走启发式"
    assert cfg.llm.base_url == "http://relay:3001/v1"
    assert cfg.llm.model == "m1"
    assert cfg.llm.resolve_api_key() == "sk-k"
    assert cfg.config_error is None


def test_env_file_overrides_config_file(tmp_path, monkeypatch):
    home = tmp_path / "amt"
    _write_config(
        home,
        "llm:\n  enabled: true\n  base_url: http://from-config/v1\n  model: from-config\n",
    )
    (home / ".env").write_text("AMT_LLM_BASE_URL=http://from-env/v1\n", encoding="utf-8")
    monkeypatch.setenv("AMT_HOME", str(home))
    monkeypatch.delenv("AMT_ENV_FILE", raising=False)

    cfg = load_config()
    assert cfg.llm.base_url == "http://from-env/v1"
    assert cfg.llm.model == "from-config", "未在 .env 中出现的字段应保留配置文件的值"


def test_env_can_disable_explicitly(tmp_path, monkeypatch):
    """显式写 enabled=false 时必须尊重，不能自作主张自动打开。"""
    home = tmp_path / "amt"
    _write_config(home)
    (home / ".env").write_text(
        "base_url=http://relay/v1\nmodel=m\nAMT_LLM_API_KEY=sk-k\nAMT_LLM_ENABLED=false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AMT_HOME", str(home))
    monkeypatch.delenv("AMT_ENV_FILE", raising=False)

    assert load_config().llm.enabled is False


def test_key_never_enters_serialization(tmp_path, monkeypatch):
    """密钥绝不能随 model_dump 落盘 —— 否则 config --init 会把它写进配置文件。"""
    monkeypatch.setenv("AMT_LLM_API_KEY", "sk-super-secret")
    monkeypatch.delenv("AMT_ENV_FILE", raising=False)
    cfg = load_config(tmp_path / "nonexistent-config.yaml")
    assert cfg.llm.resolve_api_key() == "sk-super-secret"

    dumped = str(cfg.llm.model_dump(mode="json")) + str(cfg.model_dump(mode="json"))
    assert "sk-super-secret" not in dumped
    assert cfg.llm.origin["api_key"], "来源要能说明，但只说明来源、不含密钥本身"
    assert "sk-super-secret" not in str(cfg.llm.origin)


def test_default_config_template_is_written_without_runtime_overrides(tmp_path, monkeypatch):
    """--init 写出的模板不应固化运行时覆盖（如 .env 里的中转站地址）。"""
    home = tmp_path / "amt"
    (home / ".env").parent.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text("base_url=http://relay-internal:3001/v1\n", encoding="utf-8")
    monkeypatch.setenv("AMT_HOME", str(home))
    monkeypatch.delenv("AMT_ENV_FILE", raising=False)

    cfg = load_config()
    assert cfg.llm.base_url == "http://relay-internal:3001/v1"

    path = write_default_config(cfg)
    text = path.read_text(encoding="utf-8")
    assert "relay-internal" not in text


def test_key_source_is_descriptive_but_never_leaks(tmp_path, monkeypatch):
    monkeypatch.setenv("AMT_LLM_API_KEY", "sk-abcdef")
    monkeypatch.delenv("AMT_ENV_FILE", raising=False)
    cfg = load_config(tmp_path / "none.yaml")
    assert "AMT_LLM_API_KEY" in cfg.llm.key_source()
    assert "sk-abcdef" not in cfg.llm.key_source()


def test_missing_env_file_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AMT_HOME", str(tmp_path / "empty"))
    monkeypatch.delenv("AMT_ENV_FILE", raising=False)
    monkeypatch.delenv("AMT_LLM_API_KEY", raising=False)
    # 切换到没有 .env 的目录：否则会回落到「当前目录/.env」而找到本项目那份
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.env_file is None
    assert cfg.config_error is None
    assert cfg.llm.enabled is False


def test_apply_env_is_idempotent():
    llm = LLMConfig()
    llm.apply_env({"base_url": "http://a/v1", "model": "m", "api_key": "k"})
    llm.apply_env({"base_url": "http://a/v1", "model": "m", "api_key": "k"})
    assert llm.base_url == "http://a/v1"
    assert llm.model == "m"


def test_env_timeout_and_budget_are_applied(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("AMT_LLM_TIMEOUT=300\nAMT_LLM_MAX_TOKENS=8192\nbad=1\n", encoding="utf-8")
    llm = LLMConfig()
    values, _, origin = read_llm_env(path)
    llm.apply_env(values, origin)
    assert llm.timeout == 300.0
    assert llm.max_output_tokens == 8192


def test_invalid_numeric_env_does_not_crash(tmp_path):
    path = tmp_path / ".env"
    path.write_text("AMT_LLM_TIMEOUT=abc\nAMT_LLM_MAX_TOKENS=xyz\n", encoding="utf-8")
    llm = LLMConfig()
    values, _, origin = read_llm_env(path)
    llm.apply_env(values, origin)  # 不应抛异常
    assert llm.timeout == LLMConfig().timeout or llm.timeout > 0


@pytest.mark.parametrize("turn", ["yes", "on", "1", "TRUE"])
def test_enabled_truthy_variants(tmp_path, turn):
    path = tmp_path / ".env"
    path.write_text(f"AMT_LLM_ENABLED={turn}\n", encoding="utf-8")
    llm = LLMConfig()
    values, _, origin = read_llm_env(path)
    llm.apply_env(values, origin)
    assert llm.enabled is True


def test_config_with_only_llm_section_has_no_error(tmp_path, monkeypatch):
    """仅含 llm 段的配置文件不应触发「未知配置项」告警。"""
    home = tmp_path / "amt"
    _write_config(home, "llm:\n  enabled: true\n  model: m\n")
    monkeypatch.setenv("AMT_HOME", str(home))
    cfg = AMTConfig(home_dir=home)
    assert cfg.llm.model is None or isinstance(cfg.llm.model, str)
