# 前端视觉与动效参考（2026-10-31）

本轮依据用户截图修正“深色悬浮导航与浅色阅读区割裂”，并重排首页来源模块。以下为实际查阅的官方页面；实现中的具体尺寸和时长是本站的设计选择，不是对这些网站动画参数的测量。

- [Linear：A calmer interface for a product in motion](https://linear.app/now/behind-the-latest-design-refresh)：导航降低视觉重量，正文优先；分隔结构应减少噪声，图标、边界与颜色共同服务信息层级。
- [Vercel Web Interface Guidelines](https://vercel.com/design/guidelines)：使用原生导航链接；动效优先 CSS 与 Web Animations API，优先 transform/opacity；保持焦点、可打断性与 reduced-motion 降级；使用一致色相和明确对齐。
- [Vercel Geist](https://vercel.com/geist/introduction)：以共享设计系统提供一致界面。
- [Stripe 官网](https://stripe.com/)：按能力分组的产品叙事结构。本站仅参考分组和信息层级，没有复制其品牌视觉或推断其动画实现。

## 本站落地

1. 导航与正文复用纸面主题 token，移除绿色浮岛、重装饰和口号；侧栏 236px，正文靠近导航排布，画布最大 1640px，主列与辅助列按可用空间调整。
2. 首页来源由三列长表改为 2×2 卡片：用途标题 → 来源标签 → 一句说明。窄屏单列，不把非交互卡片表现成按钮。
3. 控件反馈约 140–220ms，页面和结果入场约 260–320ms；首页下方模块只在首次进入视野时呈现约 380ms 的轻量过渡。
4. 所有导航保持原生链接行为，不拦截、不重放任务；页面入场与首页滚动呈现使用 CSS/Web Animations API。
5. 主题与语言图标有一次性反馈，原文和输入值保持不变；减少动态效果偏好会取消自有 Web Animations，并停用非必要 CSS 动效。
6. 实时轨迹列表不加入重复入场动画，避免轮询刷新时闪动。动效不表示任务进度或完成比例。

验证使用临时合成服务与浏览器 profile，未调用真实研究模型或学术来源。证据保存在 `runs/verification/cohesive-motion-20261031/`。
