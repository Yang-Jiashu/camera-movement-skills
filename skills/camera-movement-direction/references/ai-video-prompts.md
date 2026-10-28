# 把运镜转成 AI 视频提示词

用于文生视频、图生视频的镜头方向设计。这里不绑定具体供应商，也不内置付费调用、上传或发布流程。

## 先定义能看到的运动

把“大片感”“高级感”展开成机位、主体和环境的可观察变化。图生视频以输入图为起点，优先描述发生的运动；文生视频还需建立主体、场景和初始构图。

可以采用以下组织方式，不必机械填满：

`起始景别/视角 + 相机动作/方向/速度 + 主体动作 + 终点看到什么 + 必要的连续性要求`

- 明确“相机绕主体”与“主体自己旋转”，以及“机位前移”与“镜头变焦”。英文术语加一句物理动作比堆叠风格词更清楚。
- 优先构造一个核心动作的可验证版本。复合运动需说明先后与各阶段看到什么；用户明确要求时保留复合设计，不武断宣布模型不支持。
- 稳定建筑可描述为“rigid architecture with consistent geometry”；静镜可描述为“locked-off camera”。通用主提示词优先正向描述；负面提示词只在目标工具当前支持时使用，单独列出。
- 如果首帧是单张图，大幅环绕会涉及未被记录的背面；可以缩小弧度、补充参考视图或采用可控制的三维路线。明确新增画面由模型推断，不把它当作原场景的真实记录。
- 时长、角度、轨迹精度、首尾帧和参考运动能否被控制，取决于具体接口。提示词里的精确数字只是设计目标；需要精确复现时优先查验是否有轨迹、关键帧、参考运动或三维控制能力。
- 用户指定 Runway、Veo、可灵、海螺、Seedance 等时，核查相应当前官方文档后调整。不要把某家某一版本的限制扩写成全平台规则，也不要凭记忆发明 JSON 字段。

## 原创示例

以下英文描述供可读复制使用，不声称英文始终优于中文，不保证一次生成命中。时长在实际工具中另行设置，并选用其支持值。

### 静态建筑：克制横移，观察空间层次

中文意图：从近处栏杆旁缓慢右移，让栏杆与远处建筑产生视差，终点完整看到入口。

> The camera slowly trucks to its right past the foreground railing. The building stays stationary, with its entrance coming fully into view. Foreground elements pass across the frame faster than the distant facade. Smooth motion, level horizon, rigid architectural geometry.

检查：运动来自机位，建筑没有伸缩；若单图无法支持足够新视野，缩小移动范围。真实入口外观不能靠提示词验证。

### 人物：后方跟随进入街巷

中文意图：人物向巷内走，相机从后方同步前移，保持中景和相对距离。

> A stabilized camera follows behind the person as they walk into the narrow street. The camera moves forward at the same pace, maintaining a medium rear view and a steady distance. Shopfronts pass naturally along both sides.

检查：人物确实行走；相机没有只变焦；步态、身份、店面结构和遮挡连续。

### 情绪变化：希区柯克变焦

中文意图：人物站定，相机后退并同步拉近焦距，主体占比近似不变，空间关系改变。

> The person stands still. The camera dollies backward while the lens zooms in, keeping the person's head at approximately the same size and position in the frame. The background changes scale relative to the person, creating a controlled dolly-zoom effect.

检查：主体未被拉伸；背景变化来自相对视点与视场变化，而非整幅缩放或建筑变形。失败时先减小幅度并明确主动作。

### 水景：相机静止，场景运动

中文意图：维持构图，只保留水流和远处列车的运动。

> Locked-off wide shot. The waterfall flows continuously, and the distant train travels along its existing track. The camera remains completely still throughout the shot. The architecture and framing stay consistent.

检查：有水流和列车动作而没有镜头漂移；参考图若只有静止列车，生成的行进属于合成内容。

## 交付与迭代

用户只需要提示词时，交付中文意图、可复制提示词和决定成败的一两项条件即可。多镜头按镜头分别交付，不默认把所有段落塞进一次生成。

实际生成后先检查运动是否正确，再评价风格。记录偏差属于机位动作、主体动作还是几何连续性，每次优先修改一个主要问题；不无限重试。若当前环境没有合适的视频工具，交付提示词与镜头规格，准确说明尚未生成。

## 当前文档入口

核查日期：2026-10-28；具体功能在执行前按目标平台复核。

- [Runway · Image to Video Prompting Guide](https://help.runwayml.com/hc/en-us/articles/48324313115155-Image-to-Video-Prompting-Guide)：以图像建立起点、以文字描述运动；复杂动作可用顺序描述。
- [Runway · Camera Terms, Prompts, & Examples](https://help.runwayml.com/hc/en-us/articles/47313504791059-Camera-Terms-Prompts-Examples)：摄影术语和静镜描述示例。
- [Google DeepMind · Veo prompt guide](https://deepmind.google/models/veo/prompt-guide/)：主体、场景与镜头语言的组织。
- [Google Cloud · Video generation prompt guide](https://cloud.google.com/vertex-ai/generative-ai/docs/video/video-gen-prompt-guide)：运动和视角描述，及部分高级效果的可变可靠性。
