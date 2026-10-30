# RecallFS

RecallFS 是一个面向开发资料与个人知识资产的 **本地离线检索桌面应用**。它把散落在多个目录里的代码、文档、日志和 PDF 建立成可核查的索引，并且让每一条搜索结果都能回到原始文件的具体行号或页码。

核心目标不是“搜到像的内容”，而是：

- 索引可靠：增量扫描，能识别新增、修改、删除和移动；
- 检索可解释：区分精确文件名、全文匹配和本地向量相似度；
- 证据可定位：结果带真实路径、行号/页码、索引时间和失效状态；
- 文件摘要只做可定位的原文摘录，明确标注“非模型生成”，不做推测性总结；
- 绝不修改原文件：删除索引、重建、清空目录都只操作 SQLite 数据库。

核心功能仅依赖 Python 标准库（`sqlite3` + FTS5、`os.scandir`、`hashlib`、`re`、`json`）。PDF / DOCX / PPTX / GUI 是可选增强，缺失时会如实降级，不会假装读取。

## 快速开始

要求：Windows 10/11，Python 3.12（最低 3.10）。

```powershell
cd RecallFS
python -m pip install -e .
```

安装后即可使用 `python -m recallfs`。若不安装，也可以临时把 `src` 放进模块路径：

```powershell
$env:PYTHONPATH = "src"
python -m recallfs --help
```

建立索引并检索：

```powershell
# 登记根目录（可多次执行，登记多个目录）
python -m recallfs roots add D:\work\notes --label notes
python -m recallfs roots add D:\work\code

# 增量索引所有已登记根目录
python -m recallfs index --all

# 也可以直接指定目录（同时登记为根目录）
python -m recallfs index --root D:\work\notes

# 全文检索：多词为 AND，引号表示短语，-词 表示排除
python -m recallfs search "证据 定位" --ext .md
python -m recallfs search '"本地离线"' --limit 10
python -m recallfs search "检索 -草稿" --root D:\work\notes

# 查看命中文件及其片段（证据定位）
python -m recallfs show 12 --context
# 文件摘要（可定位的原文摘录）
python -m recallfs summary 12 --max-chars 600

# 状态、完整性检查
python -m recallfs status
python -m recallfs verify
```

全局参数 `--db` 必须写在子命令之前：

```powershell
python -m recallfs --db D:\idx\recallfs.sqlite3 index --all
```

默认数据库位于 `~/.recallfs/recallfs.sqlite3`，可用环境变量 `RECALLFS_HOME` 改目录。

## 图形界面

```powershell
python -m recallfs            # 无参数：尝试启动 GUI
python -m recallfs gui        # 显式启动 GUI
python -m recallfs gui --db D:\idx\recallfs.sqlite3
```

GUI 使用 PySide6，并且是 **惰性导入**：只有 `src/recallfs/ui/qt_app.py` 内部才 `import PySide6`。未安装 PySide6 时，启动命令会打印回退提示并进入 CLI，不会崩溃。

GUI 提供：搜索栏（全文/本地向量）、扩展名/集合/符号/解析状态/失效筛选、结果表、证据详情（路径、行号/页码、索引时间、stale 原因、当前文件上下文）、添加根目录并索引、增量索引、暂停/继续/取消、集合/收藏/最近搜索、打开原文件、复制路径、相关文件、摘要（原文摘录）。

## 检索语义

RecallFS 分三层给出结果，避免把“相似”当成“事实”：

1. **精确文件名层**：查询词与文件名或去掉扩展名后的文件名完全相等时，优先返回该文件的元数据片段。这一层表示“文件存在”，不代表正文命中。
2. **全文层（FTS5）**：`unicode61` 分词，`bm25` 排序。中文通过“单字 + 双字 bigram”展开，因此任意长度的中文子串、单字和短语都能检索；这属于分词回退方案，不是语言模型。
3. **本地向量层（可选）**：`--mode semantic` 使用本地哈希向量计算余弦相似度。模型名为 `local-hash-bigram-v1`，标签明确为 **“本地哈希向量（非语义模型）”**，只表示词面/字面相似，不代表语义理解，更不代表事实。未构建向量时会回退全文检索，并在结果中说明原因。

