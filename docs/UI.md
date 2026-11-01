# 界面与交互

> 本文包含历史界面设计，当前产品行为以 [开发指南](DEVELOPMENT.md)、[API 文档](API.md)和实际代码为准。最新工程验证见 `runs/verification/`。

状态：Search、Chat、Activity、Sources、Models 五页共用 app shell，零依赖原生 HTML/CSS/JS。
首次使用区分 Live/固定 Demo，示例只填草稿；输入、协作停止、结果阅读、引用导出和模型配置均有真实浏览器回归。

## 当前语言切换与首页（2026-10-31）

- 六个页面在主题按钮旁提供语言图标按钮，按简体中文 → 繁體中文 → English 循环；角标显示简／繁／EN，原生按钮支持 Enter/Space，提示中说明当前语言和下一语言。
- 语言保存在 `localStorage['scholarguide-language']`；同源标签页通过 storage 事件同步。存储不可用时只在当前页面生效。主题偏好独立保存。
- 本地页面字典由 `theme.js` 读取，只更新明确标记或由前端登记的界面节点。语言切换不触发模型请求、不修改提交内容，不翻译论文、用户输入、引用或模型生成的研究内容。未知服务端自由文本保留原样。
- 首页压缩重复说明，保留一处“研究流程演示 · 虚拟样例”标识；研究说明继续注明摘要依据与待验证假设。
- 首页来源标识持续循环，已移除暂停按钮和悬停暂停交互；系统减少动态效果偏好仍适用。工作流程演示的独立播放控件保持原有行为。
- 五页侧栏与正文复用纸面主题，线性图标与说明文字展示检索、对话、活动三条研究路径；来源和模型使用双格配置入口。当前页使用克制的砖红色强调，已移除深色浮岛、装饰图形、彩色圆点和仿账号卡。
- 首页来源能力区为 2×2 卡片：用途标题、来源标签、一句说明，窄屏单列。
- 页面/结果轻量入场、详情展开和控件按压提供可打断的反馈；首页下方模块首次进入视野时呈现，支持原生跨页 View Transitions 的浏览器提供导航过渡。所有非必要动效支持 reduced-motion。参考与实现说明见 [前端设计笔记](FRONTEND_DESIGN_NOTES_20261031.md)。
- 顶栏不再重复显示模块胶囊；保留移动菜单、模型状态及主题和语言工具。
- 布局回归覆盖 320/390/1024/1440 px、三语言和浅深主题；这些是离线浏览器验证，不代表真实设备或屏幕阅读器认证。

## 1. 布局与 token

左侧为 236px 纸面导航，顶栏提供模型状态与主题/语言控制，正文画布最大宽度 1640px 并靠近导航对齐。主列与辅助列按空间分配，正文段落保留阅读宽度；移动端导航为抽屉。导航使用线性图标与双格配置入口。

| token | 亮色 | 暗色 | 用途 |
|---|---|---|---|
| `--bg` | `#faf9f6` | `#121211` | 页面底 |
| `--side` | `#f7f6f2` | `#171716` | 辅助面板 |
| 侧栏 `--nav-bg` | `var(--side)` | `var(--side)` | 与正文同色系的导航底色 |
| `--card` | `#ffffff` | `#1c1c1a` | 卡片/输入 |
| `--ink` | `#1b1b1b` | `#f2f0e9` | 正文/主按钮 |
| `--muted` | `#77746c` | `#a29d93` | 说明 |
| `--line` | `#e7e3da` | `#2b2b27` | 1px 边 |
| `--brick` | `#a44a36` | `#d0836a` | 证据边/异常 |
| `--serif` | Iowan / Baskerville / Songti | 同左 | 标题、数字 |
| `--sans` | Inter / PingFang / YaHei | 同左 | 正文/控件 |

主题：顶栏按钮循环 Light → Dark → System，写入 `localStorage['scholarguide-theme']`，`theme.js` 处理。
组件：`.card`、`.side-link`、`.seg`、`.pill-field`、`.chip`、`.badge`、`.notice`、`.metric`、`.paper`、`.turn`、`.act-*`。
四页共用 `style.css`，无外部字体/CDN。

