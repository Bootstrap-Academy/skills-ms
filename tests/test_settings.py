from pathlib import Path

from _pytest.monkeypatch import MonkeyPatch

from api.settings import Settings, settings


def test__environment_retains_case_insensitive_names_and_json_settings(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("Reload", "ON")
    monkeypatch.setenv("PORT", "8123")
    monkeypatch.setenv("LESSON_MODULE_ORIGINS", '["https://modules.example"]')
    monkeypatch.setenv("LEARNING_ROOMS_EXERCISE_REFS", '{"unit": {"type": "coding", "task_id": "task"}}')
    monkeypatch.setenv("PRIVATE_COURSES_DIRECTORY", str(tmp_path / "synthetic-courses"))
    monkeypatch.setenv("CHALLENGES_URL", "https://challenges.example")
    configured = Settings()
    assert configured.debug is False and configured.reload is True
    assert configured.port == 8123 and Settings(port=8124).port == 8124
    assert configured.lesson_module_origins == ["https://modules.example"]
    assert configured.learning_rooms_exercise_refs == {"unit": {"type": "coding", "task_id": "task"}}
    assert configured.private_courses_directory == tmp_path / "synthetic-courses"
    assert configured.challenges_url == "https://challenges.example"


def test__internal_jwt_secret__falls_back_to_the_shared_secret(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "shared")
    monkeypatch.setattr(settings, "internal_jwt_secret_auth", "")
    monkeypatch.setattr(settings, "internal_jwt_secret_shop", "")
    monkeypatch.setattr(settings, "internal_jwt_secret_skills", "")

    assert settings.internal_jwt_secret("auth") == "shared"
    assert settings.internal_jwt_secret("shop") == "shared"
    assert settings.internal_jwt_secret("skills") == "shared"
    assert settings.internal_jwt_secret("unknown") == "shared"


def test__internal_jwt_secret__per_audience(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "shared")
    monkeypatch.setattr(settings, "internal_jwt_secret_auth", "auth secret")
    monkeypatch.setattr(settings, "internal_jwt_secret_shop", "shop secret")
    monkeypatch.setattr(settings, "internal_jwt_secret_skills", "skills secret")

    assert settings.internal_jwt_secret("auth") == "auth secret"
    assert settings.internal_jwt_secret("shop") == "shop secret"
    assert settings.internal_jwt_secret("skills") == "skills secret"
    assert settings.internal_jwt_secret("unknown") == "shared"
