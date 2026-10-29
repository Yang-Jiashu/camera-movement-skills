---
name: camera-motion-prep
description: "为后期运镜执行视觉预处理：背景诊断与稳像、ROI初始化的视频掩码传播、有限分层和补洞、跨镜头视觉锚点配准。用于准备遮挡、2.5D与匹配变焦资产；语义身份重识别、透明matting与真实三维重建使用专门工具。"
---

# 运镜视觉预处理

在拍摄运动与后期动作之间准备可靠输入。只选当前镜头需要的处理；刻意的摇移、跟拍或冲击运动可以保留。

## 按问题选择入口

| 当前问题 | 执行方式 | 交付 |
| --- | --- | --- |
| 不确定片段是否适合全局稳像 | `analyze`，选择稳定背景ROI，检查每帧可信度 | 运动诊断JSON，失败帧与模型适用性 |
| 轻度抖动妨碍构图或转场 | `stabilize`，调平滑、强度与裁切预算 | 静音SDR视频及JSON |
| 照片或某一帧需要前景轮廓 | `mask`，矩形ROI与可选正负种子 | 同源尺寸8位alpha及JSON |
| 视频中的刚性遮挡物 | `motion_masks.py`，中间帧ROI/初始mask，双向光流传播与边缘验证 | 逐帧物体mask、经验证的直边reveal序列 |
| 照片需要有限2.5D运动 | `motion_layers.py`，ROI/alpha与目标运动 | 前景、alpha、有限补洞背景及允许的运动 |
| 两镜头有共同刚性纹理 | `motion_match.py`，SIFT匹配与RANSAC | 视觉锚点、内点及配准诊断 |
| 发丝透明边、语义身份、真实深度或新视角 | 读 [条件流程](references/preparation.md)，使用可用专门工具 | 标明已执行阶段与来源 |

## 处理顺序

1. 看实际画面并确定选段、画幅、颜色和时间基准。共用解码器按CFR帧索引运行，VFR未自动检测；需精确同步或颜色转换时读 [输入规范化](references/input-normalization.md)。
2. HDR输入先走显式HDR→SDR转换；不要对已有SDR重复映射。`analyze`和`stabilize`面向连续视频，跨切点分段。
3. 分清背景运动与主体运动。背景ROI应有分散的稳定纹理；行人、水、屏幕或树叶占主导时，单一全局模型可能不适用。
4. 按 [预处理规范](references/preparation.md) 先诊断再决定是否稳像；需要视频mask、分层补洞或自动锚点时读 [资产执行流程](references/asset-pipelines.md)，优先实际执行现成入口。
5. 稳像后尺寸、取景或时间基准若改变，重新标主体ROI、视觉锚点与mask。原片坐标不能直接套到处理后视频。
6. 将处理后素材交给相应运镜技能。稳像输出静音，声音按最终时间线另行处理。

## 可复用命令

以下从仓库根目录执行；ROI和种子示意值必须按素材重选：

```bash
python3 scripts/prepare_motion.py analyze --input input.mp4 --output motion.json --duration 3 --background-roi 0.05 0.05 0.8 0.5
python3 scripts/prepare_motion.py stabilize --input input.mp4 --output stable.mp4 --duration 3 --background-roi 0.05 0.05 0.8 0.5 --smoothing 0.15 --strength 0.8 --max-zoom 1.3
python3 scripts/prepare_motion.py mask --input photo.jpg --output alpha.png --roi 0.2 0.15 0.5 0.7 --foreground-point 0.45 0.4
```

查看每个子命令的 `--help`，通过 `--ffmpeg /absolute/path` 选择非PATH中的FFmpeg。安装后本技能的 `scripts/run.py` 接受相同子命令和参数；共享运行时由整库安装器提供，单独复制目录不包含它。

## 不可省略的判断

- `analyze`度量特征和几何匹配，不能自动断言整幅画面质量合格；可信特征可能来自错误的运动物体。
- 全局相似变换只能修正有限平移、旋转和尺度变化，不能修复滚动快门、视差变形、运动模糊或新增被遮挡内容。
- 稳像低置信默认停止；显式 `--fallback hold` 在失败帧对假定零增量并沿用累计位姿，后续可信帧对继续更新。它不会恢复失败区间的未知运动，需查看报告与相应画面。
- GrabCut为用户约束的颜色分割，软边羽化不等于真实alpha matting。单帧结果不自动传播到视频。
- 视频传播使用单独入口和一致性检查；直边reveal需观察到真实扫过画面。一般物体alpha不能直接当作转场mask。
- 有限补洞会评估运动暴露区；缩小的运动必须按manifest交付，严格要求用strict模式拒绝超预算请求。不要把补洞称为恢复真实隐藏背景。
- 输出失败时保留已有成片；不要降低可信度阈值来隐藏失败。缩短选段、换背景ROI、补种子或改为人工路径。

## 验收

正常播放比较处理前后背景抖动、构图漂移、裁切损失和主体完整性。检查报告中的失败区间和实际zoom，确认稳像没有抹去有意运动。掩码在棋盘格、深色和浅色背景上检查轮廓、孔洞及颜色污染。记录实际算法与人工调整，按需求交付诊断、视频或alpha。
