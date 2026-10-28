# 体感输入解释层设计（Motion Input Interpretation Layer）

> 状态：**设计蓝图（尚未实现）**。本文是 native（iOS / macOS）与 Web 三端、fast-path 与
> SDK 两类游戏在"摄像头视觉 → 游戏输入"这件事上的**唯一依据**。实现时以此为准；偏离需先改本文。
>
> 关联：体感游戏运行时契约见 `robot/CLAUDE.md`（InputBridge）、fast-path archetype 见
> `motem-backend-game-fastpath/README.md`、跨平台创作见 `motem-studio/docs/`。

## 1. 背景与目标

体感游戏的视觉**检测库**天然是两套，且不该统一：

| Runtime | 检测库 | 原因 |
|---|---|---|
| iPhone / macOS | **Apple Vision**（系统自带） | 原生、Neural Engine 加速、零依赖；**进不了浏览器** |
| 浏览器（Web 游戏） | **MediaPipe**（谷歌开源，WASM） | 浏览器里唯一可行 |

> 强行统一只能往 MediaPipe（Apple Vision 上不了 Web），代价是给原生引入重依赖、丢掉原生最优，
> 收益边际 → **不统一检测库**。

真正要统一、且要能撑住**多种类型**体感游戏（接物 / 闪避 / 打地鼠 / 投篮 / 排球 / 切水果 /
植物大战僵尸 / 网球 / 跳舞 / 健身 / 瑜伽 / 拳击 / 射箭 / …）的，是**解释层**：把 landmark
翻译成游戏输入的那套逻辑。本文定义它。

**核心矛盾**：现在 InputBridge 是 ~10 个**固定低级属性**，为现有 archetype 量身定，不同游戏类型
输入语义差异极大，继续往属性表加字段不可持续。**解决办法 = 分层 + 可组合识别器 + 稳定交付契约。**

## 2. 三层架构

```
Tier 1  检测器（平台相关，薄）
        Apple Vision(native) / MediaPipe(web) → 只产【规范帧】→ InputBridge
        不做任何手势判定。
   ↓
Tier 2  解释库（GDScript，三端共享一份，本文核心）
        消费规范帧 → 连续通道 + 离散事件
        识别器可组合，按每个游戏声明的集合实例化
   ↓
Tier 3  交付（Godot 原生 action 系统）
        Input.parse_input_event() 注入合成 InputEventAction
        游戏读 is_action_just_pressed("poke") / get_action_strength("throw_power")
        —— 完全 CV-无关，iOS/Web 同一份游戏代码
```

**为什么 Tier 2 放在 Godot（GDScript）**：游戏引擎是所有平台的公共 runtime（iPhone =
SwiftGodotKit、Web = Godot WASM）。把识别器写进 Godot 模板 → **一份实现三端跑**；平台差异只
剩 Tier 1 的"landmark → 规范特征"（无法避免）。这与 MediaPipe（landmark 是稳定接口、分类是薄头）、
Just Dance（一套共享骨架+解释器跨平台、因为"独立于任何主机"）的工程取向一致。

## 3. 规范帧契约（Tier 1 → Tier 2）

> **⚠️ 必须现在就锁定的决定**：帧是**玩家列表**，不是单人。单人时长度为 1。
> 现在改零成本；等做多人再改就是破坏性契约变更。

```jsonc
{
  "t": 1234567.89,                 // 单调时间戳（秒），识别器算速度/时序用
  "players": [
    {
      "id": "p0",                  // 稳定身份（Apple Vision / MediaPipe trackingId）
      "present": true,
      "face": {                    // 归一化到 [0,1]（图像坐标，前置镜像后）
        "pos": [0.5, 0.4],
        "yaw": 0.0, "pitch": 0.0, "roll": 0.0,   // 弧度
        "blendshapes": { "mouthOpen": 0.0, "browUp": 0.0, "smile": 0.0, "blink": 0.0 }
      },
      "hands": [                   // 0..2 只手
        { "side": "right", "pos": [0.6,0.5], "palm_open": 0.0, "pinch": 0.0,
          "z": 0.0,                // 相对深度（模型给的，小=近镜头）；可空
          "vel": [0.0,0.0] }       // 归一化/秒，Tier 1 已平滑
      ],
      "body": {                    // 33 点骨架的派生量（归一化）
        "lean_x": 0.0, "lean_y": 0.0,
        "joints": { "hip":[..], "l_wrist":[..], "r_ankle":[..], "...": [] },
        "center_vel": [0.0,0.0]
      }
    }
  ]
}
```