查询语法：

| 写法 | 含义 |
| --- | --- |
| `检索 索引` | 两个词都要命中（AND） |
| `"本地离线"` | 短语匹配 |
| `-草稿` | 排除包含该词的片段 |
| `--ext .py` | 限定扩展名，可重复 |
| `--symbol main` | 限定代码符号 |
| `--kind metadata` | 只返回元数据片段 |
| `--name-contains report` | 文件名包含 |
| `--path-contains src\core` | 路径包含 |
| `--mtime-from 2026-01-01` | 修改时间下限（ISO 日期或 epoch） |
| `--size-min 1024` | 文件大小下限（字节） |
| `--collection notes` | 限定集合 |
| `--include-stale` | 保留失效结果并标注 |
| `--json` | 机器可读输出 |

空结果会明确说明原因：查询为空、索引为空、无匹配、或命中的片段全部失效需要重新索引（CLI 退出码 1）。

## 证据与失效（stale）

每个 `chunk` 都带：

- 真实绝对路径与文件名；
- `kind`：`metadata` / `raw_text`（当前版本不生成 `summary` / `model` 片段）；
- 文本文件的行号范围，PDF 的页码；
- 索引时间与解析状态。

`show` 和检索结果在查询时会重新 `stat` 文件，比较大小与修改时间；内容证据还会重新读取文件并核对片段内容。不一致时标记 `stale` 并给出原因（文件不存在、大小变化、修改时间变化、内容不一致），默认从结果中过滤，`--include-stale` 可保留查看。旧索引不会冒充当前文件内容。

## 集合、收藏、最近搜索与相关文件

```powershell
# 集合
python -m recallfs collections create notes --kind project --description "个人笔记"
python -m recallfs collections add-root notes D:\work\notes
python -m recallfs collections add-file notes D:\work\notes\a.md
python -m recallfs collections list
python -m recallfs search 索引 --collection notes

# 收藏文件与收藏查询
python -m recallfs favorites add-file D:\work\notes\a.md --note "重点"
python -m recallfs favorites add-query "证据 定位" --label "常用"
python -m recallfs favorites list
python -m recallfs favorites remove 1

# 最近搜索
python -m recallfs recent --limit 10
python -m recallfs recent --clear

# 相关文件：同内容哈希 / 其他文件引用本文件名 / 共同关键词（至少 2 个）
python -m recallfs related 12
```

每条“相关文件”都带 `reasons`，说明依据，不把猜测写成结论。

## 索引范围与安全边界

- 多根目录；路径使用 `normcase` 绝对路径作为唯一键；
- 增量判断基于 `path + size + mtime`；移动识别基于 `size + SHA-256`；
- 默认不跟随符号链接，且用 `realpath` 校验目标必须在授权根目录内，防止逃逸；
- 默认排除 `.git`、`node_modules`、`__pycache__`、`.venv` 等目录，以及 `.pyc`、`.tmp`、压缩包、可执行文件等后缀；
- 默认跳过隐藏文件和隐藏属性文件（可用扫描规则 `include_hidden=True` 开启，CLI 当前不暴露该开关）；
- 默认单文件上限 50 MB；文本解析上限 10 MB，超出部分截断并在元数据中标记 `truncated`；
- 索引库自身（`.sqlite3`、`-wal`、`-shm`）始终排除，不会被当成资料索引；
- 索引写入使用显式事务（`BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`），单个文件失败不影响已有索引；
- 数据库记录 `schema_version`，版本不一致时拒绝打开并提示 `rebuild --reset`；
- 删除索引、清空目录、删除集合、重建都 **不会删除源文件**。

## 支持的格式

