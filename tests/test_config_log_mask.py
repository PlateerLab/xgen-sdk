"""설정 업데이트 로그에 비밀값 원문이 남지 않는다.

update_config_by_name · set_config 은 바꾼 값을 그대로 INFO/DEBUG 로그에 남겼다. API 키를
관리자 화면이나 서비스 API 로 바꾸면 키 원문이 파드 로그와 로그 수집기에 평문으로 남는다.
부팅 로그(base_config)에 이미 쓰던 마스킹을 업데이트 로그에도 쓴다.
"""
import logging

from xgen_sdk.config.local_config import LocalConfigManager
from xgen_sdk.config.log_mask import _mask_config_value

SECRET = "sk-ant-test-0123456789"


def test_mask_rule():
    assert _mask_config_value("ANTHROPIC_API_KEY_TEAM", SECRET) == f"<redacted len={len(SECRET)}>"
    assert _mask_config_value("anthropic.api_key", SECRET).startswith("<redacted")
    assert _mask_config_value("ANTHROPIC_MODEL_DEFAULT", "claude-sonnet-4-5") == "claude-sonnet-4-5"


def test_base_config_reexports_same_function():
    from xgen_sdk.config import base_config

    assert base_config._mask_config_value is _mask_config_value


def test_local_update_log_hides_secret(caplog, tmp_path):
    mgr = LocalConfigManager(persist_path=str(tmp_path / "cfg.json"))
    mgr.set_config(config_path="anthropic.api_key", config_value="old-value-123",
                   data_type="string", category="anthropic", env_name="ANTHROPIC_API_KEY")
    caplog.set_level(logging.DEBUG)
    mgr.set_config(config_path="anthropic.api_key", config_value=SECRET,
                   data_type="string", category="anthropic", env_name="ANTHROPIC_API_KEY")
    mgr.update_config_by_name("ANTHROPIC_API_KEY", SECRET)
    assert "Config 업데이트 완료" in caplog.text  # 로그 자리를 실제로 지났다
    assert SECRET not in caplog.text
