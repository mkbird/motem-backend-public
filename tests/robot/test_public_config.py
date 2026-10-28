"""Production signing-key checks cannot be disabled by a CI environment flag."""
import pytest
from pydantic import ValidationError

from robot.config import Settings


@pytest.mark.parametrize("ci", ["", "true"])
@pytest.mark.parametrize("weak_key", ["", "short", "change-me-in-production"])
def test_production_rejects_weak_key_even_in_ci(monkeypatch, ci, weak_key):
    monkeypatch.setenv("CI", ci)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, debug=False, jwt_secret_key=weak_key)


def test_explicit_strong_signer_is_accepted(monkeypatch):
    monkeypatch.setenv("CI", "true")
    config = Settings(_env_file=None, debug=False, jwt_secret_key="test-only-" + "f" * 64)
    assert len(config.jwt_secret_key) >= 32
