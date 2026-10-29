# 入门指南

先让一个短镜头成立，再把它组合成整片。首页的六个示例展示不同用途；38 项技法图谱还包括实拍规划和 AI 设计路线。

## 安装

需要 Python 3、FFmpeg 和 Python 依赖。FFmpeg 应能通过 `ffmpeg -version` 调用，包含 H.264 编码器 `libx264`。显式 HDR → SDR 转换还需要 `zscale`、`tonemap`。

```bash
git clone https://github.com/Yang-Jiashu/camera-movement-skills.git
cd camera-movement-skills
python3 -m pip install -r requirements.txt
python3 scripts/install_skills.py
```

安装器默认写入 `$CODEX_HOME/skills`，未设置时使用 `~/.codex/skills`，并自动安装 `.camera-motion-runtime` 共享运行时。已有副本升级会先备份：

```bash
python3 scripts/install_skills.py --upgrade
```

也可以只选用两项技能，或先预览将发生的安装：

```bash
python3 scripts/install_skills.py --skill camera-motion-prep --skill camera-reframe
python3 scripts/install_skills.py --dry-run
```

自定义技能目录用 `--dest /path/to/skills`。安装器不会替代 Python 依赖和 FFmpeg 的安装。

## 交给 Codex

调用总控技能时，提供素材路径、希望观众看到什么、时长和输出画幅：

```text
用 $camera-movement-direction 处理 /path/to/video.mp4。
希望先交代环境，再把注意力带到右侧人物；时长 5 秒，输出 1280×720。
先检查裁切余量和抖动，给我可执行的方案与成片。
```

明确需要单一效果时，直接调用对应技能，如 `$camera-barrel-roll` 或 `$camera-speed-ramp`。安装后若当前会话尚未发现技能，重新打开会话。

对已有素材，越具体的输入越省试错：主体是谁、动作起落在哪里、是否保留声音、有没有前后景图层。请求新的视角时，技能会判断应走实拍规划、有限的分层合成，还是 AI 提示词流程。

## 直接运行一个短镜头

以下命令从仓库根目录运行，使用随包的夜景视频，输出一秒向右侧水幕靠近的数字重构图：

```bash
python3 scripts/apply_camera_motion.py reframe \
  --input media/jewel-night.mp4 --output outputs/push.mp4 \
  --width 640 --height 360 --duration 1 \
  --zoom-start 1.05 --zoom-end 1.35 \
  --center-start 0.5 0.5 --center-end 0.61 0.5
```

完整滚转并回正：

```bash
python3 scripts/apply_camera_motion.py roll \
  --input media/gardens.mp4 --output outputs/roll.mp4 \
  --width 640 --height 360 --duration 1.5 \
  --angle-start 0 --angle-end 360
```

`center` 为 0–1 的归一化坐标；`zoom` 相对最大同画幅裁切，值越大画面越近；角度使用度。输出会伴随 JSON 报告，说明裁切、有效帧和检查结果。相同输出名默认受保护，确认需要替换时才加 `--overwrite`。

FFmpeg 不在 PATH 时，给处理命令增加 `--ffmpeg /path/to/ffmpeg`。完整参数见：

```bash
python3 scripts/apply_camera_motion.py --help
python3 scripts/apply_camera_motion.py reframe --help
python3 scripts/prepare_motion.py --help
```

## 把资产准备与成片连接

`camera_pipeline.py` 为四种需要准备资产的效果提供统一入口：

| 子命令 | 先准备什么 | 需要提供什么 |
| --- | --- | --- |
| `wipe` | 传播遮挡物掩码，生成揭示序列 | 两段视频与初始遮挡物 ROI，或初始物体掩码 |
| `parallax` | 提取静态前景、补全背景并核算运动余量 | 一张照片与前景 ROI，或已有 alpha |
| `layered-vertigo` | 同上，固定主体并反向缩放背景 | 一张照片与前景 ROI，或已有 alpha |
| `match-zoom` | 匹配图像特征，提出两个视觉锚点 | 两段有对应结构的素材，或一对手工锚点 |

```bash
python3 scripts/camera_pipeline.py --help
python3 scripts/camera_pipeline.py parallax --help
python3 scripts/camera_pipeline.py wipe --help
```

