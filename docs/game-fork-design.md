# 游戏 Fork 方案

## 背景

用户 A 创建了一个好玩的游戏，用户 B/C 也想在此基础上修改出自己的版本。需要支持从已有游戏 fork 出独立副本，各自迭代互不影响。

## 版本树模型

```
原版游戏 (ai_xxx)
  ├── 用户A修改1 → A修改2 → A修改3
  │                  └── 用户C fork → C修改1 → C修改2
  └── 用户B fork → B修改1 → B修改2
                      └── 用户D fork → D修改1
```

每个节点 = 一个游戏目录（代码 + meta.json + session）。fork 后各分支独立，互不干扰。

## Fork 实现

### 核心操作（纯文件操作，不需要 agent/LLM）

```python
async def fork_game(source_game_id: str, new_owner_id: str) -> str:
    """Fork 一个游戏，返回新 game_id。"""
    source_dir = GAMES_DIR / source_game_id
    new_game_id = _generate_game_id(source_meta["title"])
    new_dir = GAMES_DIR / new_game_id

    # 1. 拷贝游戏目录
    shutil.copytree(source_dir, new_dir)

    # 2. 更新 meta.json
    meta = json.loads((new_dir / "meta.json").read_text())
    meta["game_id"] = new_game_id
    meta["owner_id"] = new_owner_id
    meta["forked_from"] = source_game_id
    meta["created_at"] = int(time.time())
    meta["modified_at"] = int(time.time())
    meta["session_id"] = ""  # 清空或拷贝 session（见下文）
    meta["history"] = []     # 重置 history
    (new_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))

    # 3. 打 zip
    shutil.make_archive(str(GAMES_DIR / new_game_id), "zip", str(new_dir))

    return new_game_id
```

### Session 处理策略

SDK session 文件路径：`~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`

| 策略 | 做法 | 首次 modify 成本 | 复杂度 |
|------|------|-----------------|--------|
| **不拷贝 session** | `session_id=""` | 高（~70s Read+理解） | 零 |
| **拷贝 session** | 拷贝 JSONL 到新 cwd 对应目录 | 低（resume 直接 Edit） | 中 |

#### 拷贝 session 的实现

```python
def _copy_session(source_game_dir: str, target_game_dir: str, session_id: str):
    """将 session JSONL 拷贝到新游戏目录对应的 SDK 存储位置。"""
    claude_projects = Path.home() / ".claude" / "projects"

    def _encode_cwd(cwd: str) -> str:
        # SDK 将 cwd 中非字母数字字符替换为 -
        return re.sub(r'[^a-zA-Z0-9]', '-', cwd)

    source_encoded = _encode_cwd(source_game_dir)
    target_encoded = _encode_cwd(target_game_dir)

    source_session = claude_projects / source_encoded / f"{session_id}.jsonl"
    if source_session.exists():
        target_dir = claude_projects / target_encoded
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_session, target_dir / f"{session_id}.jsonl")
```

**前提条件**：session JSONL 内的工具调用全部使用相对路径（`scenes/main.gd`），两个目录结构相同，理论上可直接复用。

**需要实测验证**：SDK resume 时是否校验 cwd 与 session 记录的 cwd 一致。如不一致是否会报错或静默新建 session。

### meta.json 新增字段

```json
{
    "game_id": "ai_fork_123",
    "title": "火脸球",
    "forked_from": "ai_original_456",
    "owner_id": "user_b",
    "session_id": "copied-session-or-empty",
    "history": [],
    "..."
}
```

## API 设计

```
POST /api/games/{game_id}/fork
```

请求体（可选）：
```json
{
    "title": "我的火脸球改版"  // 可选，默认沿用原标题
}
```

响应：
```json
{
    "game_id": "ai_fork_123",
    "forked_from": "ai_original_456"
}
```

## iOS 端

- 游戏详情页 / 列表页增加 "Fork" 按钮
- fork 后跳转到新游戏的编辑/游玩页面
- 游戏列表可展示 "基于 XXX 改编" 标签（读 `forked_from` 字段）

## 权限

- 默认：只能 fork 自己的游戏
- 后续可扩展：公开游戏允许任何人 fork

## 存储

每个 fork 是完整拷贝（~50-100KB 代码 + ~8KB zip）。1000 个 fork ≈ 100MB，不是问题。

## 实施优先级

1. **P0（当前）**：先验证 resume + Edit 优化效果
2. **P1**：实测 session 拷贝是否可行（拷贝 JSONL + resume）
3. **P2**：实现 fork API + iOS UI