## 2. Agent 轨迹浏览（`/activity`）

- 顶部统计卡（Sessions / Runs / Events / Completed）使用全量 totals，不把列表的 100 条上限当成总数。
- 会话/运行列表显示标题、模式、来源、轮次、事件数；支持按标题/消息/ID 搜索与 Live/Demo 过滤。
- 会话详情显示每轮历史（消息、状态、论文数）；操作决策与工具调用分开（`decision` vs `search/assess`），旧 DB 的混淆事件在读取时归一化。
- 左侧列出本机 `runs/` 里的**会话**与**单次运行**，显示标题、状态、模式/来源、事件数、轮次、时间和成本；标题来自最近一轮消息/任务。
- 右侧先展示 **Execution graph** 与 **Trace tree**：span 按 `PROPOSAL / CONTRACT / NODE / TOOL / GATE / ERROR` 分型，点击行在右侧 inspector 查看输入、输出、合法动作/预算/证据与 raw JSON；原始事件流默认折叠。
- 顶部可按动作组筛选（任务/检索/评估/复用/交付/澄清/异常），并显示事件计数。
- 检索事件显示每个来源的状态、`elapsed_ms`、`cost_cny`（DeepSeek 按成功请求计）与 router action cost；多源失败不会只留一条笼统错误。
- 会话详情同时显示 TaskState（主题、用途、类别、K、排除、超范围）与 settled/reserved 成本；单次运行显示 mode/method/论文数/耗时。
- Activity 的读取通过 `activity.py` 以 `mode=ro` 打开 SQLite。页面提供本地记录删除入口，但当前后端为保护跨进程数据安全而拒绝删除（503）；不会删除文件，用户会看到原因。

## 2.1 Agent trace（proposal vs. contract）

完整说明见 `docs/AGENT_TRACE.md`。核心展示原则：

- LLM 的 TaskState patch、解析计划 = **proposal**；trace tree 用类型和 inspector 展示，不把隐藏 CoT 当事实。
- `allowed_actions`、预算、证据门、交付拒绝 = **contract**；`invalid_action` 是树中的 ERROR span，不是被淹没的日志。
- 执行图与 span 树区分 single-pass Search workflow 和 stateful LangGraph session。
- LangGraph 会话显示 `planner/decide/retrieve/assess/critic/verifier/finalize` spans；导入 Search 会话显示原始 trace 作为 IMPORT span。

## 2.2 Provider settings（`/settings`）

- 左侧 profile 列表显示 env / custom、协议、模型、key 状态和价格；右侧编辑 display name、协议、base URL、model、API key、输入/输出单价、max output tokens、currency。
- `Test connection` 由服务端发起一次有界结构化探测；结果区分 `auth / model_or_endpoint / timeout / protocol / error`，显示 HTTP 状态、耗时、模型与 usage；不写任务历史。
- API key 只写不读，页面只显示 masked hint；env profile 作为 DeepSeek/deepseek-flash fallback 可在列表中 **Activate**，但其字段只读，Save/Delete 仍禁用；自定义 profile 可通过 New 创建。
- active profile 影响下一轮 Search/Chat；成本显示在 Search metrics、Chat turn foot、Activity header 与 settings probe。
- 未保存的元数据草稿按标签页恢复；API key 不写入草稿存储，刷新后需要重新填写。探测/保存期间阻断并发修改。
- env profile 的只读限制同时覆盖保存函数与键盘快捷键；自定义远程免密 profile 可以就绪，连接是否可用仍由显式探测确认。

## 2.3 Chat capability / archive（2026-10-19）

- Chat 顶栏显示 active profile + model（两行，title 补 protocol/profile id），打开、focus、visibility 时刷新 `/api/config`。
- `#budget-status` 显示反馈预算剩余/耗尽；耗尽时禁用 composer 与诊断 retry，保留当前结果，提供归档和 New session。
- 诊断区显示 source health（如 `venue_resolve / bot_challenge / rate_limited / timeout`）与 near misses；`new_session` 调用 `/fork` 保留请求并选择 source。
- `归档` / `恢复` 只改 `meta.json` 标记；不删除 turn 文件或 SQLite 证据；恢复不重置预算。历史列表的删除入口目前被后端安全停用，不能当作已实现的删除功能。
- `POST /messages` 在反馈耗尽时返回 409，不产生 no-op turn；运行中重复 POST 返回 409。

