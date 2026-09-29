"""安全：敏感信息扫描与脱敏（需求文档 §25）。"""

from __future__ import annotations

from amt.services.security import SecretScanner

OPENAI_KEY = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
BEARER = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payloadpart.signaturepart"


def test_structured_key_keeps_type_prefix_only():
    scanner = SecretScanner("balanced")
    redacted, findings = scanner.redact(f"openai_key = {OPENAI_KEY}")
    assert OPENAI_KEY not in redacted
    assert "sk-" in redacted
    assert "abcdefghij" not in redacted
    assert any(f.kind == "openai_api_key" for f in findings)


def test_password_value_is_fully_masked():
    """密码值没有任何可公开部分，必须全掩码。"""
    scanner = SecretScanner("balanced")
    redacted, _ = scanner.redact("password=hunter2superSecret")
    assert "hunter2superSecret" not in redacted
    assert "hun" not in redacted
    assert "password=****" in redacted


def test_same_span_is_not_reported_twice():
    """同一片段被多条规则命中时只上报一次（去重叠）。"""
    scanner = SecretScanner("balanced")
    _, findings = scanner.redact("db_password=SuperSecret123")
    assert len(findings) == 1


def test_bearer_token_masked_including_prefix_word():
    scanner = SecretScanner("balanced")
    redacted, _ = scanner.redact(f"Authorization: {BEARER}")
    assert "payloadpart" not in redacted
    assert "Bearer ****" in redacted


def test_aws_key_masked():
    scanner = SecretScanner("balanced")
    redacted, findings = scanner.redact(f"AKIA... {AWS_KEY}")
    assert AWS_KEY not in redacted
    assert any(f.kind == "aws_access_key" for f in findings)


def test_jdbc_password_only_value_masked():
    scanner = SecretScanner("balanced")
    url = "jdbc:oracle:thin:@10.0.0.1:1521/ORCL?user=app&password=TopSecret123"
    redacted, _ = scanner.redact(url)
    assert "TopSecret123" not in redacted
    assert "jdbc:oracle:thin:@10.0.0.1:1521/ORCL" in redacted


def test_off_mode_does_not_touch_text():
    scanner = SecretScanner("off")
    redacted, findings = scanner.redact(f"key={OPENAI_KEY}")
    assert redacted == f"key={OPENAI_KEY}"
    assert findings == []


def test_strict_mode_redacts_medium_severity_too():
    balanced = SecretScanner("balanced")
    strict = SecretScanner("strict")
    text = "token: abcdefghijklmnop"
    _, balanced_findings = balanced.redact(text)
    _, strict_findings = strict.redact(text)
    assert len(strict_findings) >= len(balanced_findings)


def test_redact_structure_walks_nested_payloads():
    scanner = SecretScanner("balanced")
    payload = {
        "env": {"OPENAI_API_KEY": OPENAI_KEY},
        "steps": [{"cmd": f"curl -H 'Authorization: {BEARER}'"}],
    }
    redacted, findings = scanner.redact_structure(payload)
    assert OPENAI_KEY not in str(redacted)
    assert "payloadpart" not in str(redacted)
    assert len(findings) >= 2


def test_normal_source_code_is_not_over_redacted():
    """普通源码不应被误判成敏感信息。"""
    scanner = SecretScanner("balanced")
    code = (
        "public class LoginService {\n"
        "  private static final String TOKEN_TYPE = \"Bearer\";\n"
        "  public void login(String username) { /* 处理登录 */ }\n"
        "}\n"
    )
    redacted, findings = scanner.redact(code)
    assert redacted == code
    assert findings == []
