# 测试报告

测试日期：2026-10-29
项目目录：`RecallFS`（仓库内相对路径）

## 1. 测试环境

| 项目 | 值 |
| --- | --- |
| 操作系统 | Windows（`sys.platform = win32`） |
| Python | 3.12.3 |
| pytest | 9.0.3 |
| SQLite | 3.45.1，FTS5 可用 |
| pypdf | 6.15.0 |
| python-docx | 1.2.0 |
| python-pptx | 1.0.2 |
| PySide6 | 6.12.0 |
| psutil | 7.2.2 |

测试命令：

```powershell
cd RecallFS
python -m pytest -q
```

测试配置来自 `pyproject.toml`：

```toml
[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
addopts = "-ra"
```

## 2. 总体结果

```
...............................................................          [100%]
66 passed in 8.85s
```

- 收集测试：66
- 通过：66
- 失败：0
- 错误：0
- 跳过：0

## 3. 测试文件与覆盖点

| 测试文件 | 用例数 | 主要覆盖 |
| --- | --- | --- |
| `tests/test_indexing.py` | 6 | 已知内容索引与检索、多 chunk 行号准确、增量新增/修改/删除/移动、旧内容不再命中、查询时 stale 过滤与标注、重复扫描增量计数、统计与完整性 |
| `tests/test_search.py` | 11 | 中文查询与短语、英文短语、多词 AND 与 `-` 排除、bm25 排序、扩展名/时间/大小/名称/路径筛选、符号筛选、metadata 与精确文件名层、空结果原因、`<mark>` 高亮、解析状态筛选、筛选组合与 offset |
| `tests/test_scope.py` | 7 | 排除目录/后缀、隐藏文件、大小上限、符号链接逃逸、根目录内符号链接、多根目录与集合筛选、索引库自身不被索引 |
| `tests/test_parsers.py` | 10 | 全部纯文本格式、PDF 页码与证据、损坏 PDF 错误可见、无 pypdf 时 unsupported、未知扩展名保留记录、图片需 OCR、10MB 截断标记、DOCX/PPTX 可用或标记、索引期间文件变化为 incomplete、可选解析器状态 |
| `tests/test_persistence.py` | 8 | 收藏/集合/最近搜索持久化、最近搜索去重与上限、完整性检查与 stale 列表、删除集合不删源文件、rebuild/clear 保留源文件、移除根目录不删源文件、索引版本不匹配需重置、收藏缺失文件返回空 |
| `tests/test_scheduler.py` | 4 | 异步索引完成、暂停/继续/取消、修复文件后重试失败项、无根目录时不误扫 |
| `tests/test_semantic.py` | 6 | 无向量时回退全文、构建/检索/清除、资源限制、哈希向量明确非神经网络模型、相关文件带依据、未知文件返回空 |
| `tests/test_cli.py` | 7 | 核心模块无网络导入、断网环境下索引与检索、help/version、CLI 端到端、筛选参数、错误根目录退出码、子进程 `python -m recallfs --help` |
| `tests/test_gui.py` | 2 | PySide6 不可用时回退 CLI 不崩溃、离屏 GUI 冒烟 |
| `tests/test_benchmark.py` | 2 | 小数据集 benchmark 输出真实 JSON 指标、人类可读输出 |

## 4. 需求覆盖矩阵

| 需求 | 验证用例 | 结果 |
| --- | --- | --- |
| tmp_path 下中文/特殊字符/空文件/超长行索引与检索 | `test_indexing.py`、`test_search.py`、`test_parsers.py` | 通过 |
| 高亮与行号/页码定位 | `test_search.py::test_highlight_markers`、`test_indexing.py::test_line_numbers_are_accurate_for_multichunk_file`、`test_parsers.py::test_pdf_pages_and_evidence` | 通过 |
| 文件摘要为可定位原文摘录、无文本时明确不可用 | `test_summary.py`（3 个用例） | 通过 |
| 增量新增/修改/删除/移动，旧内容不命中 | `test_indexing.py::test_incremental_add_modify_delete_move` | 通过 |
| 排除规则、隐藏文件、大小上限、符号链接逃逸 | `test_scope.py`（7 个用例） | 通过 |
| 中文查询、短语、筛选组合、空结果明确 | `test_search.py`（11 个用例） | 通过 |
| 损坏 PDF 与不支持格式保留记录且错误可见 | `test_parsers.py::test_broken_pdf_is_error_and_visible`、`test_parsers.py::test_unsupported_extension_keeps_record` | 通过 |
| 收藏/集合/最近搜索持久化、完整性检查 | `test_persistence.py`（8 个用例） | 通过 |
| 暂停/继续/取消、失败重试 | `test_scheduler.py::test_pause_resume_cancel`、`test_scheduler.py::test_retry_failed_after_fixing_file` | 通过 |
| 查询时 stale 校验与标注/过滤 | `test_indexing.py::test_query_time_staleness_filters_and_marks`、`test_persistence.py::test_integrity_and_stale_listing` | 通过 |
| 语义索引诚实标注、不可用时回退全文 | `test_semantic.py::test_hash_vector_is_not_neural_model`、`test_semantic.py::test_semantic_fallback_without_vectors` | 通过 |
| 相关文件给出依据 | `test_semantic.py::test_related_files_with_reasons` | 通过 |
| 核心模块不 import requests、断网可运行 | `test_cli.py::test_core_modules_do_not_use_network_imports`、`test_cli.py::test_index_and_search_without_network` | 通过 |
| PySide6 惰性导入，缺失回退 CLI | `test_gui.py::test_fallback_to_cli_when_gui_unavailable` | 通过 |
| GUI 可实例化 | `test_gui.py::test_gui_smoke_offscreen` | 通过 |
| benchmark 输出真实耗时/DB 大小/内存 | `test_benchmark.py`（2 个用例） | 通过 |
| CLI 可运行 | `test_cli.py`（7 个用例），另实际执行 `python -m recallfs --help` | 通过 |

