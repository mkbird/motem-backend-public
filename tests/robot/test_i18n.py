from robot.agent.tools import _context as ctx
from robot.i18n import get_locale, normalize_locale, set_locale, t


def test_normalize_locale_defaults_to_english():
    assert normalize_locale(None) == "en"
    assert normalize_locale("") == "en"
    assert normalize_locale("fr-FR,fr;q=0.9") == "en"


def test_normalize_locale_supported_languages():
    assert normalize_locale("en-US,en;q=0.9") == "en"
    assert normalize_locale("zh-CN,zh;q=0.9") == "zh-Hans"
    assert normalize_locale("zh-Hans") == "zh-Hans"


def test_context_locale_uses_normalized_default():
    set_locale(None)
    assert get_locale() == "en"

    token = ctx._current_user_lang.set("zh-Hans")
    try:
        assert t(ctx.get_user_lang(), "GAME_PROGRESS_LOADING") == "加载游戏..."
    finally:
        ctx._current_user_lang.reset(token)

    ctx.set_request_context("owner", "user", user_lang="")
    assert ctx.get_user_lang() == "en"
