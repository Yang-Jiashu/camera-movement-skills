# 可执行的运镜资产准备

这些入口是本地实现，不需要模型权重。初始化ROI、种子或初始mask用于指定操作对象；它们不等于任意视频的语义识别。先查看选段，再按需执行。

## 直接产出效果

仓库根目录 `python3 scripts/camera_pipeline.py --help` 提供 `wipe`、`parallax`、`layered-vertigo`、`match-zoom`。它先生成资产、验证，再渲染；最终输出视频、JSON与同名 `.assets/` 目录。失败保留旧产物，默认不覆盖。

安装后可用相应效果技能中的 `scripts/pipeline.py`，子命令已固定。已有人工资产仍可交给原 `scripts/run.py`，不必重新准备。

## 视频mask与直边揭示

```bash
python3 scripts/motion_masks.py --input input.mp4 --output-dir masks \
  --start 2 --duration 1 --fps 30 --init-frame 15 --roi 0.4 0 0.2 1 \
  --mode reveal --reveal-direction right --require-complete-reveal
```

1. `init-frame`从所选区间0开始计数；ROI/种子必须对应这一帧，不是原片首帧。也可提供 `--initial-mask`。
2. 初始化分割后，DIS或Farnebäck稠密光流双向传播；前后向一致性、边缘与局部修正约束识别失效。反光、水纹、严重模糊或完全失去边界时应缩短片段、补初始化或拒绝。
3. `object/`为物体alpha；`reveal/`白色表示B。直边模式测量跨画面的边界，按方向转换揭示区域并检查覆盖、连续性与头尾；不把缺失测量补成虚假的完成转场。
4. `right`表示B从右侧显现，相应遮挡边界向左扫过；其他方向以实际 `--help` 和manifest为准。
5. `--require-complete-reveal`要求实测序列满足完整连接契约。曲线/复杂交叉边界可使用物体序列辅助人工修mask，不能强套直边模型。
6. 先叠加看mask与原片，再看转场。报告的流一致性仅为运动支持，不证明语义身份。

## 有限分层与背景补洞

```bash
python3 scripts/motion_layers.py --input photo.jpg --output-dir layers \
  --roi 0.3 0.2 0.4 0.6 --foreground-point 0.5 0.5 \
  --width 1280 --height 720 --foreground-shift-end 0.03 0
```

1. ROI与正负种子初始化静态分割，或提供显式alpha。轮廓仍需在实际图像上复查，特别是暗部、镂空和连通区域。
2. 根据前后景尺度、位移和锚点求整段会暴露的背景区域；修复只服务这个有限运动，不承诺无限移动。
3. 内置Telea/NS补洞；修补区域、条带宽度、暴露比例与允许运动写入manifest。大范围或复杂结构应补真实参考或另用修复工具。
4. 默认可在预算不满足时缩小相对运动并明确记录 `requested_motion`、`approved_motion`、`motion_factor`；严格保持请求使用 `--strict-motion`，不满足就失败。
5. 渲染必须消费 `approved_motion`，不能用原始强度重新暴露未经修复的像素。高层pipeline会执行这一步。
6. 输出foreground、alpha、background、repair-mask、QA图与manifest；检查前景完整、边缘色污染、重复纹理、补洞露馅和漂浮感。

## 自动匹配视觉锚点

```bash
python3 scripts/motion_match.py --input first.mp4 --input-b second.mp4 \
  --time 1 --time-b 0.5 --output anchors.json
```

1. 使用SIFT特征、双向ratio筛选和RANSAC单应性；验证内点数量、比例、空间覆盖、局部尺度与翻折。
2. 锚点由几何支持区域产生，JSON记录源像素变换与归一化锚点。重复窗格、昼夜纹理差异、非平面视差仍需实际核查。
3. 低纹理或几何不一致会拒绝，不静默回到画面中心。不同地点的形状匹配可明确提供两侧人工锚点。
4. 高层 `match-zoom` 在未提供anchors时执行自动匹配，保存匹配图与报告，再导出效果。

安装后本技能的 `scripts/assets.py masks|layers|match` 转发到对应模块；各子命令 `--help` 与仓库入口一致。非PATH的FFmpeg用 `--ffmpeg /absolute/path`。