## 2.4 Failure diagnosis（`diagnosis.py`）

- `no_results` / `partial` 结果现在附带 `diagnosis`：`constraints_unmet / quota_unmet / insufficient_candidates / source_degraded / budget_exhausted / requirements_unmet`。
- Chat 在每轮结果中显示真实原因、provider-level 检索异常、未交付的 near misses，以及“不限年份 / 不限 venue / 目标降到 N 篇”等用户确认后才执行的动作。
- Search 结果显示同一 diagnosis；旧结果没有该字段时自动降级为原提示。
- `venue='CVPR 2026'` 在 Request 边界拆分为 `venue='CVPR'` + `year=2026`；Chat 读取旧任务时也做规范化。
- 不自动放宽硬约束；宽松动作必须由用户点击或新请求触发。


## 3. 交互

- **运行进度**：单轮页 `#stagebar` 四段（理解 → 检索 → 检查 → 结果），随 trace 的 `plan/search/assess/check` 推进；Chat 创建/发送使用 `stream=true` 后台 turn + `/events?after=` 轮询，running 条显示 graph 节点（planner/retrieve/assess/critic/verifier），完成后再渲染结果。
- **阅读目标**：Live 表单支持 Auto / 新手入门 / 方法对比 / 前沿 idea / 通用；结果卡显示 survey/benchmark/research 徽标，coverage 显示 mix；高级设置保留显式 category quota。
- **通用主题约束**：Live 表单支持主题、Venue（如 CVPR）和年份区间；多源模式下 OpenAlex 按 venue source id + publication_year 过滤，结果再经 venue/year 硬过滤。Demo 仍使用固定 RAG fixture，不冒充通用实时结果。
- **会话历史**：多轮页左侧列出每轮序号、消息摘要、状态与论文数；点击平滑跳转到对应轮。会话 id 可显示并一键复制。
- **证据卡片**：长引文默认两行截断，可展开/收起；复制完整实际作者、标题、年份和定位链接；支持 Markdown/JSON/BibTeX 与逐轮导出。`引文已校验` / 类别徽标区分状态。
- **信源透明**：结果卡显示来源徽章、venue、`OA 全文` 与 `DOI` 链接；`仅元数据` / `来源摘要不一致` 徽标提示不可用或冲突证据。多源结果出现来源筛选条（全部 / arXiv / OpenAlex / …）。
- **覆盖状态**：结果上方显示「已交付 n/K · 类别 quota」，有缺口时转为琥珀提示；搜索失败显示表单错误和「重试」，无需重新输入。
- **多轮状态**：ASK 时 `#question` 与状态条切到 awaiting_user，输入框提示回答；每轮 footer 显示 `cost` 与 reuse 的 `reused/(reused+stale)`；失败轮可直接「重试本轮」。
- **Agent 轨迹**：Chat 每轮结果下方有「Agent 轨迹」折叠块，默认折叠并按需加载；运行时 running strip 实时追加 `agent_node / decision / search / assess` 事件。左侧「轨迹」和折叠块内链接都跳 `/activity?session=<id>` 查看完整审计。
- **多源会话**：Chat composer 的 `Source` 可选 arXiv / multi / OpenAlex / S2 / DBLP / OpenReview / ACL / DeepSeek；首轮创建后锁定，`ChatService._public` 保留 DOI/OA/metadata/conflict，卡片不再生成空 DOI 链接。Saved sessions 从磁盘列出，点击可恢复；`轨迹` 跳到 `/activity?session=<id>`。
- **状态文案**：空会话、等待澄清、partial、unsupported、no_results、failed 各有提示与下一步建议。
- **演示深链**：`/?demo=followup`（单轮）与 `/chat?demo=ask`（多轮 ASK 恢复）自动跑离线 fixture，方便演示与截图。
- **输入与续接**：中文 IME 不误发送；Chat Enter发送、Shift+Enter换行，Ctrl/Cmd+Enter发送；草稿随会话保存，失败保留。当前任务来自服务端实际解析字段；后续建议先填草稿。
- **停止**：Search/Chat 可协作停止，保留已验证结果；在途请求完成并记账后不再启动下一个动作，页面持续轮询同一任务直至停止结果。