约定：所有位置归一化 `[0,1]`（或轴向 `[-1,1]`）；坐标系/死区/平滑策略**只定义一次**，native 与
Web 各自照此实现（消除行为漂移）。Tier 1 负责平滑 + 速度差分 + 前置镜像。

## 4. 两通道模型（连续通道 + 离散事件）

借 Kinect Visual Gesture Builder 的形式化：
- **连续通道（Continuous）**：每帧有效的标量/矢量 —— 指针位置、轴、力度进度。对应 Godot
  `get_action_strength()` / 自定义通道。
- **离散事件（Discrete）**：识别器在某瞬间"判定发生" —— swipe / throw / poke / 命中。进**事件队列**，
  对应 `is_action_just_pressed()`。
- **门控 + 进度配对**：蓄力类（投篮/射箭）= 连续 `charge` 进度 + 释放瞬间离散触发采样。

```gdscript
# Tier 3 游戏侧读法（CV-无关）
var aim := bridge.channel("pointer", 0)            # 连续
var power := bridge.strength("throw_power")        # 连续进度
for ev in bridge.poll_events():                    # 离散
    match ev.type:
        "swipe": slice_along(ev.path)
        "throw": launch(ev.power, ev.angle)
        "poke":  whack(ev.pos)
```

## 5. 识别器目录（Tier 2 工具箱）

每个识别器吃规范帧、吐**连续通道**或**离散事件**或**匹配分**。算法刻意避开训练流水线
（$Q 模板 / DTW / 启发式 / 姿势相似度）。

### 5.1 已有（覆盖接物/闪避/打地鼠/投篮/排球/切水果/PvZ）

| ID | 名称 | 输入 | 输出 | 算法 |
|---|---|---|---|---|
| pointer | `pointer(hand\|face)` | 一只手或鼻尖 | 连续 `(x,y,active)` | 直接映射 |
| poke | `poke` | 指针 + 前戳速度 | 事件 `{pos}` | 速度阈值 |
| swipe | `swipe / shape` | 手轨迹窗口 | 事件 `{path,vel,dir,shape}` | **$Q** + Douglas-Peucker（slicer_v2 已有，应正式化） |
| throw | `throw` | 手 windup→release | 事件 `{power,angle}` + 连续 `charge` | 启发式 + 2D 速度 |
| bump | `bump` | 抬手+上冲 | 事件 `{pos,power}` | 速度阈值 |
| select | `dwell / pinch_select` | 指针停留 / 捏合 | 事件 `{pos}` | 停留计时 / pinch 阈值 |
| pose_state | `pose_state` | 骨架 | 连续 `lean_x/y, duck, hands_up` | 启发式 |
| power | `velocity_power` | 任意关节 | 连续 `[0,1]` | 2D 速度（+可选 Δz / Δsize 近似深度） |

### 5.2 待补（按品类缺口，撬动商业体感目录主体）

