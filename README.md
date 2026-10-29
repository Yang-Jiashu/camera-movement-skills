# camera-movement-skills

用自己的照片和视频，做出有动感的镜头。

**12 个 skills · 38 项技法 · 6 个视频示例**

[![运镜动态预览](docs/preview.gif)](examples/showreel.mp4)

**[▶ 观看 28 秒完整展示片](examples/showreel.mp4)** · 720p / 30fps / 原创节拍

## 看效果

![六种效果一览](docs/contact-sheet.jpg)

[滚转](examples/01-barrel-roll.mp4) · [甩镜](examples/02-whip-pan.mp4) · [分层眩晕](examples/03-layered-vertigo.mp4) · [遮挡转场](examples/04-occlusion-wipe.mp4) · [匹配变焦](examples/05-match-zoom.mp4) · [速度渐变](examples/06-speed-ramp.mp4)

## 安装与使用

需要 Python 3 和 PATH 中的 FFmpeg（含 `libx264`）。

```bash
git clone https://github.com/Yang-Jiashu/camera-movement-skills.git
cd camera-movement-skills
python3 -m pip install -r requirements.txt
python3 scripts/install_skills.py
```

在 Codex 中调用，并提供素材路径：

```text
用 $camera-movement-direction 剪辑我的视频，做出节奏鲜明的运镜效果。
```

已有版本使用 `python3 scripts/install_skills.py --upgrade`；只安装部分技能可加 `--skill 技能名`。

## 选择技能

| 任务 | Skills |
| --- | --- |
| 规划与准备 | [运镜导演](skills/camera-movement-direction/SKILL.md) · [诊断 / 稳像 / 静态掩码](skills/camera-motion-prep/SKILL.md) · [AI 视频提示词](skills/camera-ai-video-prompts/SKILL.md) |
| 镜头运动 | [推拉重构图](skills/camera-reframe/SKILL.md) · [主体跟随](skills/camera-subject-track/SKILL.md) · [滚转](skills/camera-barrel-roll/SKILL.md) · [速度渐变](skills/camera-speed-ramp/SKILL.md) |
| 转场与空间 | [甩镜](skills/camera-whip-pan/SKILL.md) · [遮挡转场](skills/camera-occlusion-wipe/SKILL.md) · [匹配变焦](skills/camera-match-zoom/SKILL.md) · [分层眩晕](skills/camera-layered-vertigo/SKILL.md) · [2.5D 视差](skills/camera-depth-parallax/SKILL.md) |

每项技能都写有输入、算法步骤、失败处理和验收标准。深度模型、视频分割与三维重建属于可选外部流程，当前未内置自动调用。

## 深入查看

[38 项技法](docs/movement-catalog.md) · [场景配方](docs/shot-recipes.md) · [CV 能力矩阵](docs/cv-capabilities.md) · [处理流程](docs/architecture.md) · [示例实现](docs/techniques.md)

14 项回归测试通过；[真实素材检查](docs/validation-cv.json)与[独立检查](docs/validation-cv-independent.json)记录了验证范围和待修问题。视频处理采用 CFR 时间轴，声音与复杂掩码按具体流程处理。

演示来自作者的新加坡旅行素材，音乐由程序合成。[素材来源](media/provenance.json)；代码与媒体尚未指定开源许可证。
