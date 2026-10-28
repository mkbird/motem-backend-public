"""LLM-driven title translator.

Game / widget rename 端点用这个 helper 保证 `title_i18n` 始终是双语：
用户在 publish sheet 改了名字（只输入一种语言），后端用同一个 LLM client
（dashscope / OpenAI 兼容）把另一种语言补齐。

Failure mode：LLM 不可用或调用失败时，两个 slot 都回填原文，DB 不会
存进半截 / null 状态。短 title 长度有限制（en ≤ 40 / zh ≤ 15）防止
LLM 失控输出整段解释。
"""

import logging
import re
from typing import Literal

logger = logging.getLogger(__name__)

# CJK Unified Ideographs (basic + extension A) — 覆盖游戏 / widget 标题用得到
# 的所有简体中文字符，足够做轻量语言检测。日文 / 韩文也会落到 zh 分支，
# 但当前产品只支持 en / zh-Hans 两种语言，把它们当 zh 处理交给 LLM 翻成英文，
# 没坏 case。
_CJK_RE = re.compile(r"[㐀-鿿]")


def detect_title_language(text: str) -> Literal["en", "zh-Hans"]:
    """检测短 title 的源语言。含 CJK 字符 → zh-Hans，否则 en。"""
    return "zh-Hans" if _CJK_RE.search(text) else "en"


async def translate_title(
    text: str, source_lang: str | None = None
) -> dict[str, str]:
    """补齐 title 双语映射。

    Args:
        text: 用户输入的标题（任意一种语言）
        source_lang: 显式指定源语言（"en" / "zh-Hans"）；None 时自动检测

    Returns:
        `{"en": ..., "zh-Hans": ...}`。LLM 失败时两个 slot 都回填原文。
    """
    text = (text or "").strip()
    if not text:
        return {"en": "", "zh-Hans": ""}

    if source_lang not in ("en", "zh-Hans"):
        source_lang = detect_title_language(text)

    target_lang = "en" if source_lang == "zh-Hans" else "zh-Hans"
    out: dict[str, str] = {source_lang: text, target_lang: text}

    from robot.config import settings
    if not settings.dashscope_api_key:
        # 无 LLM 配置（如离线测试环境）—— 直接返回 source-as-both，
        # 调用方拿到的仍是合法 bilingual dict。
        return out

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(
            api_key=settings.dashscope_api_key,
            base_url=settings.openai_base_url,
        )
        if target_lang == "en":
            sys_prompt = (
                "Translate the given short game/widget title from Simplified "
                "Chinese to English. Reply with ONLY the translated title — "
                "1-3 words, no punctuation, no quotes, no explanation."
            )
        else:
            sys_prompt = (
                "Translate the given short game/widget title from English to "
                "Simplified Chinese. Reply with ONLY the translated title — "
                "2-5 Chinese characters, no punctuation, no quotes, no "
                "explanation."
            )
        resp = await client.chat.completions.create(
            model=settings.default_model,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": text},
            ],
            max_tokens=40,
            temperature=0.3,
        )
        translated = (resp.choices[0].message.content or "").strip()
        # 剥掉常见包装符号（双引号、单引号、中文引号）
        translated = translated.strip('"\'“”‘’')
        # 与 _extract_game_meta 的字段长度约束保持一致
        if target_lang == "en":
            translated = translated[:40]
        else:
            translated = translated[:15]
        if translated:
            out[target_lang] = translated
    except Exception as e:
        logger.warning(
            "[title_translator] translate failed (%s); "
            "falling back to source-as-both", e,
        )

    return out
