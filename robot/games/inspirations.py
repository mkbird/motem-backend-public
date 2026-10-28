"""Hard-coded inspiration cards shown in iOS GameCreatorView's chat tab.

When the user toggles `idea` ON in the new game creator UI, the iOS app fetches
this list via `GET /api/games/inspirations` (Accept-Language honored). The cards
are tap-to-fill: tapping one populates the prompt field with the card's `prompt`
text so the user can edit and submit.

Currently hard-coded — can be promoted to DB / admin CMS later without changing
the API contract.

Each card has:
- `id`     — stable identifier, unique within a language
- `title`  — short genre label, ≤ 12 chars
- `description` — one-liner pitch, ≤ 40 chars
- `prompt` — full sentence the iOS prompt field will be filled with on tap
"""

INSPIRATIONS_EN: list[dict[str, str]] = [
    {
        "id": "shooter",
        "title": "Space Shooter",
        "description": "Top-down ship vs alien waves",
        "prompt": (
            "A top-down space shooter where I control a spaceship that auto-fires bullets. "
            "Enemies spawn from the top in waves; the wave count and difficulty rises over time. "
            "Show a score in the top-left."
        ),
    },
    {
        "id": "rhythm",
        "title": "Rhythm Tap",
        "description": "Tap notes on the beat",
        "prompt": (
            "A rhythm game where falling notes appear in 4 vertical lanes from the top. "
            "Tap each lane when the note reaches the bottom hit zone. "
            "Track combo and accuracy; show score and combo prominently."
        ),
    },
    {
        "id": "platformer",
        "title": "Platform Jumper",
        "description": "Hop platforms, dodge spikes",
        "prompt": (
            "A side-scrolling platformer where the player auto-runs to the right and jumps when tapped. "
            "Avoid spikes and gaps. Track distance traveled as the score; speed slowly increases."
        ),
    },
    {
        "id": "endless_runner",
        "title": "Runner",
        "description": "Endless dash with obstacles",
        "prompt": (
            "An endless runner where the character runs forward automatically. "
            "Tap left half of screen to dodge left, right half to dodge right. "
            "Obstacles appear in 3 lanes; track distance as score."
        ),
    },
    {
        "id": "pong",
        "title": "Pong Battle",
        "description": "Paddle & ball, single-player",
        "prompt": (
            "A pong-style game: a paddle at the bottom, a ball that bounces off walls and paddle. "
            "Move paddle by dragging. Track how many bounces in a row without dropping the ball."
        ),
    },
    {
        "id": "match3",
        "title": "Match 3",
        "description": "Swap tiles to make matches",
        "prompt": (
            "A match-3 puzzle where a 6x6 grid of colored tiles can be swapped between adjacent cells. "
            "Match 3 or more in a row clears them and grants points; new tiles fall from above."
        ),
    },
]


INSPIRATIONS_ZH: list[dict[str, str]] = [
    {
        "id": "shooter",
        "title": "太空射击",
        "description": "从上往下打飞机",
        "prompt": (
            "一个俯视角太空射击游戏，我控制一艘自动开火的飞船。"
            "敌人从屏幕顶部成波次出现，波数和难度逐渐上升。"
            "左上角显示得分。"
        ),
    },
    {
        "id": "rhythm",
        "title": "节奏点击",
        "description": "踩点点击下落的音符",
        "prompt": (
            "一个节奏游戏：4 条垂直轨道从上方落下音符，"
            "音符到达底部判定区时点击对应轨道。"
            "记录连击和准确率，显示分数和连击数。"
        ),
    },
    {
        "id": "platformer",
        "title": "平台跳跃",
        "description": "跳过平台躲尖刺",
        "prompt": (
            "一个横版平台跳跃游戏，角色自动向右跑，点屏幕跳跃。"
            "躲避尖刺和深坑。以前进距离作为分数，速度缓慢上升。"
        ),
    },
    {
        "id": "endless_runner",
        "title": "无尽跑酷",
        "description": "三车道躲障碍",
        "prompt": (
            "一个无尽跑酷游戏：角色自动向前跑。"
            "点击屏幕左半边切到左车道，右半边切到右车道。"
            "障碍物分布在 3 条车道中，以距离作为分数。"
        ),
    },
    {
        "id": "pong",
        "title": "弹球",
        "description": "挡板接弹球，看连击",
        "prompt": (
            "一个 pong 风格小游戏：底部一个挡板，一个弹球在墙壁和挡板间弹跳。"
            "拖动挡板。记录连续接住弹球不掉的次数。"
        ),
    },
    {
        "id": "match3",
        "title": "三消",
        "description": "交换方块凑三消",
        "prompt": (
            "一个三消益智游戏：6x6 网格里可交换相邻彩色方块。"
            "横竖连成 3 个或以上同色就消除得分，上方新方块下落补位。"
        ),
    },
]


def for_language(lang: str) -> list[dict[str, str]]:
    """Pick the inspiration list matching the normalized language code.

    Empty string / unknown lang → fall back to English.
    """
    if not lang:
        return INSPIRATIONS_EN
    code = lang.lower().split("-")[0]
    if code == "zh":
        return INSPIRATIONS_ZH
    return INSPIRATIONS_EN
