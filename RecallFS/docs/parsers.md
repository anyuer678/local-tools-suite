# 解析器与格式支持

本文说明 RecallFS 如何读取文件、哪些格式能提取正文、可选依赖缺失时如何处理，以及解析结果如何进入索引和证据链。

## 1. 总原则

1. **只读原文件**：解析过程只打开文件读取，不写入、不改名、不删除。
2. **永远返回结果，不抛内容异常**：`parse_document()` 的契约是返回 `ParseResult`。文件读不到、格式不支持、依赖缺失、PDF 损坏，都通过 `status` 与 `error` 表达。
3. **失败也保留记录**：解析失败的文件仍写入 `files` 表，并保留一个 `metadata` chunk，错误原因可被检索和查看。
4. **不假装读取**：可选依赖缺失时状态为 `unsupported`，不会生成空正文冒充成功，也不会用摘要或模型输出顶替原文。

## 2. 解析状态

`ParseResult.status` 与文件表的 `parse_status` 一致：

| 状态 | 含义 | 是否可检索 | 是否可重试 |
| --- | --- | --- | --- |
| `ok` | 正文解析成功 | 是（元数据 + 正文片段） | 文件变化时会重新解析 |
| `unsupported` | 格式没有可用解析器（依赖缺失、图片、未知扩展名、PDF 无文本层） | 元数据片段可检索，正文没有 | 安装依赖或换文件后可重试 |
| `error` | 解析器存在但文件损坏/加密/读取失败 | 元数据片段可检索，含错误原因 | 文件修复或依赖修复后可重试 |
| `incomplete` | 索引期间文件仍在变化，或重试后仍不稳定 | 元数据片段可检索，标注不完整 | 后续增量扫描会再次尝试 |

每次扫描都会重新评估 `pending` 与 `incomplete` 的文件；其他状态在 `size + mtime` 未变化时跳过，避免无意义重复解析。

## 3. 格式支持矩阵

### 3.1 纯文本与代码（标准库，始终可用）

```
.txt .md .markdown .log .rst .csv .tsv .json .jsonl
.yaml .yml .toml .ini .cfg .conf .env
.py .pyi .java .js .jsx .mjs .cjs .ts .tsx
.c .h .cpp .cc .cxx .hpp .sql
.sh .bash .zsh .ps1 .bat .cmd
.go .rs .rb .php .kt .swift
.xml .html .htm .css .scss .less .tex
.gitignore .dockerignore .editorconfig .properties .gradle .make
```

处理流程：

1. 最多读取前 10 MB（`DEFAULT_MAX_BYTES = 10 * 1024 * 1024`）；
2. 若样本含 NUL 字节，判定为二进制，返回 `unsupported`，不强行解码；
3. 按 **BOM → UTF-8 → GB18030 → Latin-1** 顺序解码；
4. 超过 10 MB 时 `truncated=True`，元数据 chunk 会写明“内容已截断，仅索引前 10MB”；
5. 文件无扩展名时，会先探测是否像文本；像文本则按纯文本解析，否则 `unsupported`。

截断解码的一个细节：当字节流在多字节字符中间被切断时，UTF-8/GB18030 使用 `errors="ignore"` 丢弃残缺字节，避免整段文本误回退到 Latin-1 变成乱码。

### 3.2 PDF（可选 `pypdf`）

| 情况 | 行为 |
| --- | --- |
| `pypdf` 已安装 | 使用 `pypdf.PdfReader`，逐页 `extract_text()` |
| `pypdf` 缺失 | `unsupported`，`parser=pypdf`，错误写明“PDF 仅保留元数据记录” |
| 加密 PDF | 先尝试空密码解密；失败则 `error`，错误可见 |
| 损坏 PDF | `error`，错误含异常类型与信息，记录保留 |
| 无文本层（扫描件） | `unsupported`，错误说明“需要 OCR” |
| 页数超过 200 | 只解析前 200 页，`truncated=True`，notes 标注总页数 |
| 单页提取失败 | 该页文本为空，notes 记录失败页号，其余页继续 |

PDF 的片段带 `page`（从 1 开始），页码是该页内的页码，不是全文偏移。证据校验时会重新按页读取文本。

### 3.3 DOCX（可选 `python-docx`）

