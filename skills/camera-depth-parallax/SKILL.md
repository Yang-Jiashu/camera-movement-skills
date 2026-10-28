---
name: camera-depth-parallax
description: "用显式前后景图层和alpha制作有限的2.5D视差、横纵移或弧移感，安排各层相对位移并检查遮挡补洞。用于有分层资产的照片运动；固定主体反向缩放用layered-vertigo，单图裁切用reframe。"
---

# 有限分层视差

利用不同层的相对位移表现前后关系。先选择能承受新露出区域的幅度，再设计动作；二维分层不提供准确的物体侧面和背面。

## CV处理设计

用户要提前设计CV算法，或素材存在跟踪、边缘、几何、分层或时序问题时，先读 [处理方案](references/processing-pipeline.md)。参考按输入契约、预处理、算法、失败恢复与指标展开，并区分已有代码和需另行执行的设计。

如同库 [素材预处理技能](../camera-motion-prep/SKILL.md) 已安装，可按其实际帮助准备分析、稳像或mask；缺失时，本技能自己的参考也给出可执行的人工/CV处理步骤。

## 输入与分层准备

- 需要前景源图、背景图与alpha。白色alpha表示前景，黑色表示后景，两层需对应同一空间与取景。
- 检查主体是否可以作为刚性层移动，以及近景被移开后会暴露什么。细枝、头发、透明物和反射面更容易显露分层缺陷。
- 深度图可以帮助制作层或mask，但不能直接当作真实度量几何；检查实体轮廓，再修正分层。
- 背景遮挡区需先补全并记录方法。本共享命令执行显式两层合成，不自动把单张照片重建成多视点场景。

## 参数与执行

| 参数 | 决策 |
| --- | --- |
| `--foreground-shift-start/end x y` | 前景相对输出宽高的位移，通常比远景移动更明显 |
| `--background-shift-start/end x y` | 远景位移；与前景形成可读的相对变化 |
| `--foreground-scale-start/end` / `--background-scale-start/end` | 相对各源图铺满输出的倍率，为移动预留边缘 |
| `--anchor x y` | 输入与输出中的同名归一化缩放锚点 |

例：让近景与远景反向小幅移动，形成有限视差：

```bash
python3 scripts/apply_camera_motion.py parallax --foreground foreground.png --background background.png --alpha alpha.png --output parallax.mp4 --duration 4 --foreground-scale-start 1.2 --foreground-scale-end 1.2 --background-scale-start 1.2 --background-scale-end 1.2 --foreground-shift-start -0.02 0 --foreground-shift-end 0.02 0 --background-shift-start 0.01 0 --background-shift-end -0.01 0 --anchor 0.5 0.5
```

先看 `parallax --help`。负位移表示相应输出坐标轴的反方向；不要把相机方向、图层移动方向与观众看到的景物方向混为一谈。

## 动作与实现边界

用层间横纵相对运动可以营造有限弧移感；应称“2.5D视差/合成弧移”，不能声称完成真实orbit、FPV穿越或大幅移动后的新视点。

当前景露出很大的未知背景时，优先缩小幅度或增加实际参考视图。用户要准确三维绕拍时，转为实拍、多视点重建、三维场景或经明确授权的生成路线。

保持层内建筑与人物刚性，检查mask边缘、遮挡先后和背景补洞。边界不足时默认停止；若用clamp修正轨迹，需报告实际运动与设计的差异。图层命令默认无声。

## 验收与交付

正常播放全段，检查层间关系可读、前景不漂浮脱离地面、边缘不拉丝、补洞不暴露明显重复结构。检查全程采样范围，不能只看静止首尾图。

交付记录图层来源、alpha、尺度、位移、锚点和补洞方式。明确哪些运动是合成、哪些像素来自原图；不要把深度估计或补洞效果当作已记录的真实空间。

## 运行入口

仓库根目录用 `scripts/apply_camera_motion.py parallax`；整库安装后的本技能 `scripts/run.py` 已固定子命令。共用运行时需随整库安装，先读 `--help`；只复制本技能目录不包含共享工具。
