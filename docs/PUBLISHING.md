# 把当前项目发布成公开 GitHub 仓库

当前工作目录就是 ScholarGuide 项目本身，但它同时保存了**不能公开**的本地材料：`.env`、`runs/` 运行历史、课程 PDF、`.pi`/`.codex` 个人 agent 配置、`.cache`、旧 Cloudflare 实验目录，以及部分含个人路径的内部文档。

因此不要直接 `git init && git add .`。发布分两步：**导出干净仓库** → **推送并打 tag**。

## 1. 导出公开源码树

```bash
python scripts/build_source_repo.py --out /tmp/scholarguide-public
```

导出器行为：

- 只按白名单收录源码、`web/`、`tests/`、`scripts/`、`examples/`、`vendor/`、`install/`、`.github/`、`evaluation/`、公开文档；
- 丢弃 `.env*`（保留 `.env.example`）、`runs/`、`archive/`、`cloudflare-*`、`.pi`、`.codex`、`.cache`、PDF、`*.sqlite*`、日志、`NUL`；
- 丢弃 `examples/live_result.json` 等真实 provider 输出；
- `docs/` 默认只发布公开文档集合；加 `--all-docs` 才会带上历史内部记录（含个人路径，不推荐）；
- 扫描凭证形态字符串和开发者主目录路径，命中则**删除导出并失败**；
- 写入 `SOURCE_MANIFEST.json`（文件数与逐文件 SHA-256）。

本次导出结果为 354 个文件、约 5.3 MB，扫描零命中。

## 2. 写入仓库名

创建 GitHub 仓库后，在导出目录执行一次：

```bash
python scripts/set_repository.py --repository YOUR_NAME/scholarguide
python scripts/set_repository.py --repository YOUR_NAME/scholarguide --check
```

它把 README、安装文档里的 `OWNER/REPOSITORY` 替换成真实 slug。重复导出会重置为占位符，需要再执行一次。

## 3. 推送

```bash
cd /tmp/scholarguide-public
git init -b main
git add .
git commit -m "ScholarGuide v0.1.0"
gh repo create YOUR_NAME/scholarguide --public --source=. --remote=origin --push
git tag v0.1.0
git push origin v0.1.0
```

没有 `gh` 也可以在网页创建空仓库后：`git remote add origin git@github.com:YOUR_NAME/scholarguide.git && git push -u origin main`。

## 4. Tag 之后自动发布安装包

`.github/workflows/release.yml` 在推送 `v*` tag 时：

1. 安装 `requirements.txt`；
2. 运行 `tests/test_release_package.py`（构建白名单 + 解包 + 离线 Demo）；
3. 用 `${{ github.repository }}` 与 tag 生成确定性的源码包、`install.sh`、`install.ps1`、`SHA256SUMS`；
4. 以该 tag 创建 Release 并挂上四个资产。

之后组员即可使用：

```bash
curl -fsSL https://github.com/YOUR_NAME/scholarguide/releases/download/v0.1.0/install.sh | sh
scholarguide web --open
```

Windows：

```powershell
irm https://github.com/YOUR_NAME/scholarguide/releases/download/v0.1.0/install.ps1 | iex
```

## 5. 发布前必须人工确认

- **第三方来源标识**：`web/assets/logos/` 是 arXiv、OpenReview、OpenAlex、Semantic Scholar、DBLP、ACL Anthology、Lune 的官方标识，归各自权利人所有，不在 MIT 授权范围内。公开再分发前确认其条款，或从导出中移除该目录并调整落地页。
- **许可证**：仓库根目录已放 MIT `LICENSE`，`vendor/gptr_arxiv.py` 保留上游 Apache-2.0，说明见 `NOTICE.md`。
- **文档时效**：`docs/DEVELOPMENT.md` 提到若干未导出的历史文档；它们只是历史索引，不影响运行。
- **密钥**：仓库与 Release 均不含任何真实 key。组员安装后需在 Models 页面自行填写；`Live` 调用会消耗其自己的模型/来源配额。
- **未验证项**：本机未验证真实 PyPI 依赖安装（安装器机制用空依赖桩包验证）、未验证 macOS、未验证带真实依赖的 Windows 端到端安装、未实际推送 GitHub。

## 6. 后续更新

修改源码后重新导出到新目录（导出器要求目标为空），确认 `SOURCE_MANIFEST.json` 与扫描结果，再在仓库中提交并打新 tag。Release 安装器拒绝覆盖已安装的同名版本目录，升级即安装新版本并切换启动器指向。
