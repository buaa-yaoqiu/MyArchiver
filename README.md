# MyArchiver

本地实验归档与检索工具。Python 包名为 `experiment-archive`，桌面窗口显示为 ExperimentArchive。

实验说明存为 Markdown，可扩展元数据存为 JSON，精确指标和比较结果存为 CSV。支持可追溯版本、本地关键词/语义检索、中文 App 和可选中英翻译。项目通过 SDK、标准 bundle 或独立适配器接入。

本仓库只包含通用工具、测试和合成示例，不包含实验记录、项目适配器、模型权重、缓存或本机来源配置。

## 安装

要求 Python 3.10+。Windows 桌面启动器使用 PowerShell 和 Edge/Chrome；其他系统可使用 CLI 或浏览器界面。

```powershell
git clone https://github.com/buaa-yaoqiu/MyArchiver.git
cd MyArchiver
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

核心功能无需在线服务。语义检索、翻译和浏览器测试是可选依赖。

```powershell
# 首次显式下载语义模型；之后本地 CPU 推理
.\.venv\Scripts\python.exe -m pip install -e ".[semantic]"
.\ea.ps1 model download
```

语义模型使用量化 multilingual-e5-small，按固定 revision 下载。模型、索引和翻译缓存保存在归档的 `.cache/` 中，不纳入 Git。模型及其下载包受各自许可证约束，MIT 许可证适用于本仓库代码，不替代第三方模型许可。

## 启动与示例

Windows 双击 `ExperimentArchive.cmd`，或运行 `./Start-Archive.ps1`。首次可在空归档中浏览；在“来源与项目接入”选择同步配置和来源路径。

```powershell
# 合成分类示例：写入独立的示例归档
.\.venv\Scripts\python.exe examples/classification_project.py --archive ./demo-archive
.\.venv\Scripts\python.exe -m experiment_archive --archive ./demo-archive evidence "分类" --mode lexical

# 通用浏览器服务，默认仅监听本机
.\.venv\Scripts\python.exe -m experiment_archive.app --archive ./demo-archive
```

`ea.ps1` 默认操作本仓库内的 `archive/`。默认桌面窗口同样使用该目录。更换归档目录只切换当前库，不搬移原文件。退出 App 可释放已加载模型内存；`./Start-Archive.ps1 -Stop` 会等待服务收尾。

## 功能

- 不可变 revision、内容哈希与原始来源引用；更正保留历史。
- 列表支持时间正序/倒序；搜索支持相关性及时间排序，排序先于分页。
- 日期优先来自显式时间字段，其次编号/标题的日期，最后回退至归档版本时间，并标明依据。
- 关键词 BM25 与本地 Transformer embedding 混合检索，返回有界证据、指标和比较条件。
- 文件/文件夹路径可点击选择，设置保存在本机。
- 同步预览列出正文、指标、元数据、来源或位置变化，以及新增/更新/复用数量。
- 对章节内容未变、仅文件哈希或定位变化的导入复用 revision，保留原归档来源快照。
- 运行、评价、结论状态独立声明；未知信息不从正文猜测。
- 统一验收回执涵盖本地 schema、哈希、评价引用、基线配对、显式身份、声明完整性和索引。

相关性分数不代表实验收益。完整性检查通过也不表示科学结论通过，应结合评价范围、预测时机、基线身份、有效性和限制阅读。

## 同步与检索

复制 `pipeline.example.json` 为本机的 `pipeline.json`，修改 bundle 路径。实际配置已被 `.gitignore` 排除。

```powershell
.\ea.ps1 sync --dry-run
.\ea.ps1 sync --index-mode none        # 无语义模型时只导入
.\ea.ps1 index                         # 已安装并下载语义模型时
.\ea.ps1 evidence "检索问题" --mode lexical --sort time_desc --limit 3
.\ea.ps1 verify
.\ea.ps1 accept
```

`accept` 将回执写入 `archive/.cache/acceptance-receipt.json`。退出码：0 通过，1 检查失败，3 尚需复核，2 运行错误。来源漂移、缺失配对信息或未声明完整性会显式报告；不会联网重跑实验。

`sync --dry-run` 使用同一适配器在临时归档中模拟发布，不修改原归档和来源；自定义适配器应遵守只读取来源、只通过传入归档发布的接口契约。

## 中英翻译

详情页提供原文、中文译文、English 和双语对照。译文是绑定原版本的派生缓存，不替换权威 Markdown 或 CSV。英文是否节省 token 取决于模型，不预设节省率。

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[translation-local]"
.\ea.ps1 translation-model download
.\ea.ps1 translation-settings --provider local
.\ea.ps1 translate <experiment_id> --language en --provider local
```

本地翻译使用 Argos 分发的 OPUS-MT 1.9、CTranslate2 CPU int8 与 SentencePiece。下载包保留模型归属说明；原 OPUS 模型为 CC-BY 4.0，使用时请核对下载模型附带许可。参考 [Argos 包索引](https://github.com/argosopentech/argospm-index) 和 [OPUS-MT](https://github.com/Helsinki-NLP/Opus-MT)。

可选后端还包括本机 Ollama 和显式联网的 Google 公共翻译端点。Ollama 需自行安装并准备模型；Google 端点可能限流或变化，只有选择后点击翻译才发送所选文本，不会从本地失败自动切换联网。代码、表格、数值和标识受到保护；保护失败时保留原段或拒绝译文，机器译文仍需核对科学含义。

## 项目接入

见 [接入接口](docs/integration_api.md)。推荐由项目报告生成阶段共用结构化结果，调用 `ExperimentRecord.write_bundle()`，避免事后解析 Markdown。核心不负责训练、评分或拉取项目原始数据。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip install -e ".[ui-test]"
.\.venv\Scripts\python.exe scripts/validate_ui.py
```

单元测试使用合成数据和假的 embedding/翻译后端；浏览器冒烟测试启动临时归档并使用合成记录，不依赖个人实验库。`requirements.lock.txt` 和 `requirements.translation.txt` 为参考环境版本快照；跨平台安装优先使用 `pyproject.toml`。

## 许可证

[MIT](LICENSE)。第三方依赖、下载模型及用户导入内容保留各自许可与所有权。