## 3.1 功能切换与结果归属

- Search 切换 Live / Demo、改变检索条件或演示场景时，立即清空当前结果及轨迹，显示当前模式的待运行提示；不会自动发起在线搜索。旧任务及其结果仍保存在服务端，可在 Activity 查看。同一模式且条件未变时，刷新可恢复已完成结果。
- 需要显式恢复的中断搜索在切换时不会自动重试；当前浏览器标签页保留「查看中断任务」入口，刷新后可重新打开恢复提示。索引存于 `sessionStorage`，关闭标签页后的可用性取决于浏览器；任务记录不因此删除。
- Sources 切换配置对象时加载该对象自身的值；如有未保存的修改会先确认，取消则保留当前草稿，确认则放弃。密钥不会带到其他来源。来源测试查询或 Lune 工具参数变化后不再展示旧查询结果；延迟返回的旧响应不会覆盖新上下文。切换 Lune 工具时收起其他工具面板。
- Models 修改配置或切换 profile 后隐藏旧连接测试结果；Chat 切换会话时先显示读取状态，不沿用上个会话的任务与澄清问题；Activity 筛选期间隐藏旧详情并停止跟踪不再选中的记录。

## 4. 响应式与可访问性

- 断点：1150 / 920 / 740 / 430px；`920px` 以下侧栏变为抽屉（顶栏 ☰），内容单列。
- 最终28个场景均 `scrollWidth == clientWidth`；覆盖四页亮暗、1440/1024/390和首次使用。另对390px Search/Chat正文列宽作真实几何断言，避免无溢出但正文被压窄。
- 主题 Light/Dark/System 持久化，OS 主题变化在 system 模式跟随；移动端 390px 抽屉由 ☰ 打开。
- `:focus-visible` 砖红描边；表单有 label；状态区 `aria-live`；`prefers-reduced-motion` 关闭动画；主题token有静态对比度检查。尚未做完整WCAG、屏幕阅读器或移动真机认证。
- CSP 下无内联样式/脚本，资源全部同源。

## 5. 截图

当前可重复验收使用 `python scripts/verify_delivery.py --require-browser --screenshots --out runs/verification/NEW-DIRECTORY`，
在新目录生成全量测试与28张截图，不需要安装 puppeteer。下表和 `screenshot_ui.mjs` 记录早期截图路径。

| 页面 | 1440 | 1024 | 390 |
|---|---|---|---|
| 单轮 `/` | `docs/ui/index-1440-full.png` | `index-1024-full.png` | `index-390-full.png` |
| 多轮 `/chat` | `docs/ui/chat-1440-full.png` | `chat-1024-full.png` | `chat-390-full.png` |
| Chat follow-up Agent 轨迹 | `docs/ui/chat-followup-trace-1440.png` | — | — |
| Chat 失败诊断 / source health / recovery | `docs/ui/chat-recovery-1440.png` | — | `docs/ui/chat-recovery-390.png` |
| Chat active model + feedback exhausted | `docs/ui/chat-active-model-exhausted-1440.png` | — | — |
| ASK 状态 / 恢复 | `docs/ui/chat-ask-1440.png` / `chat-ask-resume-1440.png` | — | — |
| 轨迹 `/activity` | `docs/ui/activity-1440-full.png` | `activity-1024-full.png` | `activity-390-full.png` |
| 暗色主题 | `docs/ui/index-dark-1440.png` | — | — |
| Trace tree · GAD live | `docs/ui/activity-gad-1440.png` | — | — |
| Trace tree · demo follow-up | `docs/ui/activity-trace-demo-1440.png` | — | — |
| Trace tree · imported session | `docs/ui/activity-session-story-1440.png` | — | — |
| Search → Chat 统一历史 | `docs/ui/chat-search-history-1440.png` | — | — |
| Chat 打开 GAD 导入会话 | `docs/ui/chat-gad-import-1440.png` | — | — |
| 过期 session 自动重置 | `docs/ui/chat-stale-1440.png` | — | — |
| CVPR 3D live smoke | `docs/ui/cvpr-live-1440.png` | — | — |
| 阅读目标表单 | `docs/ui/index-intent-1440.png` | `index-intent-1024.png` | `index-intent-390.png` |
| 模型设置 `/settings` | `docs/ui/settings-1440.png` | — | — |