| 类别 | 扩展名 | 依赖 | 缺失时行为 |
| --- | --- | --- | --- |
| 纯文本/代码/配置 | `.txt .md .markdown .log .rst .csv .tsv .json .jsonl .yaml .yml .toml .ini .cfg .conf .env .py .pyi .java .js .jsx .mjs .cjs .ts .tsx .c .h .cpp .cc .cxx .hpp .sql .sh .bash .zsh .ps1 .bat .cmd .go .rs .rb .php .kt .swift .xml .html .htm .css .scss .less .tex` 及 `.gitignore` 等 | 标准库 | 始终可用 |
| PDF | `.pdf` | `pypdf` | 标记 `unsupported`，保留元数据记录 |
| Word | `.docx` | `python-docx` | 标记 `unsupported`，保留元数据记录 |
| PowerPoint | `.pptx` | `python-pptx` | 标记 `unsupported`，保留元数据记录 |
| 图片 | `.png .jpg .jpeg .gif .bmp .tif .tiff .webp .heic` | OCR（未内置） | 标记 `unsupported`，说明需要 OCR |
| 其他 | 任意 | 无 | 标记 `unsupported`，保留记录且错误可见 |

解析失败的文件不会消失：状态为 `error` / `unsupported` / `incomplete`，可以通过 `index --retry-failed` 重试，或在 `status`、`search --kind metadata`、CLI `show` 中看到错误原因。

详细格式行为见 [docs/parsers.md](docs/parsers.md)。

## 本地向量（可选）

```powershell
python -m recallfs semantic build --limit 5000
python -m recallfs semantic stats
python -m recallfs search "证据 定位" --mode semantic
python -m recallfs semantic clear
```

- 模型：`local-hash-bigram-v1`，256 维，blake2b 哈希 + 符号累加 + L2 归一化；
- **不是神经网络/语义模型**，结果只反映词面相似；
- 完全本地：`network_required=False`、`auto_download=False`，不上传、不下载；
- 资源限制：单片段最多 4000 字符、最多 20000 个片段、查询最多扫描 50000 个向量；
- 显示覆盖率；未构建或覆盖不足时检索回退全文并给出说明。

## 项目结构

```
RecallFS/
  pyproject.toml
  requirements.txt
  README.md
  docs/
    parsers.md  packaging.md  test-report.md
  src/recallfs/
    __init__.py  __main__.py  cli.py
    filesystem_watcher.py  document_parser.py  content_chunker.py
    fulltext_index.py  index_scheduler.py
    search_engine.py  evidence_locator.py  semantic_index.py
    project_collections.py
    ui/__init__.py  ui/qt_app.py
  tests/
  tools/benchmark.py
```

## 测试与性能

```powershell
python -m pytest -q
python tools\benchmark.py --files 200 --semantic --json
```

当前 66 个测试全部通过。真实 benchmark（200 个生成文件、约 0.72 MB、665 个 chunk）的实测数据见 [docs/test-report.md](docs/test-report.md)。

## 已知限制

- 无 OCR：扫描版 PDF 和图片只保留元数据，不伪造文本；少数生成器导出、缺少 ToUnicode 映射的 PDF（含部分中文 PDF）也会如实标记为“无可提取文本层”，不猜测内容；
- 中文检索依赖单字 + 双字展开，不是分词器或语义模型，极长中文词的召回按 bigram 组合工作；
- 本地“向量”是哈希向量，不能理解同义改写，只能做字面相似排序；
- 不监听文件系统事件，需要手动执行增量索引（GUI 提供按钮）；
- 解析器为可选依赖，未安装时对应格式只保留元数据记录；
- GUI 依赖 PySide6，缺失时只能使用 CLI；
- 单文件 50 MB 上限、文本 10 MB 截断、PDF 默认最多 200 页；
- Windows 上符号链接测试可能因权限被跳过，但逃逸检查逻辑始终启用。

## 文档

- [docs/parsers.md](docs/parsers.md)：格式支持矩阵与解析降级规则
- [docs/packaging.md](docs/packaging.md)：安装、打包、数据库位置与离线说明
- [docs/test-report.md](docs/test-report.md)：测试环境、覆盖矩阵与 benchmark 实测数据
