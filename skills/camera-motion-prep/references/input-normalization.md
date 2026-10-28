# 输入规范化：时间、颜色与几何

以下为可执行准备方法，按实际输入选用；不会由 `analyze` 或普通运镜命令自动完成。

## 检查帧时间与颜色

可用FFprobe时先取流信息与逐帧时间：

```bash
ffprobe -v error -select_streams v:0 -show_entries stream=width,height,sample_aspect_ratio,r_frame_rate,avg_frame_rate,pix_fmt,color_space,color_transfer,color_primaries -of json input.mp4
ffprobe -v error -select_streams v:0 -show_frames -show_entries frame=best_effort_timestamp_time -of json input.mp4 > frame-times.json
```

相邻有效时间戳差值应接近目标帧间隔，并考虑时间基量化。`avg_frame_rate == r_frame_rate` 不能单独证明全段CFR；分析实际使用区间，记录缺失/重复或不均匀时间戳。

只有FFmpeg时可完整解码并查看showinfo的 `pts_time`，日志保存在本地：

```bash
ffmpeg -hide_banner -i input.mp4 -map 0:v:0 -vf showinfo -an -f null - 2> frame-times.log
```

此过程只诊断，不自动产出VFR结论。查看有效显示方向、像素宽高比和实际首帧，不能只相信容器宽高。

## 已知SDR输入转CFR

```bash
ffmpeg -n -i input-sdr.mp4 -map 0:v:0 -map '0:a:0?' -vf fps=30 -fps_mode cfr -c:v libx264 -crf 18 -pix_fmt yuv420p -c:a aac -b:a 192k working-sdr-cfr.mp4
```

- 替换fps与文件名；`-n`避免覆盖已有文件。这里只做时间采样与编码，输入须已是可用SDR，不能拿它替代HDR色调映射。
- CFR转换可能丢帧或重复原帧，不是光流插帧，也不保证运动更流畅。保留原片与工作片对应关系，事件用工作时间轴重新标定。
- 音轨按输入时间保留并重编码；原始时间戳存在断裂、音频起点不同或漂移时另做校正，导出后核对首尾同步。
- 有非方形像素或需画幅变换时先明确几何缩放，不随意只写 `setsar=1` 改变显示比例。

## HDR转SDR

仓库现成入口：

```bash
python3 scripts/apply_camera_motion.py convert-hdr --input input-hdr.mov --output working-sdr.mp4 --duration 4 --width 1920 --height 1080 --fps 30 --peak 10
```

命令使用显式Hable映射，并会按输出画幅等比放大裁切；它不是保持源几何不变的格式转换。`--peak`是相对SDR白的设计参数，应根据源内容检查；不要将默认值当测得峰值。未识别到HDR标记时命令会拒绝转换，不能据此判定文件一定是SDR。输出默认静音。

检查高光、暗部、肤色与文字，并确认没有对已转换SDR做二次映射。HDR标记缺失/错误时需先诊断实际色彩，不能删标签强行绕过检查。

## 交给CV工具前

1. 固定工作片版本、显示方向、尺寸、颜色与fps。
2. 在工作片上重新选择ROI、锚点与mask时间；原始视频第N帧可能不等于工作片第N帧。
3. 若外部mask/depth/flow基于代理图，记录缩放与时间对应；恢复到工作坐标后叠加检查。
4. 检查工作片完整解码和实际持续时长，再启动稳像/跟踪/转场。不要用元数据标成30fps代替真正的时间采样。

参考：[FFprobe文档](https://ffmpeg.org/ffprobe.html)、[fps滤镜](https://ffmpeg.org/ffmpeg-filters.html#fps-1)、[FFmpeg帧同步选项](https://www.ffmpeg.org/ffmpeg.html)。
