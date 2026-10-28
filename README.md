# camera-movement-skills

**一个运镜导演，加十个可单独调用的专项 skill。** 从镜头目的、起止构图与素材条件出发，为实拍规划、已有照片/视频后期和 AI 视频提示词选择可实现的路线。

先看现成的 **28 秒展示片**：用新加坡旅拍素材展示滚转、甩镜、分层眩晕、遮挡揭示、匹配变焦与速度渐变。

[![六种运镜动态预览](docs/preview.gif)](examples/showreel.mp4)

**[观看完整 MP4](examples/showreel.mp4)** · 1280 × 720 · 30 fps · 原创程序合成节拍

## 现成示例：六种效果

| 主片时段 | 效果 | 画面里的变化 | 独立示例 |
| --- | --- | --- | --- |
| 00–04 秒 | **Barrel Roll · 滚转** | 高空俯瞰沿画面光轴转过一圈，回到正向构图 | [01-barrel-roll.mp4](examples/01-barrel-roll.mp4) |
| 04–08 秒 | **Whip Pan · 甩镜** | 城市街景甩向滨海湾，方向模糊连接切点 | [02-whip-pan.mp4](examples/02-whip-pan.mp4) |
| 08–12 秒 | **Layered Vertigo · 分层眩晕** | 艺科馆前景保持大小，远景反向缩小 | [03-layered-vertigo.mp4](examples/03-layered-vertigo.mp4) |
| 12–16 秒 | **Occlusion Wipe · 遮挡揭示** | 机场立柱经过镜头，揭开夜间樟宜瀑布 | [04-occlusion-wipe.mp4](examples/04-occlusion-wipe.mp4) |
| 16–20 秒 | **Match Zoom · 匹配变焦** | 屋顶环口成为视觉锚点，白天切入夜间 | [05-match-zoom.mp4](examples/05-match-zoom.mp4) |
| 20–24 秒 | **Speed Ramp · 速度渐变** | 列车穿过树木与水幕，行进节奏由疏到密再展开 | [06-speed-ramp.mp4](examples/06-speed-ramp.mp4) |

24–28 秒用六段回闪与片名收束。节拍由程序合成，未使用第三方音乐。

![六种效果画面一览](docs/contact-sheet.jpg)

以上是当前提供的六段示例，展示真实素材上的数字执行与合成：真实机位运动保留自原片，滚转、甩镜和变焦由后期完成，分层眩晕使用 2.5D 合成。它们不代表下面每个工作流都已有对应实拍演示。素材来源、实现方式与观察重点见 [示例说明](docs/techniques.md)。

## 技能库：按工作流拆分

| Skill | 适合解决的问题 | 主要输入 / 输出 | 现成示例 |
| --- | --- | --- | --- |
| [camera-movement-direction](skills/camera-movement-direction/SKILL.md) | 分析素材、挑选技法、安排分镜和整片节奏 | 场景或素材 → 镜头计划、执行路线与验收重点 | 28 秒整片 |
| [camera-reframe](skills/camera-reframe/SKILL.md) | 数字推拉与横纵重构图 | 照片/视频、起止倍率和中心 → 裁切动画 | 变焦示例中的局部能力 |
| [camera-subject-track](skills/camera-subject-track/SKILL.md) | 让取景平滑跟随指定人物或物体 | 视频、初始主体区域 → 跟踪重构图 | 技能说明 |
| [camera-barrel-roll](skills/camera-barrel-roll/SKILL.md) | 滚转角度、旋转余量和回正 | 视频、角度与节奏 → 数字滚转 | 01 |
| [camera-whip-pan](skills/camera-whip-pan/SKILL.md) | 快速甩动连接两镜头 | 两段视频、方向与过渡时间 → 甩镜转场 | 02 |
| [camera-occlusion-wipe](skills/camera-occlusion-wipe/SKILL.md) | 前景遮挡与下一场景的边缘合成 | 两段视频、对应掩码 → 遮挡切换 | 04 |
| [camera-match-zoom](skills/camera-match-zoom/SKILL.md) | 用视觉锚点连接两个地点或时段 | 两段画面、各自锚点与倍率 → 匹配变焦 | 05 |
| [camera-speed-ramp](skills/camera-speed-ramp/SKILL.md) | 压缩行进、强调动作经过时刻 | 视频、目标时长与速度曲线 → 时间重映射 | 06 |
| [camera-layered-vertigo](skills/camera-layered-vertigo/SKILL.md) | 固定主体、反向缩放背景的眩晕演绎 | 刚性前景、背景、alpha → 2.5D 合成 | 03 |
| [camera-depth-parallax](skills/camera-depth-parallax/SKILL.md) | 在图层覆盖内产生有限空间层次 | 分层资产、移动幅度 → 有限视差合成 | 技能说明 |
| [camera-ai-video-prompts](skills/camera-ai-video-prompts/SKILL.md) | 把镜头设计写成可复制的生成描述 | 场景/参考、平台与目标动作 → 镜头规格和提示词 | 提示词与检查方法 |