| ID | 名称 | 输入 | 输出 | 算法 | 解锁品类 |
|---|---|---|---|---|---|
| **swing** | `swing` | 主手腕轨迹窗口 | 事件 `{direction,plane,speed,impact_t}` | 弧线启发式 + 顶点 2D 速度，平面类可 $Q | 网球/高尔夫/棒球/羽毛球/保龄球/曲棍球（**最大单一品类**） |
| **pose_match** | `pose_match`（单帧） | 骨架 vs 目标姿势模板 | 匹配分 `0–1`（+阈值→事件） | 关节角余弦/欧氏距离 | 穿墙洞 / 瑜伽单姿 / 模仿秀 / 123木头人 |
| **pose_seq** | `pose_sequence_match` | 骨架流 vs 编排模板 | 匹配分流 + 累计分 | **DTW** over 逐帧 pose 相似度 | 跳舞（Just Dance/Zumba） |
| **rep** | `rep_counter` | 单关节轴（髋y/脚y/腕y） | 每次事件 + 连续 `rep_progress` | 峰值/过零 + 滞回 | 健身（深蹲/跳跃/踏步） |
| **pose_hold** | `pose_hold` | 骨架 + 目标姿势 + 时长 | 完成事件 + 连续 `hold_progress` | pose_match≥阈值持续 N 秒；freeze=运动能量<ε | 瑜伽 / 木头人 / 平衡 |
| **limb_hit** | `limb_hit` | 所有肢端 + 游戏定义区域 | 事件 `{limb,zone,vel,punch_type}` | poke 泛化：点在区域 + 入区速度 | 拳击连击 / 砸目标 |
| **bimanual** | `bimanual` | 双手位置 | 连续 `{wheel_angle,separation,midpoint}` | 纯几何/三角 | 方向盘 / 划水 / 拉弓 |
| **aim_charge** | `aim_charge_release` | 瞄准手矢量 + 拉拽手距离 | 连续 `aim_vec` + `charge` + 释放事件 | 门控+进度 | 射箭 / 弹弓 / 蓄力法术 |
| **trace** | `trace_follow` | 指针路径 vs 目标路径 | 连续 `path_adherence 0–1` | 到最近线段距离 / $Q 部分匹配 | 迷宫 / 描线 |
| **face_expr** | `face_expression` | blendshape 系数 | 事件（挑眉/张嘴/微笑/眨眼） | 阈值（≈免费，Vision/MediaPipe 直接给） | 无障碍 / 表情休闲 |
| **spin** | `spin_modifier` | 释放瞬间侧向腕速 | 连续 `spin ∈ [-1,1]` | 启发式，附着到 throw/swing | 保龄球弧线 / 网球旋转 |
| **dodge** | `dodge` | 躯干质心侧向速度 | 事件 `{dir}` | 速度阈值 | 闪避障碍 |

## 6. 声明式手势集（每个游戏声明要哪些）

游戏在 `meta.json`（fast-path）或 `main.gd` 头部（SDK）声明所需识别器 + 参数；Tier-2 **只实例化用到的**，
并可**静态检测冲突**（Proton 启发：同一游戏同时要 swipe 与 poke 时，判定边界是否打架）。

```jsonc
"motion": {
  "recognizers": {
    "whack":  ["pointer:hand0", "poke"],
    "bball":  ["throw"],
    "volley": ["bump"],
    "slicer": ["swipe"],
    "pvz":    ["pointer:hand0", "dwell_select"],
    "tennis": ["swing", "spin"],
    "dance":  ["pose_sequence_match"],          // 需姿势模板资产
    "fitness":["rep_counter"]
  },
  "pose_templates": "poses/*.json",             // 见 §7.1，仅 pose_* 用
  "beat": { "bpm": 120, "track": "song.ogg" }   // 见 §7.2，仅节奏游戏用
}
```

## 7. 三处真·架构追加（其余皆"加识别器"）

绝大多数识别器吃现有规范帧、走现有通道/事件总线，**不改架构**。只有下面三处需要追加基础设施：

### 7.1 姿势模板数据存储（pose_match/seq/hold 一来就需要）
把"目标姿势 / 整套编排"**当数据**存（类比 Kinect 手势库）。新增资产类型：
- 姿势模板 = 命名的归一化关节角度集；
- 编排序列 = 带时间戳的姿势轨道。
识别器是**通用**的，模板是**每游戏数据**。把"手势声明为数据"原则扩展到全身姿势。

### 7.2 节拍时钟服务（节奏游戏需要）
共享 BPM + 相位时间线，游戏驱动、识别器查询，使任意离散事件（limb_hit/swing/pose_match）能按
"是否踩点"打分。是 Tier-2 兄弟服务，不是识别器。

### 7.3 多人多骨架身份（多人游戏需要）
帧已设计成**玩家列表**（§3），所以契约不破。剩下：Tier-2 按 `player_id` 路由通道/事件、
交付层分玩家 action 集、active-player/分配策略（Kinect `TrackingId` / Nex 自动选主玩家模式）。
**现在做契约（列表），路由以后补。**

## 8. 单前置摄像头的力度 / 深度

- **主信号**：2D 关节速度（最可靠的单目信号）→ swipe/throw/swing 力度。
- **辅助**：模型免费给的相对 Z（MediaPipe Pose/Hand world coords、Apple Vision），或手部尺寸变化
  （手变大=朝镜头来）作"推/前掷"代理；仅定性用。
- **配方**：`power = clamp(w1·|2D速度| + w2·(Δsize|Δz), 0, 1)`，用离散释放/接触触发门控采样。

## 9. 算法选型（不要训练流水线）