截图由本机 Chrome + puppeteer-core 生成（仅开发工具，不进入应用依赖），视口 1440×1000 / 1024×1000 / 390×1000，`fullPage`。重新生成：

```bash
# 可选：仅截图需要
npm i puppeteer-core
node scripts/screenshot_ui.mjs
```

脚本会输出 6 张全页图，并在出现横向溢出时打印 `overflow`。

## 6. 验证

```bash
python -m unittest discover -s tests   # 2026-10-20：441 tests，含56项真实Chrome流程
```

`test_ui_static.py` 检查共用 token、必需 ID、provenance hooks、无外链/内联、a11y、对比度、截图；`test_ui_browser.py` 用 Selenium 实测引文/引用复制、展开、缺引文、chat 徽标/历史、ASK 恢复、来源筛选/覆盖、主题持久化与移动抽屉。

## 7. 已知边界

- Light/Dark/System 三态；系统字体栈，不加载 webfont。
- 截图脚本不是应用依赖，未提交 puppeteer 配置；PNG 为验收产物。
- 多轮 running 条实时追加 `/events?after=` 收到的 `agent_node / decision / search / assess` 事件；完成后的每轮轨迹从同一持久化事件流按 revision 过滤。
- 多源结果仍可能缺摘要（DBLP/DeepSeek），UI 会标 `仅元数据`，不会把网页 summary 当论文摘要。

## 4.5 Search → Chat 统一历史（2026-10-19）

- 每次 UI Search 完成时，`Jobs` 调用 `ChatService.import_run()`，自动创建一个 `origin=search` 的 Chat session。
- 导入首轮是原搜索的 5 篇结果；候选、评估、TaskState、query 和 delivered 事件一起落库，follow-up 优先复用已有判断。
- Chat 列表同时列出 `origin=chat` 和 `origin=search`，标题优先用 `display_title`，并显示 `Search/Chat · status · mode · turns · source · 本地时间`。
- GAD live 与最新 demo 已手动导入；Search 结果页有「在 Chat 继续 →」直接打开对应 session。
- 所有 Search 都自动导入（demo 和 live）；测试使用临时 `--runs` 目录，不会污染真实历史。

## 5. 历史清理与时间口径（2026-10-19）

- Activity 历史已清理：只保留 `57f79127…` 的 GAD live run 和最新一条 demo run（`75339e…`）；其余 908 条 run/会话目录移动到 `runs/_archive-20261019-history/`，附 `MANIFEST.json`，可恢复。
- 保留项命名：`Live · GAD × diffusion / flow matching`、`Demo · RAG evaluation walkthrough`；筛选/搜索仍可用原始 topic。
- 时间统一为**浏览器本地时间**（当前 Asia/Shanghai，UTC+8）；后端保留 UTC 时间戳，列表和事件详情不再出现 07:52/08:17 混用。
- Session 历史清空后，Activity 默认落在 Runs 标签，避免打开就是空 Sessions。
- Chat 的 localStorage 若指向已清理/不存在的 session：后端返回 404，前端自动清除旧 sessionId、隐藏旧轨迹链接并切回「新会话」，不会反复报错或继续向已删除会话发送。

## 8. UI 打磨层（2026-10-23）

`web/style.css` 末尾新增一层只改表面的样式：统一的半径与阴影尺度、侧栏当前项竖条、按钮按压手感、
输入框聚焦外环、行与卡片悬停底色、统一 `:focus-visible`，以及整层的 `prefers-reduced-motion` 降级。
行为、id、数据流均未改动。`web/landing.css` 另有同性质的一层（hero 底纹、路径卡箭头位移、结尾面板）。
侧栏与页面标题同步中文化（检索 / 对话 / 模型 / 活动）。细节见 `runs/uiux-20261023/UIUX_REPORT.md`。