共用的读取、几何约束与编码逻辑集中维护；各执行 skill 说明自己独有的输入、决策和失败边界。图层合成与真实机位运动分别命名，数字滚转不会写成围绕主体的三维环绕。

了解完整摄影语言，可先读 [38 项运镜与相关技法图谱](docs/movement-catalog.md)；要按建筑、人物、美食、产品等场景选镜头，读 [八个镜头配方](docs/shot-recipes.md)。

## 安装并调用

需要 Python 3、[requirements.txt](requirements.txt) 中的依赖，以及带 `libx264` 编码器的 FFmpeg。

在项目根目录运行：

```bash
python3 -m pip install -r requirements.txt
python3 scripts/install_skills.py
```

安装器将 skill 与它们需要的共享运行时一起安装到个人技能目录。自定义位置、覆盖和其他安装选项以 `python3 scripts/install_skills.py --help` 为准，避免只复制一个技能文件夹而漏掉执行依赖。

在 Codex 中用技能名称调用，例如：

```text
用 $camera-movement-direction 分析我的旅拍素材，设计一支节奏鲜明的运镜展示。
给出每个镜头的起止构图、运动路径和实现方法，再导出独立示例与完整主片。
```

```text
用 $camera-barrel-roll 处理我的航拍视频，做一次完整滚转后回正。
先检查旋转裁切余量，再输出可读的起点、动作过程与结束构图。
```

## 处理自己的素材

独立技能面向新的输入；演示片的固定时间线另由 `render_showcase.py` 复现。

| 先说明什么 | 为什么需要 |
| --- | --- |
| 素材路径、起止区间、输出画幅和时长 | 确定有效画面、真实运动和时间范围 |
| 主体区域或每侧的视觉锚点 | 确定跟踪谁、变焦看哪里、两段怎样对应 |
| 起止倍率、中心、角度或速度关系 | 将“更有动感”转成可执行的变化 |
| 掩码和图层对应关系 | 核实当前源片、分辨率、起始帧与背景覆盖 |
| 声音与合成意图 | 决定原声如何处理、哪些新增画面被允许 |

数字重构图使用已有像素；完整环绕、全新高机位或隐藏背面需要对应实拍、多视角、三维重建或生成输入。HDR 素材应先完成明确的色彩转换流程，避免把已有 SDR 素材再做一次 HDR 映射。

例如，使用随包输入做一次推近，所有参数都能替换为自己的素材：

```bash
python3 scripts/apply_camera_motion.py reframe --input media/jewel-night.mp4 --output outputs/push.mp4 --duration 1 --zoom-start 1.05 --zoom-end 1.3 --center-start 0.5 0.5 --center-end 0.6 0.5 --max-upscale 1 --ffmpeg /path/to/ffmpeg
python3 scripts/apply_camera_motion.py roll --input media/gardens.mp4 --output outputs/roll.mp4 --duration 1.5 --angle-end 360 --max-upscale 1 --ffmpeg /path/to/ffmpeg
```

通用命令按 CFR 帧索引处理。需要精确时序或原声同步的 VFR 视频，应先规范化为 CFR；当前不会自动检测 VFR。默认输出静音，`--audio source` 仅用于不改变时间线的单视频处理。坐标、角度、倍率和采样修正写入输出旁的 JSON 报告。

通用命令入口及各子命令的实际参数可直接查询：

```bash
python3 scripts/apply_camera_motion.py --help
```

## 复现展示片

```bash
python3 scripts/render_showcase.py --input-dir media --output-dir examples --ffmpeg /path/to/ffmpeg
python3 scripts/build_previews.py --examples-dir examples --docs-dir docs --ffmpeg /path/to/ffmpeg
```

把 `/path/to/ffmpeg` 替换为本机 FFmpeg 可执行文件路径。`media/` 随包提供裁剪后的演示媒体与遮挡掩码，复现无需原始 4K 文件。

`render_showcase.py` 会先从前景、背景和掩码重建分层片段，再渲染主片。`build_previews.py` 生成画面一览与八秒 GIF 预告；GIF 使用四段效果的加速片段，完整动作和声音以 MP4 为准。

```text
docs/                         动态预览、画面一览与技法说明
examples/                     主片与六个独立示例
media/                        演示输入与遮挡掩码
scripts/                      通用执行、安装器与展示片复现入口
skills/                       总控与十个专项 skill
requirements.txt              Python 依赖
```

仓库内的图片、示例和文档使用相对路径，便于随分支或克隆目录一起保留，写法见 [GitHub 官方文档](https://docs.github.com/en/get-started/writing-on-github/getting-started-with-writing-and-formatting-on-github/basic-writing-and-formatting-syntax#relative-links)。

## 验证范围

- 六段现成示例与28秒主片：720p、30fps，已完整解码与视觉检查。
- 九种通用技法：各以真实素材短导出验证；另测外部遮挡掩码、原声、HDR转换与失败保护。
- 几何与时间映射：`python3 -m unittest discover -s tests -v`。
- 详细记录：[主片验收](docs/validation.json)、[通用工具验收](docs/validation-runtime.json)。