成功运行会得到 MP4、同名 JSON 和 `.assets` 目录；后者保留准备资产、QA 和渲染报告。ROI 是初始化帧中 `X Y WIDTH HEIGHT` 的归一化值，要根据自己的素材选择。遮挡转场的 `--init-frame` 相对转场起点计数，`--direction` 表示第二镜头从哪一侧揭示。

背景修复有可用范围。分层流程可缩小超出余量的运动并在报告中记录；加 `--strict-motion` 会直接拒绝。视频掩码传播与图像特征匹配均可能失败，错误信息与 QA 用于修正输入，不能代替逐帧验收。

## 复现首页的 CV 流程

以下命令从随包原图开始，以用户 ROI 和前后景提示点初始化 GrabCut，生成 alpha、前景、局部修复背景与有效性图，再输出四秒有限视差。无需预制掩码或图层：

```bash
python3 scripts/camera_pipeline.py parallax \
  --input media/flag.jpg --output outputs/auto-parallax.mp4 \
  --roi 0.085 0.24 0.73 0.56 \
  --foreground-point 0.3 0.4 \
  --foreground-point 0.6 0.315 \
  --foreground-point 0.6 0.35 \
  --background-point 0.1 0.245 \
  --background-point 0.805 0.5 \
  --foreground-scale-start 1.08 --foreground-scale-end 1.17 \
  --background-scale-start 1.08 --background-scale-end 1.10 \
  --foreground-shift-end 0.012 0 --background-shift-end -0.003 0 \
  --width 1280 --height 720 --duration 4 --fps 30 --strict-motion
```

此样例以原始运动幅度通过几何余量检查，输出 120 帧、1280×720、30fps 静音视频。[实际成片](../examples/pipeline/auto-parallax.mp4)与[运行摘要](../examples/pipeline/auto-parallax.json)保留结果；另有[自动视频掩码转场](../examples/pipeline/auto-wipe.mp4)及[摘要](../examples/pipeline/auto-wipe.json)。

`outputs/auto-parallax.assets/layers/` 内的 `background-validity.png` 区分可用背景与未认证的隐藏核心，`repair-mask.png` 标出这次运动需要的修复带。首页证据图将后者着青色、未知核心画为斜线；通过几何检查并不证明被遮挡的真实背景已恢复。

要根据此次实际生成的资产重建首页证据图：

```bash
python3 scripts/build_gallery.py \
  --pipeline-assets-dir outputs/auto-parallax.assets \
  --pipeline-source media/flag.jpg \
  --pipeline-video outputs/auto-parallax.mp4
```

## 先诊断，再准备

一秒运动诊断可用仓库的街景素材直接运行：

```bash
python3 scripts/prepare_motion.py analyze \
  --input media/street.mp4 --output outputs/street-motion.json --duration 1
```

诊断报告里的特征运动属于所选区域，并不自动知道哪部分是背景。有路人、反光或大面积遮挡时，先观察视频，再通过 `--background-roi X Y WIDTH HEIGHT` 选定可信背景。坐标与宽高均为 0–1 的归一化值。

需要稳像时保持有意的摇移，避免把所有运动一律消除。需要分层时，先检查 alpha 边缘和背景补洞；命令执行成功不能代替合成画面的验收。可选组合见 [CV 处理配方](../skills/camera-movement-direction/references/cv-recipes.md)。

## 复现展示与图库

```bash
python3 scripts/render_showcase.py --ffmpeg /path/to/ffmpeg
python3 scripts/build_gallery.py --ffmpeg /path/to/ffmpeg
```

展示渲染使用仓库内的固定素材和编排；通用 CLI 接受新的素材。图库脚本仅从现有六个视频等时长采样并加文字标题，不生成镜头内容。动图不含声音；完整展示片包含配乐与音效。

## 检查交付

播放从起点到终点，检查主体是否丢失、滚转是否回正、边界是否露黑、转场是否落稳，以及声音是否符合要求。数字运镜的裁切余量有限；分层运动不能补出原片不可见的新视角。

当前处理采用 CFR 帧索引时间轴，VFR 输入未自动检测；需要精确音画同步时应先规范帧率。默认多数合成为静音，源音频保留的条件见对应技能。HDR 视频应显式做颜色转换，避免二次映射。

[算法能力与验证](cv-capabilities.md) · [处理架构](architecture.md) · [示例原理](techniques.md) · [返回首页](../README.md)
