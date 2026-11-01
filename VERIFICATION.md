# MVP 验证记录

> 2026-10-20 当前交付验证见 [docs/DELIVERY_VERIFICATION.md](docs/DELIVERY_VERIFICATION.md)。本文件保留历史验证记录。

> 2026-10-19 audit note: this file is a historical 2026-10-06 snapshot. The current local
> suite is 346 tests; see `docs/UI.md` and `docs/IMPLEMENTATION.md` for current counts.


验证日期：2026-10-06。此文件记录工程验证，不是最终课程 benchmark 报告。

## 已完成

- 网络请求明确绕过系统代理。`network.py` 使用无代理 opener，实际模型和学术 API 请求都不读取 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 等环境变量。Cloudflare Wrangler 登录/部署也使用清除代理变量的独立命令。
- 按用户指定接入官方 `deepseek-flash`，使用 Chat Completions JSON Output，关闭 thinking 模式。
- 本机 `.env` 仅保存 `@pi:deepseek` 凭证引用，实际 key 不复制到代码/配置示例/运行日志。
- 真实 arXiv ID lookup：`2005.11401` 返回 `Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks`，使用精确 `id_list`。
- arXiv 真实搜索和 DeepSeek 分类/补搜流程已经跑通。
- OpenAlex 适配器在线冒烟：查询 `retrieval augmented generation evaluation`，返回两条带摘要的记录，1 次 HTTP 请求。
- 22 项自动化测试在 Python 3.14 和独立 Python 3.12 环境通过；包括本地 HTTP 服务验证 Ollama/OpenAI 请求和 token 统计。源文件指纹和文件检查见 [examples/verification_manifest.json](examples/verification_manifest.json)。
- 最后检查：18 个 Python 文件通过语法解析；项目文件中未发现实际 DeepSeek key；文档本地链接有效；CLI 的 satisfied/insufficient 离线场景分别按约定返回 0/2。
- 3 个离线场景覆盖首轮满足、条件补搜和结果不足；离线样例显式标注 demo。

## 真实端到端样例

命令（从 `scholarguide/`）：

```bash
python app.py run --request "推荐 5 篇与 RAG 相关的论文，其中至少 2 篇主要讨论评测或 benchmark。" --no-cache
```

已保存样例：`runs/deepseek_live_v3/result.json` 与 `report.md`。另有便于分享的副本：[examples/live_result.json](examples/live_result.json)、[examples/live_report.md](examples/live_report.md)。

观察到：

1. 首轮搜索返回 10 篇，模型检查推荐清单后只确认 1 篇符合评测类别。
2. 模型生成针对 evaluation/benchmark 的新查询。
3. 第二轮补充 10 篇候选，总共 20 篇。
4. 最终推荐 5 篇，模型标记的两篇评测/benchmark 分别为 MultiHop-RAG 和 MIRAGE 医学 RAG benchmark。
5. 报告含真实链接、摘要片段、缺口检查与执行轨迹。

本次耗时约 10.66 秒，2 次搜索 HTTP 请求、6 次 LLM 调用，记录输入 8,783 token、输出 1,865 token。上述为一次本机运行观测，不是稳定性能保证；分类和解释尚未经双人独立评测。

开发中保留了早期失败样例，分别显示结构化输出问题和模型误把综述算入 evaluation 的情况。随后将 JSON schema 明确传入 API 提示，并按当前请求类别提供单独定义。没有删除失败样例，也没有将它们作为成功结果。

## 评测脚本验证

已真实运行：

```bash
python evaluation/run.py --split dev --ids dev-01 --methods S A --out evaluation/results/deepseek_dev01
```

S 和 A 均返回五篇，并在这次结构化输入对应的首次检索后停止；A 不会为了演示强制补搜。耗时分别约 6.279 秒和 4.969 秒，均为 1 次搜索、2 次 LLM 调用。

结果位置：`evaluation/results/deepseek_dev01/`，包含：

- S/A 原始 JSON 与 Markdown；
- 任务文件 hash、模型及配置 manifest；
- 空白 `paper_labels.csv`、`reason_labels.csv`；
- `metrics.json`，质量分数仍为 null，资源使用已有实际记录。

这只是一条开发任务的工程冒烟。没有跑保留 test 任务来调参，没有捏造人工相关性或证据支持标签。

- 后续网页验证：新增 `python app.py web --open`，Python 标准库提供本机服务，静态 HTML/CSS/JS 调用同一 workflow。
- 浏览器（Edge/Playwright）实际验证：离线补搜演示和 DeepSeek 在线检索均返回五篇；执行轨迹可见；Markdown 下载成功；390px 手机宽度没有横向溢出；无浏览器脚本错误。截图位于 `runs/web_demo.png`、`runs/web_live.png`、`runs/web_mobile.png`。
- 当前共 25 项自动化测试通过，新增网页静态资源、后台任务/下载与并发提交限制测试。

## 原始 GPT Researcher 基线边界

- 实现了 `evaluation/upstream.py`：固定 commit 校验、原版研究/报告调用、固定推荐块解析、ID 核验、失败保存。
- 尝试在 `.venv-upstream`（Python 3.12）安装指定 commit；源码获取成功，但大量传递依赖下载较慢，安装在 600 秒预算内未完成。
- U 入口已实际运行并正确记录“上游包未安装”失败，位于 `evaluation/results/upstream_install_check/`；没有用 S 替代 U。
- 原版还需要 embedding 配置，DeepSeek 聊天 API 本身不能自动视为 embedding 服务。
- 因而当前**不能声称已经完成 U/S/A 的完整比较或证明架构瘦身优于原版**。可运行 MVP 与 S/A 评测已完成，U 的在线结果需在原版依赖和 embedding 就绪后补跑。

## 尚未完成的课程工作

- 第二台真实机器复现（已验证两个 Python 环境，不等于两台机器）。
- 团队审阅并冻结 4 dev + 12 test 的任务初稿。
- 人工标注/复核和保留测试集结果。
- 最终报告、展示与原版基线结果。

LSP 工具没有可用的 Python server（ty/ruff 均不可用），没有虚称 LSP 检查通过。若要启用，可安装对应 server 或调整 pi-lsp.json 的命令。已使用 Python 解析/导入和行为测试做实际验证。