- 提取所有非空段落；
- 提取表格行，单元格用 ` | ` 连接；
- 无固定页码，使用段落行号定位，notes 明确说明；
- 内容按 UTF-8 字节数估算，超过 10 MB 时截断并标记；
- 依赖缺失：`unsupported`，保留元数据；
- 文件损坏：`error`，错误可见。

### 3.4 PPTX（可选 `python-pptx`）

- 按幻灯片顺序提取文本框段落与表格；
- `page` 为幻灯片序号（从 1 开始）；
- 所有幻灯片都没有文本时返回 `unsupported`（例如纯图片幻灯片），提示需要 OCR；
- 依赖缺失或文件损坏的处理同 DOCX。

### 3.5 图片与未知格式

- 图片（`.png .jpg .jpeg .gif .bmp .tif .tiff .webp .heic`）：`unsupported`，错误为“图片文件不解析，需要 OCR（本版本不内置 OCR）”；
- 其他扩展名：`unsupported`，错误包含扩展名与文件大小；
- 两类文件都只保留元数据记录，文件名、路径、大小、时间仍可检索。

## 4. 分块与定位

| 参数 | 值 | 说明 |
| --- | --- | --- |
| `MAX_LINES` | 40 | 单个 chunk 最多聚合 40 行 |
| `MAX_CHARS` | 1200 | 单个 chunk 最多 1200 字符 |
| 超长行 | 按 1200 字符切分 | 切分后的片段仍记录同一原始行号 |
| 页码 | PDF/PPTX | 每页单独分块，行号在该页内计数 |
| 元数据 | 每个文件 1 个 | `kind=metadata`，包含文件名、路径、扩展名、大小、修改时间、解析状态、解析器、截断与错误 |

`kind` 当前只有：

- `metadata`：文件属性与解析状态；
- `raw_text`：从原文件提取的真实正文。

本版本不生成 `summary` 或 `model` 片段，因此不会把模型生成内容混入证据。

## 5. 代码符号抽取

按扩展名使用简化正则抽取，存为独立 `symbols` 表，支持 `--symbol` 筛选：

- Python：`def` / `async def` / `class`；
- JavaScript/TypeScript：`function`、`class`、常见 `const/let` 函数赋值；
- Java/C/C++：`class`、`struct`、常见函数定义；
- SQL：`CREATE TABLE` / `CREATE VIEW` 等；
- Shell：函数定义；
- Markdown：标题（`heading`）。

符号抽取是启发式的，不构建 AST，也不保证覆盖所有语言语法；它的用途是快速缩小范围，而不是替代编译器或语言服务器。

## 6. 证据校验与解析器的一致性

`EvidenceLocator` 在展示证据时会：

1. `os.stat` 比较大小与修改时间；
2. 对 `raw_text` 片段重新读取当前文件（最多 16 MB），检查片段前 200 字符是否仍存在；
3. PDF 按 `page` 重新提取对应页；其他格式重新读取全文；
4. 内容不一致 → `stale=True`，原因写明“当前文件内容与索引片段不一致”；
5. 文件不存在 → `stale=True`，原因包含系统错误。

因此：**索引里的正文只代表索引时刻的内容**；当前文件是否仍然如此，由查询时的校验回答。

## 7. 重试与修复

```powershell
python -m recallfs index --retry-failed
python -m recallfs search "某个错误关键字" --kind metadata
python -m recallfs show <文件id>
```

- `--retry-failed` 会重新解析 `error` / `unsupported` / `incomplete` 文件；
- 安装 `pypdf`、`python-docx` 或 `python-pptx` 后重试，原先标记 `unsupported` 的文件可转为 `ok`；
- 修复损坏 PDF 后重试，状态可从 `error` 转为 `ok`；
- `status` 会显示各解析状态计数与可选解析器可用性。

## 8. 已知限制

- 不内置 OCR，扫描 PDF 与图片没有正文；
- PDF 只取文本层，复杂排版、表格与公式的阅读顺序可能不理想；
- DOCX 不保留页码，PPTX 不提取备注与图表数据；
- 代码符号是正则启发式，不理解宏、模板或动态定义；
- 文本超过 10 MB、PDF 超过 200 页会截断，截断状态在元数据中可见；
- 加密文件若无法用空密码打开，只能保留元数据与错误信息。
