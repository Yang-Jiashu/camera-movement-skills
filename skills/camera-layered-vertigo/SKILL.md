---
name: camera-layered-vertigo
description: "使用显式前景、背景和alpha图层制作2.5D眩晕效果，让刚性主体保持大小、远景反向变焦。用于分层vertigo合成；真实dolly zoom需要原片机位透视变化，普通视差位移使用depth-parallax。"
---

# 分层眩晕效果

让前景主体保持刚性和近似固定尺寸，同时改变后景比例。此技能执行有限的2.5D合成，交付中明确这一实现方式。

## 从原图准备资产

没有现成图层时，使用 `scripts/camera_pipeline.py layered-vertigo`，以原图ROI/种子或alpha初始化前景，执行有限背景补洞、运动暴露区检查和渲染。安装后本技能的 `scripts/pipeline.py` 固定此命令。先读 [资产流程](../camera-motion-prep/references/asset-pipelines.md)，查看QA图；必须使用manifest中的approved_motion，严格保持请求则用strict模式。已有可靠图层继续使用下方原入口。

## CV处理设计

用户要提前设计CV算法，或素材存在跟踪、边缘、几何、分层或时序问题时，先读 [处理方案](references/processing-pipeline.md)。参考按输入契约、预处理、算法、失败恢复与指标展开，并区分已有代码和需另行执行的设计。

如同库 [素材预处理技能](../camera-motion-prep/SKILL.md) 已安装，可按其实际帮助准备分析、稳像或mask；缺失时，本技能自己的参考也给出可执行的人工/CV处理步骤。

## 输入与资产条件

- 需要前景源图、完整背景图和对应alpha；白色为前景，黑色为背景，灰度用于柔边。
- 检查两图的空间关系、画幅与配准，确认alpha来自同一取景。前景可包含与主体连在一起的近处平台，避免建筑被切断。
- 原图中被前景遮挡的背景没有真实像素；先制作可接受的补洞，并记录补绘或生成的范围。本工具不会自动估计完整三维场景。
- 主体前景应有清楚轮廓，背景应有可辨认的层次。大片纯天空的缩放未必能产生明显效果。

## 参数与执行

| 参数 | 决策 |
| --- | --- |
| `--foreground-scale-start/end` | 前景倍率设为相同值以保持主体大小 |
| `--background-scale-start/end` | 用反向尺度变化制造空间关系变化；全程保持背景覆盖输出 |
| `--anchor x y` | 归一化缩放锚点，对应各层输入与输出同名位置 |
| `--foreground-shift-*` / `--background-shift-*` | 相对输出宽高的位移，固定主体时前景位移保持不变 |

例：前景固定，背景从较紧画幅逐渐退开：

```bash
python3 scripts/apply_camera_motion.py layered-vertigo --foreground foreground.png --background background.png --alpha alpha.png --output vertigo.mp4 --duration 4 --foreground-scale-start 1.2 --foreground-scale-end 1.2 --background-scale-start 1.2 --background-scale-end 1.05 --anchor 0.5 0.5
```

先看 `layered-vertigo --help`。倍率是相对“源图覆盖输出”的倍率，1表示按原比例铺满，不能直接套用其他工程的源像素仿射scale。

## 轮廓与背景边界

首、中、末帧检查主体轮廓和背景修补区，再看动态。缩放可能让原先隐藏的水面接缝、重复树木或矩形mask边界突然显露。

建筑内部保持统一刚性变换；不要用不稳定深度逐像素弯曲主体来追求更大幅度。背景覆盖不足或补洞明显时，减小幅度、调整构图或换素材，不能用复制边缘掩饰。

本命令没有原片同步音频，默认无声。输入应为可读SDR图层；HDR和色彩空间转换在生成图层前明确完成。

## 验收与交付

正常播放，验证主体尺寸近似固定、背景尺度变化可读、前景轮廓稳定且无露空。可用主体边缘或特征点测量首尾尺度变化，不能只依据输入参数宣称“锁定成功”。

报告层资产、alpha含义、锚点、倍率、位移、补洞方式和观察结果。命名“2.5D分层眩晕/Layered Vertigo”；不将其写成实拍dolly zoom、完整三维重建或能绕到主体背面。

## 运行入口

仓库根目录用 `scripts/apply_camera_motion.py layered-vertigo`；整库安装后本技能 `scripts/run.py` 已固定子命令。先读 `--help`；单独复制技能目录不会带上共享运行时。