| 用途 | 选 | 理由 |
|---|---|---|
| 笔画/形状（切水果、描线） | **$Q**（$P 的 142× 加速版，移动端友好，~100 行） | 无训练、缩放/旋转不变 |
| 时序轨迹（投/挥/击） | **DTW**（少量模板） | 速度不变；模板别太多 |
| 连续 + 简单离散（指针/戳/倾斜/计次） | **启发式/阈值** | 延迟最低、纯 GDScript |
| 姿势相似度（匹配/保持/舞蹈） | 关节角余弦距离（+ DTW 做序列） | 无训练 |
| 大词汇量学习手势 | HMM / MediaPipe Model Maker | **仅在确有需要时**，藏在同一识别器接口后，Tier-3 不变 |

## 10. 优先级路线

- **Tier A（先做，撬动最多品类）**：`swing` + `pose_match`(+模板存储) + `rep_counter`
  —— 从"5 个休闲游戏"拉到覆盖商业体感目录主体（运动挥击 + 健身 + 姿势/舞蹈基础）。
- **Tier B**：`pose_sequence_match`（DTW，整个跳舞品类）、`limb_hit`（拳击）、`face_expression`（近免费）。
- **Tier C**：`aim_charge`/`bimanual`、节拍时钟、多人路由（契约现在改、路由以后补）。

落地建议：先做 Tier-1 规范帧 + pointer + swipe($Q)，验证全链；再按上表逐个加。识别器全部进
`games/_template*/scripts/`（GDScript），native/web 共享；detector 只喂规范帧。

## 11. 示例游戏 → 识别器映射

| 游戏 | 识别器组合 |
|---|---|
| 打地鼠 | `pointer + poke` |
| 投篮 | `throw`（门控+进度） |
| 排球 | `bump` |
| 切水果 | `swipe($Q)` |
| 植物大战僵尸 | `pointer + dwell_select` |
| 网球/高尔夫/棒球 | `swing (+spin)` |
| 跳舞 | `pose_sequence_match` + 姿势模板 + 节拍时钟 |
| 健身（深蹲/跳跃） | `rep_counter` |
| 瑜伽 / 木头人 | `pose_hold` |
| 拳击 | `limb_hit`（多拳分区） |
| 射箭 | `aim_charge_release` + `bimanual` |
| 无障碍/表情 | `face_expression` + `pointer(face)` |

## 12. 参考来源

- 手势算法：[$1/$P 识别器](https://depts.washington.edu/acelab/proj/dollar/index.html)、
  [$Q 识别器](https://depts.washington.edu/acelab/proj/dollar/qdollar.html)、
  [HMM vs DTW](https://link.springer.com/chapter/10.1007/978-3-642-33275-3_29)
- 两通道 + 门控：[Kinect Visual Gesture Builder](https://github.com/Kinect/Docs/blob/master/Kinect4Windows2.0/k4w2/NUI_Tools/Visual_Gesture_Builder_0/Overview.md)、
  [RFRProgress](https://learn.microsoft.com/en-us/previous-versions/windows/kinect/dn785524(v=ieb.10))
- landmark=稳定接口：[MediaPipe Gesture Recognizer](https://developers.google.com/edge/mediapipe/solutions/vision/gesture_recognizer)、
  [Pose Landmarker](https://developers.google.com/mediapipe/solutions/vision/pose_landmarker/python)
- 声明式/冲突检测：[Proton（CHI 2012）](http://vis-ucb-maneesh.stanford.edu/papers/proton/proton-CHI2012.pdf)
- 跨平台一套解释器：[Just Dance Camera Controller](https://www.ubisoft.com/en-us/game/just-dance/2026/news-updates/2UBGhF4xWMLAVVFMmTCWwn/revolutionising-motion-gaming-the-camera-controller-feature)、
  [Nex Playground](https://en.wikipedia.org/wiki/Nex_Playground)
- 引擎 action 交付：[Unity Input Actions](https://docs.unity3d.com/Packages/com.unity.inputsystem@1.5/manual/Actions.html)、
  [Godot input system](https://deepwiki.com/godotengine/godot/3.6-input-system)
- 品类全景：[Kinect Sports](https://en.wikipedia.org/wiki/Kinect_Sports)、
  [Switch Sports](https://en.wikipedia.org/wiki/Nintendo_Switch_Sports)、
  [Project Gameface](https://developers.googleblog.com/en/project-gameface-makes-gaming-accessible-to-everyone/)
