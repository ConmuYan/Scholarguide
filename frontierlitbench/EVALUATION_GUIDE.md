# FrontierLitBench：统一评测与基线使用

本指南提供可以复用的任务文本、聊天产品导入和统一评分流程。**当前 A、B、C 语料及等级仍在复审，运行结果只能作为探索性结果，不能作为最终排行榜。** 相关缺口见 `review/track_ac_delivery_review_20261110.md` 和各 Track 的 `open_issues`。

## 评测什么

每个任务要求系统推荐并排序至多五篇符合主题、年份和会议要求的真实论文。Track A 为视频生成与编辑，B 为世界模型，C 为 LLM 智能体和工具使用。任务分别为：

| Track | 整体 | 2024 | 2025 | 2026 | 指定会议 |
|---|---|---|---|---|---|
| A | FB-01 | FB-04 | FB-05 | FB-06 | FB-13（CVPR、ICCV） |
| B | FB-02 | FB-07 | FB-08 | FB-09 | FB-14（ICLR、ICML） |
| C | FB-03 | FB-10 | FB-11 | FB-12 | FB-15（ACL、EMNLP） |

先确定相同的语料版本、评测时间窗口、统一补审表和运行方式。每个系统使用相同任务提示词；建议每任务独立运行三次，记录全部结果，不挑选最好的一次。记录产品及模型版本、运行时间、联网及工具设置、信息源、费用或额度、失败和中断。各产品可使用自己的工具，但产品级比较不等于只比较模型本身。Lune 是可使用的论文数据库来源；仅因为它参与过数据发现，不能直接断言它必然带来性能优势。

## 1. 生成统一任务

在仓库根目录运行：

```powershell
python evaluation/frontierlitbench_cli.py prompt --task FB-14 --json-format
```

替换任务编号即可生成其他任务。`--json-format` 增加输出格式要求；比较系统时请对所有系统使用同一个版本的提示词。提示词不包含 Gold 论文或等级，也不要把数据集或评分表上传给被测系统。

## 2. 运行 ScholarGuide

先配置好 DeepSeek 和需要的检索源。以下示例使用相同任务文本、Agent 模式及当前配置的多来源，禁用缓存以便记录独立运行：

```powershell
$env:SG_MAX_OUTPUT_TOKENS = '8000'
$fbPrompt = python evaluation/frontierlitbench_cli.py prompt --task FB-14 --json-format
python app.py run --request ($fbPrompt -join "`n") --method A --source multi --no-cache --timeout 300 --out runs/my-fb14-1
python evaluation/frontierlitbench_cli.py score --task FB-14 --result runs/my-fb14-1/result.json --out runs/my-fb14-1/score.json
```

每次更换输出目录。实际结果、运行状态和使用的模型与来源保存在 `result.json`；不要把失败或等待用户的运行当作成功结果。

## 3. ChatGPT 聊天模式及其他竞品

1. 打开一个新的聊天，记录产品、实际显示的模型、联网/搜索及工具设置。
2. 粘贴第 1 步生成的完整提示词，将完整原始回答保存为 UTF-8 文本，例如 `runs/chat-fb14-1.txt`。如果产品无法按要求输出 JSON，可人工忠实转录为一个 JSON 对象，保留原始回答作为证据；不要改变顺序或补写未提供的信息。
3. 将原文或转录结果导入并评分：

```powershell
python evaluation/frontierlitbench_cli.py import-chat --task FB-14 --response runs/chat-fb14-1.txt --system "ChatGPT, 实际模型版本" --tools "网页搜索开启" --out runs/chat-fb14-1.result.json
python evaluation/frontierlitbench_cli.py score --task FB-14 --result runs/chat-fb14-1.result.json --out runs/chat-fb14-1.score.json
```

导入器接受单个 JSON 对象、JSON 数组或一个 JSON 代码块；对象需有按推荐顺序排列的 `papers` 数组。每项至少提供 `title`；建议同时保留 `rank`、`authors`、`year`、`venue`、`doi` 或 `url`、`reason`。显式排名和数组顺序不一致时会报错，不会自动重排。导入结果会保留读取到的完整原文。对于人工转录，请另外保存未经转录的原始回答。竞品只要转换为相同格式即可使用同一评分入口；不要在导入时纠正、筛掉或补齐模型的推荐。

## 4. 补审与指标

评分器使用 DOI、arXiv 标识、规范化标题及年份匹配；冲突或库外论文保持待审。各系统的库外论文应先合并去重、统一复审，再对所有系统使用同一补审文件：

```powershell
python evaluation/frontierlitbench_cli.py score --task FB-03 --result runs/example.result.json --adjudications frontierlitbench/review/trackc_supplemental_adjudications.json --out runs/example.score.json
```

未解决的库外论文不会自动记零，评分结果的 `status` 为 `pending_adjudication` 且 `metrics` 为空。补审表示例中的采集时间晚于原始 Gold 快照，且本身尚未完整，因此仅供当前探索使用。重复推荐、缺失位置或经核实不符合条件的论文才按规则获得零增益。

默认使用仓库内历史语料。评估另一个复核版本时，可在 `score` 命令中添加 `--corpus 路径` 指定 JSONL 文件。评分报告会记录所用路径；比较系统时须指定同一语料版本和同一补审表。候选语料在复核完成前仍属于探索版本。

已完成的评分包括 nDCG@5（线性增益 S/A/B = 5/3/1）、WeightedHit@5 和 S-Tier Hit@5。汇总前确认每次运行成功、各系统使用相同语料和补审表，并分别报告每个任务、重复运行以及待审和失败数；不能从不完整结果宣布最终排名。

对同一个系统、同一 Track 的全部评分文件，可以运行汇总（示例中的变量应包含该 Track 五个任务、每个任务三次的全部评分文件）：

```powershell
$scoreFiles = @('runs/example-fb03-1.score.json', 'runs/example-fb03-2.score.json') # 替换成全部 15 个路径
python evaluation/frontierlitbench_cli.py summary --track C --repeat 3 --scores $scoreFiles --out runs/example-track-c-summary.json
```

只有各任务均有指定次数且评分完成时，汇总才计算每个任务的平均值和 Track 宏平均 nDCG；缺少运行、失败或存在待审论文时，宏平均为 null。按系统分别运行汇总，不要把不同产品或模型的结果混在一起。报告还应给出每个任务的可用 S 级论文数量，不能仅凭 S-Tier Hit 比较不同难度的任务。

当同一系统的三个 Track 汇总均完成后，计算三个 Track 宏平均的等权平均：

```powershell
python evaluation/frontierlitbench_cli.py overall --summaries runs/track-a-summary.json runs/track-b-summary.json runs/track-c-summary.json --out runs/overall-summary.json
```

任一 Track 不完整时 Overall 为 null。汇总要求模型和工具设置一致；在不同时间或额度条件下运行时，也应在实验记录中说明。
