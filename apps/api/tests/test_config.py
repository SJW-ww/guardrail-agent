from guardrail_api.config import Settings, get_settings


def test_cors_origins_are_split_and_trimmed() -> None:
    settings = Settings(cors_origins="http://a.com, http://b.com ,")

    assert settings.cors_origin_list == ["http://a.com", "http://b.com"]


def test_governance_defaults_match_design_doc() -> None:
    settings = Settings()

    assert settings.lease_seconds == 30
    assert settings.heartbeat_interval_seconds == 10
    assert settings.max_step_retries == 3
    assert settings.heartbeat_interval_seconds < settings.lease_seconds


def test_settings_are_cached() -> None:
    assert get_settings() is get_settings()