## 5. 关键行为验证说明

### 5.1 增量与移动识别

测试先索引一组文件，再分别执行：

- 新增文件 → 计数 `added` 增加；
- 修改文件 → 计数 `modified` 增加，旧文本查询不再命中；
- 删除文件 → 计数 `deleted` 增加，记录与 FTS 行被移除；
- 改名/移动文件 → 通过 `size + SHA-256` 识别为 `moved`，文件 id 保留，FTS 的 name/path 列同步更新。

### 5.2 证据失效

索引后修改文件，但**不重新索引**直接查询：

- 默认结果过滤掉 stale 片段；
- `include_stale=True` 时保留结果，并给出 `stale_reason`；
- 全部结果失效时，空结果原因明确为 `all_results_stale`，CLI 退出码为 1。

### 5.3 解析降级

- 使用 monkeypatch 模拟 `pypdf` 缺失，PDF 状态为 `unsupported`，记录仍存在；
- 构造损坏 PDF，状态为 `error`，错误字符串可在结果/详情中看到；
- 未知扩展名与图片均为 `unsupported`，元数据 chunk 仍可检索；
- DOCX/PPTX 在依赖可用时解析，不可用时如实标记；
- 10MB 截断后，元数据 chunk 包含截断标记，正文只保留可解码部分。

### 5.4 离线

测试静态读取核心模块源码，断言不存在 `requests`、`socket`、`urllib`、`http.client` 等导入；同时在运行时 patch `socket`，验证索引与检索不需要网络。

## 6. 性能实测

### 6.1 命令

```powershell
python tools\benchmark.py --files 200 --semantic --json
```

数据集由脚本在临时目录生成：中文/英文文本、部分 `.py` 文件与符号，混合不同大小与行数。

### 6.2 原始结果

```json
{
  "dataset": {
    "files": 200,
    "bytes": 721659,
    "lines": 6550,
    "generation_seconds": 0.149
  },
  "index": {
    "seconds": 1.109,
    "files_per_second": 180.36,
    "files_scanned": 200,
    "chunks": 665,
    "added": 200,
    "skipped": 0,
    "parse_errors": 0
  },
  "database": {
    "size_bytes": 6901696,
    "size_mb": 6.582,
    "files": 200,
    "chunks": 665
  },
  "memory": {
    "method": "psutil",
    "rss_before_mb": 26.03,
    "rss_after_index_mb": 30.12,
    "rss_after_all_mb": 30.99,
    "delta_index_mb": 4.09
  },
  "queries": {
    "count": 30,
    "p50_ms": 2.913,
    "p95_ms": 6.974,
    "max_ms": 8.279,
    "mean_hits": 13.33
  },
  "semantic": {
    "model": "local-hash-bigram-v1",
    "version": "1",
    "label": "本地哈希向量（非语义模型）",
    "is_neural_model": false,
    "dim": 256,
    "indexed_chunks": 465,
    "coverage": 1.0,
    "build_seconds": 0.187,
    "query_p50_ms": 9.986,
    "query_max_ms": 10.348
  },
  "environment": {
    "python": "3.12.3",
    "platform": "win32",
    "sqlite": "3.45.1",
    "optional_parsers": {
      "pypdf": true,
      "python-docx": true,
      "python-pptx": true
    }
  }
}
```

### 6.3 解读与边界

- 200 个文件、约 0.72 MB 正文，索引耗时 1.109 秒，约 180 文件/秒；
- 665 个 chunk 对应数据库 6.58 MB（含 FTS 倒排与 WAL 统计口径）；
- 索引阶段 RSS 增量约 4.09 MB；
- 30 次全文查询：p50 2.913 ms，p95 6.974 ms，最大 8.279 ms；
- 本地哈希向量构建 465 个 chunk 用时 0.187 秒，查询 p50 9.986 ms；
- 数据集是脚本生成的合成语料，不代表大型真实仓库；实际性能会受文件大小、扩展名、磁盘与杀毒软件影响；
- 该 benchmark 不包含 GUI 渲染、首次 PDF 解析冷启动和超大目录扫描。

## 7. 未覆盖与已知测试限制

1. GUI 测试使用 `QT_QPA_PLATFORM=offscreen` 的冒烟级实例化，未做人工交互与视觉回归。
2. 符号链接测试依赖 Windows 开发者模式/权限；若权限不足，pytest 会跳过相关用例（本次运行全部执行并通过）。
3. 可选依赖缺失通过 monkeypatch 模拟，未在真实未安装环境中重新跑全套测试。
4. PDF 测试使用手工构造的最小 PDF，未覆盖复杂排版、加密与超大文档。
5. benchmark 为合成小数据集，不能外推为生产容量结论。
6. 未包含 PyInstaller 打包产物的自动化测试；zipapp 仅验证 `--help` 可运行。
