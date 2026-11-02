# 项目接入接口

核心不依赖特定数据集、模型或报告结构。推荐将归档作为实验完成后的发布步骤，与训练和评分解耦。

## Python SDK

```python
from experiment_archive.api import ArchiveClient, ExperimentRecord

record = ExperimentRecord(
    metadata={
        "schema_version": 1,
        "experiment_id": "example_run",
        "title": "合成示例",
        "experiment_time": "2026-01-01T10:00:00+08:00",
        "domains": ["demo"],
        "source_refs": [{"id": "report", "description": "Synthetic example"}],
        "evaluations": {},
        "lifecycle": {
            "run": "completed",
            "evaluation": "pending",
            "conclusion": "inconclusive"
        }
    },
    note="方法、结果与限制；这是合成示例，无实际成绩。"
)
client = ArchiveClient("./my-archive")
client.publish(record)
# 同一结构化结果可直接写为新目录下的标准 bundle
record.write_bundle("./exports/new-run")
```

`publish(record, reason=...)` 发布不可变版本；已有记录的实质变化需要明确原因。`import_record(record, reason=...)` 用于来源刷新，保留撤回/复核决定及自定义属性。`read(id, revision=...)` 精确读取版本，`table(...)` 分页读取数值。`accept()` 生成本地统一验收回执。

记录可使用 `attributes` 扩展嵌套字段，无需为新增参数扩展 CSV 列。指标方向、别名和百分比语义由 `metric_definitions` 声明，核心不根据名称猜测。

## 标准 bundle

```text
export/
  metadata.json
  note.md
  metrics.csv
  comparisons.csv
```

`ExperimentRecord.write_bundle(new_directory)` 校验并输出四个文件，拒绝覆盖已有目录。可在项目侧由同一结构化结果同时生成 Markdown 与 bundle。

CSV 即使为空也必须包含标准表头；字段定义见核心的 `METRIC_FIELDS` / `COMPARISON_FIELDS` 及初始化归档的 `schema.json`。评价 ID 必须对应 metadata 的 `evaluations`；方法、域和预测时机必须一致。不要把缺失成绩填成零。

```powershell
.\ea.ps1 ingest ./exports/new-run --adapter bundle --reason "Result export"
```

来源配置示例见 `pipeline.example.json`，仅将模板纳入 Git。`markdown` 适配器用于未复核原文快照；不得将其视为结构化成绩解析器。

## 状态与完整性

`lifecycle.run`、`lifecycle.evaluation` 和 `lifecycle.conclusion` 独立声明，可使用 `cancelled`、`completed_subset`、`reported_pass` 等项目定义字符串；缺失时显示未记录，不推断为通过。

`experiment_identity` 可声明稳定身份，例如 `{ "project": "demo", "run": "immutable-id" }`，用于明确身份重复检查。`canonical_experiment_id` 可将关联历史记录直接指向主记录，不允许链或环。

`completeness` 可声明 `metric_rows`、`comparison_rows`、`evaluations` 的预期非负整数计数；验收比对实际数量。未声明则报告未知，不自动推断完整。

## 检索与翻译

```python
result = client.evidence("问题", mode="lexical", sort="time_desc", limit=3)
# 可选本地语义检索，需要预先安装与下载模型
result = client.evidence("问题", mode="hybrid", rows_per_record=8)
# 英文只是派生材料，原始证据仍保留
result = client.evidence("问题", language="en", translation_provider="local")
```

时间排序在全部命中实验上执行后再分页。显式时间字段优先，否则读取编号/标题中的日期，最后回退到归档时间并标记依据。建议使用带时区的 ISO 8601 `experiment_time`。

CLI 还支持 `--where lifecycle.run=cancelled` 等嵌套条件。`evidence` 自动维护索引但不刷新来源；来源刷新必须显式调用 `sync`。

## 自定义适配器

通过独立 Python 包的 `experiment_archive.sources` entry point 注册；实现 `api_version = 1`、`inputs(path, options) -> list[Path]` 和 `ingest(archive, path, options, reason) -> {"records_processed": int}`。

`inputs` 应完整列出读取的文件。`ingest` 只读项目来源，只通过传入的 archive 对象发布，不修改外部状态。预览会在临时归档调用同一适配器；插件不是沙箱，只安装可信代码。项目词汇、报告路径、评分协议和指标方向属于项目插件，不进入通用核心。
