# RepoRune（rune）— 實作計畫

狀態：**已確認（第七輪修訂）**（V1 設計）。第六輪是外部 code review 對已完成的 Milestone 1 程式碼
做的落差修正（config 驗證、git 驗證、atomic write、model 邊界、FK/併發設計），細節見文末「第六輪
修訂」。**第七輪是 Milestone 4（Scopes）開工前，針對規格中未鎖死的三個實作細節（候選 scope 是否
持久化、clustering 建議的訊號來源、incremental 自動併入的信心判準）取得確認**，細節見文末「第七輪
修訂」與 ARCHITECTURE.md §4.4。將規格 §70-77 展開為具體交付項目、模組目標與各 Milestone
的驗收標準。本文件末尾的「設計決策記錄」列出各輪討論中對開放問題與 bug 的最終決定，供後續實作與
audit 對照。第四輪已對照 OpenCode 官方 plugin 文件確認 Milestone 7 的核心假設成立（`tool.execute.
before`/`after`、session events、custom tool 皆為真實 API），並針對前一輪列出的風險（import/
reference 解析、scope clustering 品質、cheap-model 可靠度、SQLite 併發）補上具體的工程對策。**第五輪
新增 Global Code Standards / Hard Policy Injection 語意**（ARCHITECTURE.md 新第 7 節），影響
Milestone 1（model 欄位）、Milestone 6（propose 流程填寫）、Milestone 7（`rune bootstrap` CLI 與
OpenCode adapter 的 hard/soft 注入）、Milestone 8（`rune doctor` 的 MUST 數量/token 警告）。同時
調降風險優先順序：

1. OpenCode integration — 已確認核心 API 可行，風險從「高」降至「中」（仍建議 Milestone 7 前做小 spike）
2. import/reference resolution — 工程風險最高，對策：imports=high confidence／references=best-effort
   的信任層級原則，Scope/Constraint 系統不得把 reference graph 當唯一依據
3. scope clustering 品質 — 產品效果風險最高，對策：Milestone 4 用真實中型 repo 收集 precision/可用率
   資料，不預先鎖死演算法參數
4. cheap-model semantic 可靠度 — 對策：Milestone 5 補上有限步驟的 fallback policy + 可比較的 metrics
5. 其餘（Windows 相容性、qualified_name 演算法、pricing table）— 併入既有測試/介面契約，不再視為
   獨立風險項

**現在沒有任何未知項足以阻擋 Milestone 1–3 開工**——這三個 milestone 的範圍（core foundation、
code index、references/graph）不依賴上述任何一個尚待驗證的假設。

工具鏈假設（本輪升級回原始假設）：**Python 3.12+**（開發機原本只有 3.11，已透過 `winget install
Python.Python.3.12` 補裝 3.12.10 並重建 venv；考量此專案預期使用多年、未來還會有 V2/V3，3.11 的
安全支援窗口到 2027 年底、3.12 到 2028 年底，現在（程式碼量還小）是切換成本最低的時間點，因此改回
3.12+ 而非長期停留在 3.11。升級後 `ruff` 自動指出 `read_json_model`/`read_jsonl` 可改用 PEP 695
的 `def f[T: BaseModel](...)` 泛型語法（3.12 起才有），已一併採用，取代原本的 `TypeVar`
寫法）、**pip + venv**（開發機無 `uv`；`pyproject.toml` 仍維持 uv 相容，未來可無痛切換）、hatchling
為 build backend、Typer 做 CLI、Pydantic v2、標準庫 `sqlite3`（V1 不需要 server process，同步即
可）、`tree-sitter` + 各語言獨立 grammar 套件（`tree-sitter-python`、`tree-sitter-javascript`、
`tree-sitter-typescript`，本次確認採此路線而非 `tree-sitter-languages` bundle）、`httpx` 呼叫
provider、`pytest` + `ruff`、git CLI 以 `subprocess` 呼叫。

**Milestone 1 目前狀態：已實作並通過測試**（`src/rune/`，41 個單元測試全綠，`ruff check` 全綠，含
Global Code Standards 支援欄位）。經過兩輪實測（第一輪自測、第二輪外部 code review）發現並修正了
兩個設計審查沒抓到的 bug：

1. **`rebuild_cache` 刪除順序**：原本會在驗證 canonical 資料前就刪除舊的 `memory.db`，若 canonical
   有衝突（例如重複 `(record_id, revision)`），會連同銷毀原本可用的 cache。
2. **`rebuild_cache` 的 crash-safety 機制在 Windows 上與並行 reader 不相容**（本輪 code review 加測
   併發案例後才發現，屬於「紙上設計看不出來、只有真的跑才會發現」的問題）：第 1 點的第一版修法是
   「先在暫存檔完整建好新 DB，確認成功後才用 `os.replace` 原子性換掉正式檔案」。這個做法在單獨執行時
   沒問題，但一旦有其他 connection（例如一個還開著的 reader）持有 `memory.db`，Windows 的檔案鎖定
   語意會讓 `os.replace` 直接丟出 `PermissionError`——等於「只要有人在讀，寫入就會失敗」，比原本要
   解決的問題更嚴重。**最終修法**：放棄「另建檔案再置換」，改成在**同一個檔案內用一個 SQLite
   transaction**做「清空全部內容表 → 重新寫入 → commit」，crash-safety 直接交給 SQLite 自己的
   rollback journal（transaction 中斷時，下次開啟會自動回滾到上次 commit 的狀態，語意上等同於檔案置
   換法想要的效果，但不需要真的置換檔案），也因此天然與 WAL 模式下的並行 reader 相容——reader 的
   already-open 讀取交易在自己 commit/rollback 前，本來就只看得到舊快照，不受同檔案上另一個
   transaction 的影響。已補上對應的回歸測試（`tests/unit/test_materialize.py`：衝突偵測維持舊
   cache 不變、reader 在 rebuild 期間維持一致快照、`os.replace` 中斷前後 canonical 檔案不受影響）。

## Milestone 1 — Core foundation
**模組**：`rune.core.config`、`rune.core.project`、`rune.core.storage.canonical`、
`rune.core.storage.schema_versions`、`rune.core.storage.sqlite.{schema.sql,materialize}`、
`rune.cli.main`（子集）。

**交付項目：**
- `pyproject.toml`、套件骨架、`rune` console-script 進入點。
- `.rune/config.toml` 讀取/驗證 -> `RuneConfig`（DATA_MODEL §7，含 `proposals.commit_to_git`、本輪
  新增的 `pricing.input_per_million`/`output_per_million`），欄位缺漏/不合法時給出清楚錯誤訊息。
- **SQLite 連線初始化套用併發 PRAGMA（本輪新增，見 ARCHITECTURE §4.7）**：`PRAGMA
  journal_mode=WAL;`、`PRAGMA busy_timeout=5000;`，`materialize.py` 的整次寫入包在單一 transaction
  內、結尾一次 commit，確保 `rune search` 等 reader 永遠讀到一致快照，不讀到半個 materialization。
- 專案偵測：定位 git root、確認為 git repo（規格 §48）、建立 `.rune/` 目錄結構、寫入初始
  `project.json`（含 `last_indexed_head=null`、`last_indexed_tree_hash=null`）。
- **`rune init` 的 refuse/`--force` 語意（本輪重新定義為完全非破壞性）**：`.rune/` 已存在時預設拒絕
  執行並提示改用 `rune update`。`--force` **絕不清空或重建任何已存在且合法的 canonical memory**——
  `project.json`（尤其 `project_id`/`created_at`）、`scopes.json`、`semantic.jsonl`、
  `decisions.jsonl`、`constraints.jsonl`、`notes.jsonl`、`proposals.jsonl` 全部原樣保留。`--force`
  只做：(1) 補建缺少的 canonical 檔案骨架、(2) 修復 config/schema 版本的骨架問題、(3) 刪除並重建
  SQLite cache、(4) 重新建立決定性程式碼索引。需要單元測試明確驗證 `--force` 前後這七個 canonical
  檔案內容 byte-for-byte 相同（先前草案誤將 `scopes.json`/`semantic.jsonl` 排除在保護範圍外，本輪
  已修正——`scopes.json` 若被降級為 skeleton，會連帶讓 scoped Decision/Constraint 失去 target、
  semantic summary 掛不到 scope，是不可接受的破壞性副作用）。若使用者確實要清空 memory 重來，未來另
  提供獨立、需二次確認的 `rune reset`，不與 `init --force` 共用語意。
- Canonical storage 抽象層：對 `project.json`、`scopes.json` 的型別化讀寫，以及
  `decisions.jsonl`／`constraints.jsonl`／`notes.jsonl`／`semantic.jsonl`／**`proposals.jsonl`**
  （本次新增）的 append-only JSONL writer，全部透過 atomic temp-file + rename（規格 §63）。每個
  writer 都標記 `schema_version`。
- **已完成（第五輪新增欄位，已實作進 `models.py`）**：`MemoryRevision` 補上 `critical`（bool，
  decision-only）、`source_document`/`source_section`（選填字串）、`machine_check_hint`（選填字串，
  constraint-only）四個欄位，`RuneConfig` 補上 `BootstrapConfig`；`schema.sql`/`materialize.py`
  同步更新對應欄位（見 DATA_MODEL §2.5、§7）。皆為向後相容新增（有預設值），新增 3 個單元測試
  （`test_decision_critical_and_source_fields_persist`、
  `test_decision_critical_defaults_to_false`、
  `test_global_must_constraint_persists_machine_check_hint`），目前共 32 個測試全綠。
- SQLite schema 建立（`schema.sql`，取自 DATA_MODEL §5）與 `rebuild-cache`（此階段先只從既有 canonical
  檔案重建，含 `proposals.jsonl -> pending_proposals` 的重建；code index 相關表在 Milestone 2 前保持空）。
- `rune init`、`rune init --force`、`rune status`（此階段檔案/symbol 計數為 0）、`rune rebuild-cache`。
- `.gitignore` 自動維護：`init` 時寫入 `.rune/cache/`，並依 `proposals.commit_to_git` 決定是否寫入
  `.rune/proposals.jsonl`。

**驗收標準：**
- 全新 git repo 上執行 `rune init` 產生合法的 `.rune/` 樹；重複執行時拒絕並提示 `--force`；
  `--force` 後全部七個 memory canonical 檔案內容不變（見上，尤其 `scopes.json` 與 `project_id`）。
- 刪除 `.rune/cache/` 後執行 `rune rebuild-cache` 能還原出等價的 `memory.db`，過程零資料流失、零網路
  /LLM 呼叫，且 `pending_proposals` 與 `proposals.jsonl` 內容一致。
- 單元測試：hashing、canonical atomic-write 在 rename 前中斷的存活性（模擬 crash）、schema version
  標記、config 驗證錯誤路徑、`init --force` 的保留規則。

## Milestone 2 — Code index

**目前狀態：已實作並通過測試**（`src/rune/core/index/`、`src/rune/core/update.py`，88 個測試全綠，
`ruff check` 全綠，含兩個 fixture repo：`tests/integration/fixtures/{python-simple,ts-simple}`）。
`rune rebuild-cache` 這次也改為真的做全量重新掃描＋解析（而不再只是「code index 保持空」的
Milestone 1 占位行為）——因為 files/symbols/edges 完全從原始碼推導、沒有 canonical 檔案背書，
「安全可重建」的唯一方式就是重新掃描一次，而這只是本機解析，沒有 LLM/網路成本，符合
「rebuild-cache 零 LLM 呼叫」的要求。`rune update`（新指令）走 incremental 路徑：只有 content_hash
改變的檔案會被重新丟進 tree-sitter，其餘檔案直接沿用 SQLite 裡既有的 symbols/edges。兩者共用同一個
`core.update.run_update(layout, full=...)` 進入點與同一個 `materialize.rebuild_cache` 交易邏輯，只
差在 `code_index` 的算法（全量 vs. diff-and-reuse）。

**完成後自我複查發現並修正 4 個問題**（實際跑 tree-sitter AST dump 找出來的，不是紙上推演）：
1. **Python 裝飾器被完全漏掉**：`@staticmethod`/`@property`/`@dataclass` 等會把真正的
   `function_definition`/`class_definition` 包在一層 `decorated_definition` 節點裡，原本的
   walker 只匹配裸的 `function_definition`/`class_definition`，導致任何有裝飾器的函式/類別（真實
   Python 程式碼裡佔絕大多數：route handler、property、dataclass、pytest fixture 等）完全沒被索引
   到。已修正為在 walker 裡先偵測並解開 `decorated_definition`，並讓 symbol 的行號範圍包含裝飾器行。
2. **`var` 宣告被漏掉**：`var x = 1` 的 node type 是 `variable_declaration`，跟 `let`/`const` 的
   `lexical_declaration` 是不同的 node type，原本只檢查後者。已修正為兩者都處理。
3. **單一檔案解析失敗的隔離有漏洞**：原本的 `except (OSError, ValueError, UnicodeDecodeError)` 涵蓋
   不到 tree-sitter 在 error-recovery 情境下可能產生的 `MISSING` 節點（預期欄位被合成為 `None`，
   接著呼叫 `.text` 會丟 `AttributeError`），這種例外不在原本捕捉範圍內，理論上會讓整次
   `rune update` 崩潰——直接違反規格 §62「單一檔案解析失敗不應讓整次 update 失敗」的要求。已改為
   有意識地捕捉廣義 `Exception`（並加註解說明為何這裡的寬鬆捕捉是刻意的，不是隨手 catch-all）。
4. **`.rune/` 目錄沒有完整排除在掃描之外**：預設 exclude 只有 `.rune/cache/**`，`.rune/` 底下其他
   位置若意外放了 `.py`/`.ts`/`.js` 檔案會被當成專案原始碼索引。已把 `.rune` 加進掃描器的
   `_ALWAYS_PRUNED_DIR_NAMES`（與 `.git` 同等級的強制排除，不受使用者 config 影響）。

補上對應的 4 個回歸測試（含一個「測試本身寫錯層級、驗證了不存在的東西」而修正的案例——第一版
parse-failure-isolation 測試直接替換整個 `_parse_file`，結果連同它想驗證的 try/except 保護一起替換
掉了，測試在保護邏輯被刪除的情況下也會通過；修正後改成只替換 `get_parser_adapter`，讓真正的
`_parse_file` try/except 留在呼叫路徑上）。

**第二輪外部 code review（非本 agent 執行）又發現並修正 7 個問題**（皆已實際重現後才修）：

1. **高：刪除被未變檔案 import 的目標檔會讓 `rune update` 崩潰**：未變檔案的 edge 被原樣沿用，但其
   `target_file` 已不在本次的檔案集合裡，寫入時撞上 `target_file REFERENCES files(path)` 外鍵，
   丟出 `IntegrityError`，整次 update 直接中止。已於 `_materialize_code_index` 補上防禦性過濾：
   插入前檢查 `target_file`/`target_symbol` 是否還在本次的檔案/symbol 集合中，不在就置成 `NULL`
   （語意等同「已解析過但現在失效」，與外部套件 import 的 unresolved 語意一致，不是刪掉這條 edge，
   因為「這個檔案曾經 import 過東西」這件事本身仍然真實）。
2. **高：SQLite cache 已 commit、project.json 才更新，兩者非原子**：確認且接受這是無法完全消除的
   已知限制（`memory.db` 與 `project.json` 是兩個獨立檔案，做真正的兩階段提交不符合這個專案的複雜度
   預算）。已將 `tree_hash`/`updated_project` 的計算全部移到呼叫 `rebuild_cache` 之前，讓 commit 後
   唯一剩下的動作只有一次 `write_json_model`（縮小風險窗口，不是消除它）；並補上回歸測試證明：即使
   這次寫入失敗，cache 本身仍正確、下一次 `run_update` 仍能正確運作（因為 incremental diff 永遠讀
   `files` 表而非 `project.json`，不會被過期的 metadata 帶壞）。詳細取捨理由寫在
   `core.update.run_update` 的程式碼註解裡。
3. **中：Tree-sitter 語法錯誤不會被標記為 `parse_error`**：tree-sitter 對錯誤語法採 error-recovery（
   回傳含 ERROR 節點的部分樹，不丟例外），先前程式碼只檢查是否拋出例外，導致有語法錯誤的檔案仍標記
   `status=ok`、甚至寫入從錯誤區域擷取出的殘缺 symbol（例如簽章缺右括號）。已在 `ParserAdapter`
   新增 `has_syntax_error(source) -> bool` 方法（各語言 adapter 各自用 `root_node.has_error`
   實作），`_parse_file` 據此把有語法錯誤的檔案標記 `parse_error`，但仍保留已擷取出的 symbol
   （best effort，不因為檔案有語法錯誤就整個丟棄，只是明確標記「這個檔案的索引可能不完整」）。
4. **中：`ParserAdapter` 缺少規格要求的 `qualified_name` 方法**：先前的實作只有 `extract_symbols`/
   `extract_imports`，`qualified_name` 完全沒實作，違反 ARCHITECTURE §4.2 的介面契約（也是 Milestone
   3 reference 解析會需要的方法——給定任意一個 tree-sitter node，算出跟 `extract_symbols` 一致的
   qualified name，用來把「呼叫點找到的 node」對應回已知的 symbol）。已補上：每個 adapter 實作一個
   由下往上走 parent chain 的版本，並用測試驗證它跟 `extract_symbols` 由上往下算出來的名字一致
   （兩條路徑對同一個 node 必須算出同一個名字，否則同一個邏輯 symbol 會因為用哪條路徑算名字而產生
   不同 id）。
5. **中：Python `from . import services` 誤解析為套件自己的 `__init__.py`**：`RawImport` 只擷取
   `relative_import` 的點號前綴（specifier 是單獨的 `"."`），沒有擷取 `import` 後面實際的名稱列表，
   導致 resolver 誤把它解析成目前套件的 `__init__.py`——對一個宣稱「high confidence」的 edge 類型
   而言，這是一個自信滿滿的錯誤答案，比留白（unresolved）更糟。已修正：當 specifier 去掉點號後為空
   字串時，直接回傳 `None`（誠實地標記為無法解析），不再用套件自己的 `__init__.py` 充數。
6. **中：`scope_files`/`scope_symbols` 的外鍵仍未依承諾在 Milestone 2 補回**：第一輪 code review 就
   已經記錄「等 Milestone 2 索引器落地後補上這兩個 FK」，這次確認 Milestone 2 已經讓 `files`/
   `symbols` 有真實內容，兌現承諾補回兩個 FK。同時發現 `INSERT OR IGNORE` **不會**抑制外鍵違反（
   只會抑制 UNIQUE 衝突，已用一個最小重現腳本親自驗證這件事，不是憑印象假設），所以在
   `_materialize_scopes` 補上明確的前置過濾：scope 成員若沒對應到目前索引裡的任何 file/symbol，
   直接跳過該筆 membership（不中止整個 materialize），並更新了一個因此不再成立的 Milestone 1 舊
   測試（原本沒有搭配任何 `files` 資料就寫入 scope membership），新增一個「dangling membership 被
   跳過而非讓整個 materialize 失敗」的回歸測試。
7. **中：`rune status` 沒有回報規格要求的「已修改檔案數」**：先前的 CLI 只回報一個
   `working_tree_fresh` boolean，之前自己刪掉了一段本來想算「修改了幾個檔案」卻寫錯邏輯的程式碼，
   刪掉後沒有補回正確版本，導致這個 M2 明確要求的欄位一直沒有實作。已用既有的 `diff_against_previous`
   補上 `files_modified`/`files_added`/`files_deleted` 三個欄位（`--json` 與純文字輸出都有），並新增
   `tests/unit/test_cli.py` 用 Typer 的 `CliRunner` 驗證。

第 8 點（`pyproject.toml`/`IMPLEMENTATION_PLAN.md` 的 Python 版本要求不一致）覆查後確認**已經在
Python 3.12 切換那次 commit 修正過**，reviewer 看到的應該是切換前的舊快照，這次不需要任何動作。

**模組**：`rune.core.index.scanner`、`rune.core.index.treesitter`、`rune.core.index.imports`、
`rune.core.update`。

**交付項目：**
- 依 include/exclude glob 走訪檔案；計算 content hash + git blob hash；與 SQLite `files` 表中先前索引
  狀態比對，產出 added/modified/deleted 變更集。
- 同步計算並更新 `project.json.last_indexed_tree_hash`（架構文件第 9 節）：對所有已索引檔案的
  `(path, content_hash)` 排序後串接雜湊，讓 `rune status` 能偵測「HEAD 未變但 working tree 已改」的情境。
- Tree-sitter 整合，語言套件採 `tree-sitter-python`、`tree-sitter-javascript`、`tree-sitter-typescript`
  三個獨立套件（本次確認，非 bundle package，理由：版本可控、不受上游更新延遲卡住）：解析變更檔案、
  擷取 `Symbol`（DATA_MODEL §2.2），含正確的 `kind`、行號範圍、signature。
- **`ParserAdapter` 介面（本輪新增，見 ARCHITECTURE §4.2）**：定義 `extract_symbols`/
  `extract_imports`/`qualified_name` 三個方法的共同介面，`qualified_name` 的實際演算法由
  `PythonParserAdapter`/`TypeScriptParserAdapter`/`JavaScriptParserAdapter` 各自實作，不要求跨語言
  演算法一致，只要求輸出符合 `Symbol` model 的形狀。
- 各語言的 import graph 擷取（`edge_type=imports`），寫成 `Edge`。**Import 解析視為 high confidence**
  （`confidence=1.0`），但 V1 不追求完美處理 TypeScript 的 `paths`/`baseUrl`/barrel export 或
  Python 的 relative import/namespace package/`PYTHONPATH` 全部 edge case——遇到無法解析的 import
  記錄 `target_file=NULL`，不報錯、不中止整次 update（見 ARCHITECTURE §4.3 的信任層級原則）。
- `rune update` 協調流程（`core.update`）串接 scanner -> indexer -> materialize，並更新
  `project.json.last_indexed_head` 與 `last_indexed_tree_hash`。
- `rune status` 擴充為顯示真實檔案/symbol 數與已修改檔案數（規格 §51），並標示目前 working tree 是否
  與 `last_indexed_tree_hash` 一致。

**驗收標準：**
- 在 fixture repo 中修改單一檔案後執行 `rune update`，只重新索引該檔案（透過 parse 函式的 spy/計數器或
  log 斷言驗證）。
- Symbol rename（本次確認為刪除+建立）產生新 `symbol_id`，舊 ID 從 `symbols` 表消失，不做錯誤的重新映射。
- 建立 `tests/integration/fixtures/ts-simple`、`python-simple` 兩個 fixture repo。
- 只改動檔案內容但不影響任何 symbol 邊界時，`last_indexed_tree_hash` 仍正確反映該變動（即便某些
  下游如 Decision 不受影響，這條 hash 本身必須精確）。

## Milestone 3 — References / graph

**目前狀態：已實作並通過測試**（`src/rune/core/index/references.py`，107 個測試全綠，`ruff check`
全綠）。`ParserAdapter` 新增 `extract_references`，每語言各自擷取呼叫點（`call`/`call_expression`
的 function 欄位，含 `attribute`/`member_expression` 的屬性存取）與繼承關係（Python 的
`superclasses`、TS 的 `class_heritage` 的 `extends_clause`/`implements_clause`）。解析分兩階段：
擷取（純語法，各檔案獨立）與 `core.index.references.resolve_references` 解析（跨檔案，需要完整的
symbol table，故在 `core.update.run_update` 收集完所有檔案的 symbol 後才統一做一次解析），符合
`qualified_name` 已經確立的「輸出形狀固定、演算法各自實作」原則。信心分級：本檔案內命中 0.8、
透過 import 關係命中的其他檔案 0.6、完全無法比對 0.3（**仍記錄，不捨棄**）。

**模組**：`rune.core.index.references`。

**交付項目：**
- Best-effort reference 解析（`edge_type=calls`/`extends`/`implements`；`references` 保留給
  未來更細的識別字引用，V1 尚未產生這個 edge_type），帶 `confidence` 分數；無法解析的 reference
  記錄為 `target_symbol/target_file = NULL`，不刪除、不報錯。
- 查詢輔助：「誰引用了 symbol X」——`find_referencing_edges(conn, symbol_id)`，基於 `edges` 表
  （M6 的 `core.retrieval` 落地前的暫時介面，純 SQL，無額外抽象）。

**驗收標準：**
- 在 fixture repo（`python-simple`）與一個手工建立的最小 repo（TS 的 extends/implements）上，一組
  人工驗證過的 reference 查詢回傳正確結果——`run()` 呼叫 `UserService(...)`/`service.get_user(1)`
  正確解析到 `app/services.py`，`self.db.fetch(...)` 正確標記為無法解析（不是被丟棄）；
  `Circle extends Base implements Shape` 正確產生兩條分開的 edge。
- 模糊或無法解析的 reference 不會造成 crash/abort。
- **回歸測試證明 reference 解析大範圍失敗時 rune 仍可運作**（對應 ARCHITECTURE §4.3 的信任層級
  原則）：monkeypatch `resolve_references` 讓所有 reference 一律回傳 unresolved，`rune update`
  仍正常完成，scope membership 查詢完全不受影響（因為它從未依賴 reference graph）。

**自我複查發現並修正 1 個問題**（在寫「誰引用了 symbol X」的手動驗證腳本時，用不同的
`PYTHONHASHSEED` 連續跑了 5 次同一段程式碼才發現）：當一個呼叫名稱同時比對到**兩個以上**匯入檔案裡
的同名 symbol 時，原本直接對 Python `set`（`imported_files`）做迭代來挑第一個命中的候選——但字串的
`set` 迭代順序在不同行程之間會因為 hash 隨機化而不同，導致完全相同的原始碼在不同次 `rune update`
呼叫之間可能解析出不同的目標，違反這個專案一路建立起來的「相同狀態輸入、相同結果輸出」保證（`rebuild
-cache` 等價性正是建立在這個保證上）。修法是先 `sorted()` 再挑，讓結果與行程的 hash seed 無關。
回歸測試刻意用 `subprocess` 搭配五個不同的 `PYTHONHASHSEED` 值執行同一段解析邏輯，確認全部回傳同一個
答案——單一行程內重複呼叫測不出這個 bug（CPython 同一行程內的 hash 快取讓 set 順序在行程存活期間保持
穩定），必須真的跨行程才會顯現。

**外部 code review 又發現並修正 2 個問題（皆已實測重現）：**

1. **中：`parse_error` 狀態在下一次無修改的 `rune update` 被重設為 `ok`**：`run_update` 對
   `changeset.unchanged`（content_hash 沒變、本次不會重新解析）的檔案，原本無條件寫入
   `IndexedFileStatus.ok`，完全忽略這個檔案上一次真正解析出來的狀態。實測重現：第一次索引一個語法
   錯誤的檔案，正確標記 `parse_error`；接著在檔案完全沒改動的情況下再跑一次 `rune update`，狀態變成
   `ok`——等於「什麼都沒做卻看起來像修好了」。原因是「content_hash 沒變」只代表「這次不會重新解析」，
   不代表「上次解析是乾淨的」。修法是在建構 unchanged 檔案的 `IndexedFile` 時，改用上一次
   materialize 出來的實際 `status`（從 `read_current_code_index` 的結果查表），而不是寫死
   `ok`。回歸測試：對同一個語法錯誤檔案連續跑兩次 `rune update`（第二次是無修改的 no-op），確認
   `parse_error` 在兩次之間都維持不變。
2. **中，文件間契約衝突**：ARCHITECTURE.md §4.2 原本寫「該檔案標記 `status=parse_error` 並跳過」，
   只描述了「真的丟例外」這一種情況；但 Milestone 2/3 實際實作（也是 IMPLEMENTATION_PLAN.md 已經
   記錄的行為）是：語法錯誤但沒丟例外時，`status=parse_error` 但**保留**已擷取出的 symbol，不是
   「跳過」。兩份文件對同一件事的敘述互相矛盾，等於沒有單一版本的契約可以宣稱「符合」。已更新
   ARCHITECTURE.md §4.2，把「真的丟例外」與「語法錯誤但擷取成功」兩種情況分開寫清楚，並明確加上
   本次一併修正的「`parse_error` 狀態必須沿用到檔案真的被重新解析為止」這條規則，讓兩份文件的敘述
   一致。

## Milestone 4 — Scopes
**模組**：`rune.core.scopes.{model,heuristics,clustering}`。

**開工前確認的三個設計決策（第七輪修訂，見設計決策記錄第 45-47 條與 ARCHITECTURE §4.4）**：
候選 scope 不持久化、純一次性 CLI 互動；clustering 候選建議可同時用 import + best-effort reference
edge；incremental 自動併入只認 import edge 且僅限單一候選，其餘一律落回人類確認。

**交付項目：**
- 對 `scopes.json` 的 `Scope` CRUD（手動建立/編輯 scope，透過 CLI 或直接編輯檔案後 `rune update` 讀取）。
- 路徑啟發式候選產生（例如共同目錄前綴 + 命名慣例）。
- 基於 import/reference graph 的 graph-assisted 候選建議（用 NetworkX 做 connected-components /
  簡單 community detection，不超出規格 §3 明確排除的 igraph/Leiden）。**演算法參數本輪明確不在此時
  鎖死**（見 ARCHITECTURE §4.4）：V1 的主要情境是新專案從小數量 scope 逐步累積，不是對陌生大型 repo
  要求完美聚類，因此 clustering 品質留待下方「真實 repo 實驗」驗收項調整，不預先規定固定 threshold。
  **建議圖同時使用 `imports` 與 best-effort 的 `calls`/`extends`/`implements` edge**（第七輪確認，
  因為建議一定經人類確認，不違反 §4.3 的治理層信任原則，理由見 ARCHITECTURE §4.4）。
- **候選 scope（heuristic 與 clustering 皆同）不持久化，純一次性 CLI 互動**（第七輪確認）：
  `rune scope suggest` 當場計算、當場印出、當場人類 `[y]es/[n]o` 逐一確認，才會寫成 `source=model`
  的 scope（或併入現有 scope 的 membership）；不落地任何新的 canonical 檔案（不新增
  `scope_candidates.jsonl`），關掉終端候選即消失，下次重新計算即可——這與 Decision/Constraint 的
  `proposals.jsonl`（需要撐過重開機、代表尚待處理的治理狀態）刻意不同，因為 scope 候選在確認前只是
  可低成本重算的建議，不是需要持久化的狀態。`locked` scope 永遠不受 clustering/heuristic 建議影響。
- **新檔案的 incremental 分派規則（第七輪確認，取代原本未定義信心判準的版本）**：`rune update` 對
  一個新檔案，若透過 `edge_type=imports`（`confidence=1.0`）**恰好命中一個**現有、非 `locked` 的
  scope 成員，直接自動加進該 scope 的 `members.files`，不需人類確認、不觸發全庫重新分群。以下情況
  一律落回人類確認的 CLI 流程，不自動寫入：零個或多個候選 scope（模糊）、只有 best-effort reference
  edge 命中（沒有 import edge）、目標 scope 為 `locked`、任何會移除既有 membership 或建立新 scope
  的動作。這個判準刻意比 clustering 建議嚴格，因為 incremental 自動併入是唯一無人把關的寫入路徑，
  只能用 §4.3 明定的 high-confidence 訊號。
- 檔案層級與 symbol 層級的 membership，多對多，materialize 進 `scope_files`/`scope_symbols`。

**驗收標準：**
- 一個檔案可同時屬於兩個 scope，且兩個 membership 在 `rune update` 與 `rebuild-cache` 後都存活。
- 對一個 `locked=true` scope，在觸發 clustering 建議的 `rune update` 前後，其 membership 可證明完全不變
  （回歸測試斷言前後 membership 完全一致）；同一測試需涵蓋 incremental 自動併入路徑（`locked` scope
  即使被新檔案的 import edge 命中，也不會自動獲得新 membership）。
- 增量情境：新增一個依 import edge **恰好**命中一個既有 unlocked scope 的檔案，能被自動併入該
  scope 而不觸發全庫重新分群、不需人類確認；新增一個同時命中兩個既有 scope 的檔案，則不自動寫入，
  落回人類確認流程（回歸測試需覆蓋「單一候選自動併入」與「多重候選需確認」兩種情境）。
- `rune scope suggest` 的候選（heuristic 與 clustering 皆同）在人類拒絕或直接關閉 CLI 後不留下任何
  canonical 副作用——重新執行 `rune scope suggest` 能重新產生候選，而非讀到某種「已拒絕」的殘留狀態。
- **真實 repo scope 品質實驗（本輪新增，取代固定 threshold 驗收）**：在至少 2 個真實中型 repo（例如
  `honeypot-discord-bot` 本身，加上另一個結構不同的專案）上執行 clustering，人工檢查 candidate
  scope 是否「像人會畫的架構邊界」，記錄 precision（建議的 scope 有多少比例合理）與可用率（有多少
  比例不需大改就能採用），作為調參依據——**不要求達到固定數字**，只要求資料被收集下來、供後續版本
  對照改進。這條驗收標準的性質與其他機制性驗收不同：目的是收集資料而非通過/失敗判定。

## Milestone 5 — Semantic worker
**模組**：`rune.core.semantic.{provider,worker,validation,redaction}`。

**交付項目：**
- `ModelProvider` protocol + `OpenRouterProvider`（主要）、`OpenAIProvider`、通用
  `OpenAICompatibleProvider`，皆用 `httpx`，皆回傳經驗證的 `ScopeSummary`（structured
  output / tool-call JSON mode）。
- Redaction pass（規格 §58 樣式）套用於產生出的 `ScopeSummary` 每個欄位，在驗證之前執行。
- **三層驗證，strip 與 reject 的分界（本次確認）**：
  - Schema 驗證（Pydantic）+ 核心欄位（如 `purpose`）不成立 -> **拒絕整份 generation**，保留舊 summary。
  - 引用到不存在的 file/symbol 的**條目**（`entry_points`/`important_symbols` 等清單中的個別項目）
    -> **strip 該條目並記錄警告**，不影響其餘欄位、不拒絕整份 generation。
  - 這個分界本次已明確確認：便宜模型偶爾 hallucinate 一個不存在的 symbol，不應該讓整個 scope
    summary 作廢。
- Staleness 計算（`source_hash` 比對，`source_files` 提供逐檔明細）與 DATA_MODEL §6 的
  `fresh/possibly_stale/stale` 轉換——**此規則只適用於 ScopeSummary，不套用於 Decision/Constraint**
  （見 Milestone 6）。**`source_files` 必須依 DATA_MODEL §2.4 invariant 計算為
  `scope.members.files ∪ {owning_file(s) for s in scope.members.symbols}`**——一個只透過
  `members.symbols` 納入成員的 scope，仍必須把這些 symbol 的 owning file 解析出來納入
  `source_files`，不能因為 `members.files` 是空的就讓 `source_files` 也是空的。
- 逐 scope 失敗隔離：產生失敗或被拒絕時，保留 `semantic.jsonl` 舊行、不附加新行、記錄 `last_error`，
  只在 SQLite 投影中維持 `status=stale`（不是產生新的 JSONL 行，因為實際上沒有新內容產出）。
- **有限步驟 fallback policy（本輪新增，見 ARCHITECTURE §4.5）**：primary model 失敗 -> 1 次
  repair-prompt retry -> fallback model（config 另外指定）-> 仍失敗則保留舊 summary 並停止，不做
  無限重試。同時記錄 `schema_success_rate`／`reference_strip_rate`／`fallback_rate`／
  `provider_error_rate`／`cost`／`latency` 六項 metrics（存入 SQLite 或獨立 metrics 表），供未來
  比較不同 provider/model 的實際表現。
- 每次 run 的 token/成本追蹤，依 config 的 `[pricing]` 估算表輸出在 `rune update` 結果中（規格 §61），
  明確標示為估算值，非 billing-grade 金額。

**驗收標準：**
- 修改某 scope 的一個 member 檔案，該 scope 的 summary 被標記 `stale`；執行 `rune update` 後重新產生
  並轉回 `fresh`，`semantic.jsonl` 恰好新增一行，且新行的 `source_files` 正確反映哪些檔案的 hash
  改變了。
- 模擬 provider 失敗：舊 summary 保持不變，`last_error` 被設定，不損毀 `semantic.jsonl`，不影響本次
  update 的其他 scope。
- 模型回應引用不存在的 file/symbol：該引用被 strip，不被信任，但其餘欄位正常保留（非整份拒絕）。
- 一個 scope 只有 `members.symbols`（`members.files` 為空）時，`source_files` 正確解析出這些
  symbol 的 owning file 並納入，不會是空字典。
- 模擬連續失敗（primary + retry + fallback 全部失敗）：`rune update` 不無限重試，最終保留舊 summary
  並正確記錄 `last_error`；`schema_success_rate` 等六項 metrics 在正常與失敗案例下都被正確記錄。

## Milestone 6 — Policies & Memory
**模組**：`rune.core.memory.{decisions,constraints,notes,staleness}`。

**交付項目：**
- **Current 與 visible 分離（本輪修正的核心 bug，見 DATA_MODEL §1、§3）**：`core.memory` 的
  「current revision」查詢一律是 `MAX(revision)`，不得依 `status` 過濾候選集合；可見性判斷
  （是否出現在 `rune search`/bootstrap/scope-activation context，以及是否附加警告標記）在
  retrieval 層對 current revision 的 `status` 另外套用 DATA_MODEL §3、§6 的表格。這是實作時最容易
  重犯的錯誤點，需要專門的單元測試鎖死（見下方驗收標準第一條）。
- **Proposal 流程（本輪修正為 revision 化）**：`decision_propose`/`constraint_propose` 寫入
  `.rune/proposals.jsonl` 的 `revision=1`（`status=pending`）。CLI 確認流程
  （`[A]pprove/[R]eject/[E]dit`，規格 §21）核准後，`core.memory` 附加該 proposal 的 `revision=2`
  （`status=approved`/`rejected`/`edited`，`resolved_at`/`resolved_by` 有值），**同一次操作**在
  `decisions.jsonl`/`constraints.jsonl` 附加新的 `MemoryRevision`（`approved_by` 有值）——proposal
  的 `current = max(revision)` 與 Decision/Constraint/Note 共用同一套 current 規則，解決先前
  「append-only 檔案要怎麼改 status」的未定義問題（不是原地改寫，也不是無版本規則的重複 append）。
  **核准 Constraint 時，若 `persistence_mode` 為 `source_bound`／`scope_bound`／`temporary`，
  `core.memory.constraints` 必須自動計算並填入對應的 `source_hashes`／`scope_hashes`／`expires_at`
  snapshot**（DATA_MODEL §2.5 驗證規則），人類只需核准內容本身，不需手動輸入 hash。
  `pending_proposals` SQLite 表只存每個 `proposal_id` 的 current revision（含新增的
  `current_revision` 欄位），`rebuild-cache` 時從 `proposals.jsonl` 重建，不會因 cache 被刪而遺失
  待核准的提案。**`constraint_propose`／`decision_propose` 本輪新增可選參數**：`critical`（僅
  decision）、`source_document`/`source_section`、`machine_check_hint`（僅 constraint），對應
  DATA_MODEL §2.5 新增欄位，人類在核准時可調整（`[E]dit` 流程），非必填。
- **Decision/Constraint staleness（與 Milestone 5 的 semantic hash 規則完全分離，見 DATA_MODEL §6）**：
  內容修改不觸發 Decision 變動；引用的 file/symbol 被刪除 -> 附加新 revision `status=review_required`
  （current 且 visible，附警告，不隱藏）；引用的整個 scope 消失 -> 附加新 revision `status=orphaned`
  （current 但不 visible）。Constraint 依 `persistence_mode` + snapshot 微調：`persistent` 永不自動
  變動；`scope_bound` 比對現況 membership hash 與核准時的 `scope_hashes` snapshot；`source_bound`
  比對現況 content_hash 與核准時的 `source_hashes` snapshot（不符 -> `stale`）；`temporary` 比對
  `expires_at`。這些「系統偵測到偏離」觸發的新 revision，`created_by` 標記為
  `RevisionAuthor.system_staleness`/`system_lifecycle`（DATA_MODEL §2.5 的 enum，取代原本的
  `Literal["agent","human"]`——後者會讓系統自動附加 revision 直接 validation fail），不需要人類先
  核准才能附加——只有「新增權威內容」才需要人類核准。**這些系統附加的 revision 必須是前一筆 current
  revision 的完整 snapshot**（複製 `content`/`scopes`/`files`/`symbols`/`source_hashes`/
  `scope_hashes`/`expires_at` 等全部欄位，只改動 status 與 lifecycle metadata），不得只寫入
  `{record_id, revision, status}` 的殘缺內容，否則會遺失 snapshot 導致下次 staleness 判斷失去比對
  基準（DATA_MODEL §2.5）。
- **Note revision 化（見 DATA_MODEL §2.6）**：Note 的 `id` 為邏輯身分，`revision` 單調遞增，
  current = `max(revision)`，與 Decision/Constraint 共用同一套「current 與 visible 分離」邏輯，但
  **不需要 proposal/human approval 關卡**——agent 可以直接呼叫 `note_add`（新 `id`）或
  `note_update`（既有 `id`，附加新 `revision`）。依 category 有 TTL 與 source-hash-snapshot
  staleness；寫入前套用與 Milestone 5 相同的 redaction 模組（重用，不重寫）。`stale` 的 Note current
  revision 預設仍出現在 retrieval，標記 `[STALE]`；只有 `expired`／`orphaned` 預設排除，`archived`
  僅 history 模式。**`note_revisions` SQLite 表必須完整投影 `source_hashes`/`evidence`
  （`source_hashes_json`/`evidence_json` 欄位，本輪新增）**——先前草案漏了這兩欄，會導致
  canonical Note 明明有 staleness snapshot，SQLite 的 current 投影卻遺失它，與 Constraint 已經把
  `source_hashes_json`/`scope_hashes_json`/`expires_at` materialize 進 SQLite 的做法不一致。
- Orphan 偵測：Decision/Constraint/Note 引用的所有 scope/file/symbol 皆已從索引刪除時，附加新
  revision `status=orphaned`，預設 retrieval 排除（規格 §25、§32）。
- **單一寫入者衝突偵測（本輪新增，見 DATA_MODEL §1、§8）**：`materialize.py` 解析
  `decisions.jsonl`/`constraints.jsonl`/`notes.jsonl` 時，若同一 `(record_id/id, revision)` 出現
  重複，中止該次 materialize（不寫入任何一筆），回報衝突供 `rune doctor`/`rune update` 顯示，絕不
  自動選擇任一筆。
- Materialize 時填入 FTS5（`fts_decisions`、`fts_constraints`、`fts_notes`）。
- `rune search` 接上 `core.retrieval.search`，套用 ARCHITECTURE §4.8 的八層排序（Global MUST >
  Scoped MUST > Active Decision > Scoped SHOULD > Fresh Semantic > Fresh Note > Stale Note >
  Historical，本輪修訂，取代原本規格 §46 的六層版本），並在結果中明確標示 `review_required`/
  `stale` 狀態而非隱藏。Global/Scoped 的判斷純粹讀 `constraint_scopes` 是否有對應列，不需要新欄位。
- `rune check`：git diff -> 受影響 files/scopes -> 相關 constraint 清單（規格 §39），此 milestone
  不需要模型呼叫。

**驗收標準：**
- **`current_revision` 計算的單元測試明確覆蓋 `rev1=active, rev2=inactive` 這個 case**：
  current 必須是 rev2（`status=inactive`），而非退回顯示 rev1——這是本輪修正的 bug，必須有測試鎖死，
  防止實作時重新引入「用 status 過濾 current」的邏輯。
- 同一 `record_id` 的兩個 revision：預設 search/retrieval 只顯示最新的 current 一筆（依其 status
  決定 visible 與否）；明確加 history flag 才顯示兩筆（規格 §67）。
- 一個 Decision 引用的檔案內容被修改（但檔案與 symbol 都還在）：不附加新 revision，current 不變，
  仍是 `active`。
- 一個 Decision 引用的 symbol 被刪除（檔案還在）：附加新 revision `status=review_required`，current
  變為該筆，**仍出現在預設 retrieval**，附帶覆核標記；引用的 scope 整個被刪除後，才附加
  `orphaned` revision 並被排除。
- 一個 `source_bound` Constraint：核准時自動填入 `source_hashes` snapshot；之後修改對應檔案內容，
  下次 `rune update` 偵測到 hash 不符，附加新 revision `status=stale`，current 變為該筆且
  visible（附警告）；核准時若漏填 snapshot，核准流程本身直接拒絕（規格層驗證，非事後補救）。
- 一個 `temporary` Constraint：核准流程若未提供 `expires_at` 直接拒絕；超過 `expires_at` 後附加新
  revision 轉為 `stale`。
- 過了 TTL 的 `temporary_context` Note：附加新 revision `status=expired`，current 變為該筆，
  預設 search 中不出現（規格 §68）。
- 一個 Note 被「更新」（例如 `implementation_detail` 對應的檔案改了，先轉 `stale`，之後 agent
  驗證問題已解決，附加新 revision `status=archived` 並附上新內容說明）：`rune search` 對該 `id`
  只看到最新 revision 的狀態，不會同時看到新舊兩份互相矛盾的 active 內容。
- 刪除 proposal 對應的 `pending_proposals` SQLite 快取後（模擬 cache 損毀），`rune rebuild-cache`
  能從 `proposals.jsonl` 完整還原待核准的提案，狀態與內容不變。
- 刪除某個 scope 後，其下的 Decision/Constraint/Note 被 orphan，而非導致 materialize 崩潰。
- 手動在 canonical JSONL 中製造重複 `(record_id, revision)`：`rune update`/`rune rebuild-cache`
  中止並回報衝突，不寫入 SQLite，不默默選擇任一筆。

## Milestone 7 — OpenCode Adapter
**模組**：`adapters/opencode`（TypeScript），以及支援它所需的少量 `rune.cli` 新增子命令
（`--json` 輸出）。**本輪已對照 OpenCode 官方 plugin 文件確認以下 API 存在**：`session.created`
等 session event、`tool.execute.before`/`tool.execute.after`、custom tool 註冊（context 帶
`sessionID`/`messageID`/`directory`/`worktree`）、內建 `apply_patch` 也可攔截（path 需從 patch
text 解析）。

**開工前先做一個小 spike（本輪新增，不算完整 milestone 工作量，但必須先做）**：只驗證
「`tool.execute.before` → `rune scope-for --path ... --json` → 注入 context」這條最關鍵路徑能跑
通，確認 tool context 能正確取得 `directory`/`worktree` 定位到正確的 `.rune/`，再進入下面的全量開發。

**交付項目：**
- **`rune bootstrap --mode hard|soft --json`（本輪新增，見 ARCHITECTURE §7.6）**：新 CLI 子命令，
  `core.retrieval.context` 新增 `build_hard_bootstrap_context()`/`build_soft_bootstrap_context()`。
  Hard 輸出只含 current+visible 的 Global MUST Constraint（`scopes==[]` 且 `severity=MUST`）與
  `critical=true` 的 global Decision，附 `estimated_tokens`/`budget_tokens`/`overflow` 欄位（讀取
  `config.bootstrap.hard_budget_tokens`）；**超出 budget 時 `overflow=true`，絕不靜默丟棄任何
  MUST 規則**（ARCHITECTURE §7.7）。Soft 輸出含 project overview、主要 scope、其餘 active global
  decision、新鮮度資訊。兩者都只回傳結構化 JSON，不含任何 prompt 措辭。
- **Session-start hook（`session.created`）**：依序呼叫 `rune bootstrap --mode hard --json` 與
  `--mode soft --json`，分別注入 hard/soft context。不在此自動觸發昂貴的 `rune update`（規格 §54）。
- **Session-compaction hook（`session.compacted`，本輪新增）**：只重新呼叫
  `rune bootstrap --mode hard --json` 並重新注入，不重送 soft context。Adapter 維護
  `hard_context_generation` 計數器（`session.created`=generation 1，每次 compaction +1），**每個
  generation 只送一次 hard bootstrap**，此計數器與 `active_scope_ids`（scope 是否已注入過）完全獨立
  維護，不可共用同一個旗標（ARCHITECTURE §6）。
- **Constraint delivery（scoped constraint）掛在 `tool.execute.before`，不是 `file.edited`**：
  `file.edited` 在檔案已經被改完後才觸發，時機太晚；`tool.execute.before` 才能在 agent 碰檔案
  「之前」注入 constraint。依 `input.tool` 分流：
  - `read`/`edit`/`write`：直接從 tool 參數取得受影響 path
  - `apply_patch`：解析 `patchText` 取得受影響 path
  - `bash`：V1 不嘗試解析 shell command 語意，改在 `tool.execute.after` 用 `git diff`（或
    `file.watcher.updated` 事件）偵測實際變動的檔案，事後才做 scope 對應與注入
  - 取得 path 後呼叫 `rune scope-for --path ... --json`（對應邏輯在 core，adapter 只呼叫）；每個
    scope 在同一 session 中首次觸及時注入一次 scope-activation context（`active_scope_ids` 快取，
    規格 §37）；本 hook 只注入 context，不阻擋 tool 執行。**這是與 hard bootstrap 完全分開的注入
    路徑**——scoped constraint（無論 MUST 或 SHOULD）一律不進 hard bootstrap，hard bootstrap 也
    一律不含 scoped constraint。
- `decision_propose`/`constraint_propose`/`note_add` 註冊為 OpenCode custom tool，皆直接呼叫對應的
  `rune decision-propose --json`/`rune constraint-propose --json`/`rune note-add --json`，帶入
  tool context 提供的 `sessionID`/`directory`/`worktree`（propose 寫入 `proposals.jsonl`；note
  直接寫入 `notes.jsonl`）。
- **邊界規則**：adapter 與 core 之間唯一合法介面是 CLI 的 `--json` 輸出；TypeScript 端絕不直接
  import Python 邏輯或碰 SQLite/`.rune/` 檔案，也不嘗試解析 shell command 語意。Hard bootstrap 若
  OpenCode 提供 system/instruction 層級的 context 注入 API，優先使用該層，不當成一般訊息送出
  （ARCHITECTURE §6）。

**驗收標準（規格 §66「constraint tests」，本輪擴充 hard bootstrap 相關案例）：**
- **Session start**：Global MUST constraint C1 存在時，新 session 建立後 hard bootstrap context
  中出現 C1。
- **Compaction 重新注入**：C1 已於 generation 1 注入過；`session.compacted` 發生後，C1 在新的
  generation（generation 2）中被重新注入——不能因為「compaction 摘要可能已經包含 C1」就跳過。
- **Inactive 規則排除**：C1 有 `rev1=active`、`rev2=inactive`；新 session 啟動時，hard bootstrap
  **不得**出現 C1（current revision 是 rev2，`inactive` 不在 visible 集合內）。
- **更新後只顯示最新內容**：C1 有 `rev1`（舊內容）、`rev2=active`（新內容）；compaction 後注入的
  hard bootstrap **只含 rev2 的內容**，不會新舊版本同時出現或注入舊版本。
- **Scoped 規則不進 global**：`auth` scope 有一條 MUST constraint；session 啟動但 agent 尚未碰
  `auth` 相關檔案時，該 constraint **不**出現在 hard bootstrap；agent 透過 `tool.execute.before`
  碰觸 `auth` 內的檔案後，才透過既有的 scope-activation 機制注入。
- Agent 對某個帶 MUST constraint 的 scope 內的檔案執行 `read`/`edit`/`write`：`tool.execute.before`
  攔截到該次呼叫，constraint 在工具實際執行「之前」已注入（不是事後才補）。
- 同一 scope 在同一 session 中第二次存取：不重複注入。
- Agent 透過 `bash` 間接修改某 scope 內的檔案：`tool.execute.after` 之後能偵測到變動並補上該 scope
  的 context 注入（即使晚於實際修改，也不遺漏）。
- 此 milestone 的驗收標準是規格中明確點名「V1 真正價值的驗證階段」，須視為不可妥協的硬性標準，而非
  加分項。

## Milestone 8 — MCP + Polish
**模組**：`rune.mcp.server`、`rune.cli`（`doctor`）、打包。

**交付項目：**
- MCP server 暴露規格 §56 列出的十個 tool，每個都是既有 `core` 函式的薄包裝（不在此引入新商業邏輯）。
- `rune doctor`：config 合法性、canonical schema version、SQLite 可開啟性/健康度、git 可用性、
  Tree-sitter parser 可用性、provider/API key 是否存在（只檢查存在與否，絕不記錄 key 值本身）、
  canonical 與 cache 的一致性檢查（例如比對 `schema_meta.materialized_from_head`/
  `materialized_from_tree_hash` 與 `project.json` 對應欄位）。**本輪新增 Global MUST 治理警告**
  （ARCHITECTURE §7.7）：估算目前 hard bootstrap 的 token 數，若超過
  `config.bootstrap.hard_budget_tokens` 印出 overflow 警告（不自動處理，要求人類精簡）；若 global
  MUST 數量超過 `config.bootstrap.must_count_warn_threshold`（預設 30）印出整併建議，例如
  「42 條 global MUST 估計需要 6.8k token，建議整併或把可機械驗證的規則移交 linter」。同時檢查是否
  存在「global（`scopes==[]`）但 `persistence_mode` 非 `persistent`」的 constraint，若有則警告
  （語意可疑，見 ARCHITECTURE §7.2），但不阻擋核准/不自動修正。
- 打包，使 `uv tool install rune`（或等效方式）可從已建置的 wheel 安裝。
- 文件補完：README quickstart 對應規格 §79 的 UX walkthrough。

**驗收標準：**
- `rune doctor` 正確標出至少：缺少 API key 環境變數、`memory.db` 損毀（模擬截斷檔案）、canonical 檔案
  帶未知/未來的 `schema_version`。
- `rune doctor` 在製造 35 條 global MUST constraint 的 fixture 上，正確印出數量門檻警告；在
  hard bootstrap 估算 token 數超過 `hard_budget_tokens` 的 fixture 上，正確印出 overflow 警告，且
  `rune bootstrap --mode hard --json` 本身仍完整回傳全部 MUST（`overflow=true`），不因超出 budget
  就少回傳任何一條規則。
- MCP tool 呼叫與對應 CLI 指令產生相同的資料形狀（兩個介面之間不 drift）。

## 測試計畫（跨 milestone，規格 §65-68）
- **單元測試**：hash 計算、canonical 解析/atomic-write、**revision「current」選取必須是純
  `MAX(revision)`，明確包含 `rev1=active, rev2=inactive` -> current 是 rev2 這個回歸測試**（可見性
  過濾是另一層、另一組測項，覆蓋 `active`/`review_required`/`stale`/`orphaned`/`inactive` 各自的
  visible 與否）、Decision/Constraint 生命週期轉換（含存在性驅動規則、`source_hashes`/`scope_hashes`
  snapshot 比對、`temporary` 的 `expires_at` 驗證）、Note revision 化（`id`+`revision` 的 current
  選取與 Decision/Constraint 共用同一套邏輯）、Note TTL、scope membership（多重）、ScopeSummary
  `source_files` 的 union invariant（純 symbols 成員時仍能解析出 owning file）、staleness
  （Decision/Constraint 的 snapshot-vs-現況規則與 semantic 的 hash 規則須各自獨立測試，不可共用同一組
  測項）、secret redaction 樣式、FTS 索引正確性、`init --force` 對七個 canonical 檔案（含
  `scopes.json`/`semantic.jsonl`）的保留、`proposals.jsonl` 的 rebuild-cache 還原、canonical JSONL
  中重複 `(record_id/id, revision)` 的衝突偵測與拒絕寫入。
- **整合測試（fixture repo）**：init、update（無變更＝no-op）、檔案變更、新增檔案、刪除檔案、symbol
  rename、scope 變 stale 全流程、semantic 重新產生全流程、`rebuild-cache` 等價性（同一 canonical
  狀態下，重建出的 DB 與增量更新出的 DB 一致——這是全計畫中最重要的單一整合測試，因為它是 V1 核心
  持久性保證的直接驗證）。
- **Constraint delivery 測試**：bootstrap 時的全域 MUST、`tool.execute.before` 攔截時的 scoped
  constraint（含 `read`/`edit`/`write`/`apply_patch`/`bash` 各自的 path 擷取方式）、重複存取不重複
  注入——在 Milestone 7 對 OpenCode adapter 執行。
- **Hard/Soft bootstrap 測試（本輪新增，見 Milestone 7 驗收標準的五個 Given/When/Then 案例）**：
  session start 含 global MUST、compaction 重新注入（含新 generation 判定）、inactive rule 排除、
  updated rule 只顯示最新內容、scoped rule 不進 global bootstrap。這五項是規格§21 明確要求的
  scenario-based test，須逐一寫成獨立測試案例，不可合併成一個大案例。
- **Revision/audit 測試**：預設只看 current 版本 vs. 明確要求看完整歷史。
- **Note 生命週期測試**：TTL 到期、source hash 觸發 stale（且確認 stale 仍出現在預設搜尋，只是標記）。
- **韌性測試（本輪新增）**：reference 解析大範圍缺漏時 rune 仍可運作（Milestone 3）；semantic
  provider 連續失敗時 fallback policy 正確終止而非無限重試（Milestone 5）；SQLite reader 在
  `rune update` 執行中途讀取，只看到上一次完整 materialize 的快照，不讀到中間狀態（Milestone 1）。
- **Windows/編碼測試（本輪新增，併入 fixture repo，不獨立立項）**：fixture repo 內含 Unicode 路徑、
  中文檔名、CRLF 換行的檔案，驗證 hashing/atomic-write/git subprocess 行為正確。

## 設計決策記錄（本次討論已確認，取代先前的「開放問題」清單）

1. **`rune init` 冪等性**：`.rune/` 已存在時拒絕，提示改用 `rune update`；`--force` 只重建結構性部分，
   絕不覆蓋 `decisions.jsonl`/`constraints.jsonl`/`notes.jsonl`/`proposals.jsonl`。
2. **Symbol rename 語意**：視為刪除+建立（新 `symbol_id`），V1 不做 fuzzy rename 追蹤。
3. **Semantic 驗證的 strip vs reject 分界**：核心欄位（`purpose` 等）schema 不成立 -> 整份拒絕；清單
   欄位中個別引用不存在的 file/symbol -> strip 該條目，不拒絕整份。
4. **Decision staleness 不套用來源 hash 規則**：內容修改不觸發變動；引用項目被刪除 -> `review_required`
   （不隱藏）；引用 scope 整個消失 -> `orphaned`。這與 semantic summary 的 hash-based staleness 是兩套
   獨立機制，`core.memory.staleness` 與 `core.semantic.worker` 不共用邏輯。
5. **Stale Note 的可見性**：`stale` 仍出現在預設 retrieval，標記 `[STALE]`；只有 `expired`/`orphaned`
   預設排除；`archived` 僅 history/audit 模式。
6. **Proposal 持久化**：新增 canonical `.rune/proposals.jsonl`（append-only），`pending_proposals`
   SQLite 表僅為其衍生快取，`rebuild-cache` 時完整還原。是否 commit 進 git 由
   `config.proposals.commit_to_git`（預設 `false`）決定。
7. **Tree-sitter 套件選擇**：採 `tree-sitter-python`/`tree-sitter-javascript`/`tree-sitter-typescript`
   三個獨立套件，非 bundle package。
8. **OpenCode adapter 介面**：CLI 新增 `--json` 輸出模式（`rune status --json`、
   `rune scope-for --path ... --json`、`rune decision-propose --json` 等）作為 adapter 與 core 之間
   唯一合法介面，adapter 不直接 import Python 邏輯。
9. **Working-tree fingerprint（本次新增）**：`project.json` 除 `last_indexed_head` 外，新增
   `last_indexed_tree_hash`（rune 自算的已索引檔案 hash 合併值），用於判斷「HEAD 未變但 working tree
   已改動」的新鮮度情境，實作於 Milestone 2。
10. **ScopeSummary 逐檔 hash 明細**：`ScopeSummary` 除 `source_hash` 外新增
    `source_files: dict[path, content_hash]`，讓 staleness 偵測能定位「哪些檔案變了」，支援 semantic
    worker 未來做增量重新產生（只餵差異部分給模型，降低 refresh 成本），實作於 Milestone 5。

### 第二輪修訂（revision lifecycle 修 bug）

11. **Current 與 visible 分離（修正根本性 bug）**：`current_revision` 一律是 `MAX(revision)`，與
    `status` 完全無關；是否出現在預設 retrieval（以及顯示形式）由 current revision 的 `status`
    另外決定（DATA_MODEL §3、§6 的可見性表）。先前版本把「current = `{active, review_required}`
    中最大者」當定義，會導致 `rev1=active, rev2=inactive` 時錯誤地繼續顯示 rev1，已在 Milestone 6
    修正並加專門單元測試鎖死。
12. **`source_bound`/`scope_bound` Constraint 補上核准時 snapshot**：`MemoryRevision` 新增
    `source_hashes`（核准當下對 `files`/`symbols` owning file 的 content_hash 快照）與
    `scope_hashes`（核准當下對 `scopes` membership 的雜湊快照），staleness 判斷改為「現況 vs.
    snapshot」而非「現況 vs. 不存在的基準」，讓這兩種 persistence_mode 真正可實作。核准流程強制驗證
    這些欄位依 `persistence_mode` 正確填寫。
13. **`temporary` Constraint 補上 `expires_at`**：`MemoryRevision` 新增 `expires_at`，
    `persistence_mode == temporary` 時核准流程強制要求填寫，其餘情況強制為 `null`。
14. **Note 改為 revision 化**：`Note` 新增 `revision` 欄位，`id` 為邏輯身分，current =
    `max(revision)`，與 Decision/Constraint 共用「current 與 visible 分離」規則，但不需要 proposal/
    human approval——agent 可直接附加新 revision 來更新/封存一則 Note，解決「同一 `id` 出現兩行時
    誰是 current」的未定義問題，實作於 Milestone 6。
15. **ScopeSummary `source_files` 推導 invariant**：明確定義為
    `scope.members.files ∪ {owning_file(s) for s in scope.members.symbols}`，避免只用 symbol
    納入成員的 scope 得到空的 `source_files`，實作於 Milestone 5。
16. **單一寫入者假設與衝突偵測**：V1 明確假設每個邏輯 record 同時只有一個寫入者；`materialize.py`
    偵測到同一 `(record_id/id, revision)` 重複時視為 canonical 衝突，中止 materialize 並由
    `rune doctor`/`rune update` 回報，不自動選擇任一筆。多人協作場景（`revision` 升級為
    `revision_id`/ULID + `parent_revision_id`）留待未來版本，V1 不預先設計。
17. **`init --force` 收斂為完全非破壞性（修正先前的保護範圍不足）**：`--force` 絕不清空或重建任何
    已存在且合法的 canonical memory，包含 `scopes.json` 與 `semantic.jsonl`（先前版本誤將這兩者排除
    在保護範圍外，會導致 scope 定義與 semantic summary 一併消失）；只允許補建缺少的檔案、修復
    config/schema 骨架、重建 SQLite cache、重建決定性索引。真正需要清空重來時，未來提供獨立的
    `rune reset`。

### 第三輪修訂（型別矛盾與 append-only 語意修 bug）

18. **`created_by` 改為 `RevisionAuthor` enum**：原本 `Literal["agent", "human"]` 與「系統偵測到
    staleness 時自動附加 revision」的要求直接矛盾（會 Pydantic validation fail）。新增
    `RevisionAuthor`（`agent`/`human`/`system_staleness`/`system_lifecycle`），`MemoryRevision.
    created_by` 與 `Note.source` 都改用此 enum，實作於 Milestone 1（model 定義）與 Milestone 6
    （實際產生 system 觸發的 revision）。
19. **系統自動附加 revision 必須是完整 snapshot**：明寫成 invariant——system-generated lifecycle
    revisions are full snapshots of the previous current revision with only status/lifecycle
    metadata changed unless the transition explicitly requires otherwise。適用於 Decision、
    Constraint、Note 三者，實作於 Milestone 6，需要專門單元測試驗證新 revision 未遺失
    `source_hashes`/`scope_hashes`/`scopes`/`files`/`symbols` 等欄位。
20. **Proposal 改為 revision 化**：`proposals.jsonl` 先前定義為 append-only 卻要求「改 status」，
    未定義 materialize 該怎麼選——現改為 `proposal_id` + `revision`，current = `max(revision)`，
    與 Note/Decision/Constraint 共用同一套「current」規則。核准/拒絕/編輯都是附加新 revision，不原地
    改寫。`pending_proposals` SQLite 表相應新增 `current_revision` 欄位，實作於 Milestone 1（model）
    與 Milestone 6（proposal 流程）。
21. **`note_revisions` SQLite 補齊 `source_hashes_json`/`evidence_json`**：先前 schema 遺漏這兩欄，
    導致 canonical Note 的 staleness snapshot 在 SQLite 投影中消失，與 Constraint 已經完整投影
    snapshot 欄位的做法不一致，本輪已補齊，實作於 Milestone 1（schema）與 Milestone 6（materialize
    邏輯）。
22. **`temporary` Constraint 過期後確定轉為 `stale`，非 `inactive`**：過期不代表規則從未存在或肯定
    不再需要，`stale`（顯示 + warning）比 `inactive`（不顯示）更安全；真正確認不需要時，由人類手動
    再轉一次 `inactive`。移除先前「或依 config 決定，Milestone 6 再定」的未決狀態，實作於
    Milestone 6。

### 第四輪修訂（對照 OpenCode 官方文件、風險對策具體化）

23. **Milestone 7 核心假設經確認成立**：對照 OpenCode 官方 plugin 文件，`session.created` 等 session
    event、`tool.execute.before`/`tool.execute.after`、custom tool 註冊皆為真實存在的 API。不需要
    推翻 Milestone 7 設計，但 constraint delivery 的關鍵 hook 由原本設想的 `file.edited` 改為
    `tool.execute.before`（時機更早，符合「碰檔案之前就要看到 constraint」的實際需求），`bash` 則
    改為 `tool.execute.after` + `git diff` 事後偵測，不嘗試解析 shell command 語意。開工前先做一個
    小 spike 驗證這條路徑，實作於 Milestone 7。
24. **Import/reference 信任層級原則**：`imports` 是 high confidence，`references`/`calls`/
    `extends`/`implements` 是 best-effort；Scope/Constraint 系統不得把 reference graph 當唯一
    依據，reference 解析大範圍失準時 rune 仍須正常運作。這是本輪認定「工程風險最高」的區塊採取的
    韌性對策，不是解決底層解析難度本身，實作於 Milestone 2–4，驗收見 Milestone 3。
25. **Scope clustering 品質定位為實驗，非正確性需求**：不在架構/設計階段鎖死 clustering 演算法參數，
    Milestone 4 新增「真實中型 repo 品質實驗」驗收項，收集 precision/可用率資料而非要求固定
    threshold。這是本輪認定「產品效果風險最高」的區塊採取的對策。
26. **Semantic worker fallback policy 明確為有限步驟**：primary model -> 1 次 repair-prompt retry
    -> fallback model -> 保留舊 summary 停止，不做無限重試；同時追蹤
    `schema_success_rate`/`reference_strip_rate`/`fallback_rate`/`provider_error_rate`/`cost`/
    `latency` 六項 metrics，供未來用真實數據比較 provider/model，而非憑印象判斷，實作於 Milestone 5。
27. **SQLite 併發策略確定**：`PRAGMA journal_mode=WAL` + `PRAGMA busy_timeout=5000`，`rune
    update`/`rebuild-cache` 是唯一 writer 且整次 materialize 包在單一 transaction 內一次 commit，
    reader 永遠讀到一致快照。V1 不支援多個 `rune update` 行程並行，交由 `busy_timeout` 逾時報錯處理，
    不建 queue，實作於 Milestone 1。
28. **`ParserAdapter` 介面確定為「輸出形狀契約」而非「跨語言演算法規範」**：`qualified_name` 的實際
    演算法由各語言的 parser adapter 自行決定，架構層只保證輸出符合 `Symbol` model，不要求 Python/
    TypeScript/JavaScript 用同一套規則，實作於 Milestone 2。
29. **`PricingConfig` 純估算、Windows 相容性併入既有測試**：`config.toml` 新增
    `[pricing].input_per_million`/`output_per_million`，人類手動維護，不追求自動同步真實 provider
    定價；Windows 的 Unicode 路徑/中文檔名/CRLF/subprocess 編碼併入既有 fixture repo 整合測試，不
    另立里程碑或獨立測試分類。這兩項本輪確認不再視為需要進一步設計的風險項。

### 第五輪修訂（Global Code Standards / Hard Policy Injection，agent-injection semantics 正式擴充）

30. **Global MUST Constraint 的判定不需要新欄位**：一個 Constraint 的 current revision 若
    `scopes == []`（`constraint_scopes` 無對應列）即為 global；global 且 `severity == MUST` 即為
    Global MUST Constraint。`persistence_mode` 理論上應為 `persistent`，`rune doctor` 對「global
    但非 persistent」發出警告但不阻擋，實作於 ARCHITECTURE §7.2、Milestone 8（doctor 檢查）。
31. **`MemoryRevision` 新增四個選填欄位**：`critical`（bool，decision-only，標記進 hard
    bootstrap）、`source_document`/`source_section`（選填字串，追溯到 `CODE_STANDARDS.md`）、
    `machine_check_hint`（選填字串，constraint-only，記錄可對應的驗證工具名稱）。皆為向後相容新增
    （有預設值），需補進 `models.py`（Milestone 1 待辦）與對應的 `decision_revisions`/
    `constraint_revisions` SQLite 欄位（DATA_MODEL §5）。
32. **Hard/Soft Bootstrap 拆分，取代原本單一的 project bootstrap context**：Hard bootstrap 只含
    global MUST constraint 與 `critical=true` 的 global decision，在 `session.created` 與
    `session.compacted` 都重新注入；Soft bootstrap 含 project overview 等，只在 `session.created`
    注入一次，compaction 後不整份重送。Compaction 視為「可能失憶事件」，authoritative hard policy
    永遠由 rune 重新送出，不依賴 agent 自己的 context compression 是否保留規則，實作於 Milestone 7。
33. **`hard_context_generation` 與 `active_scope_ids` 是兩個獨立的 dedup 機制**：前者追蹤 hard
    bootstrap 是否已對目前這一代 context 送過（`session.created`=1，每次 compaction +1，每代送一
    次）；後者追蹤 scope 是否已注入過。兩者用途不同，不可合併成同一個旗標，實作於 Milestone 7 的
    OpenCode adapter。
34. **Retrieval 排序改為八層，插入 Global/Scoped MUST 的區分**：Global MUST > Scoped MUST > Active
    Decision > Scoped SHOULD > Fresh Semantic > Fresh Note > Stale Note > Historical，取代原本
    規格 §46 的六層版本。衝突時最新的 current Global MUST Constraint 優先於其他任何 memory 內容，
    實作於 Milestone 6（`core.retrieval.search`）。
35. **Token budget 的 overflow-not-truncate 規則**：Hard/soft bootstrap 各有獨立 token 預算
    （`BootstrapConfig`，預設 hard=3000/soft=8000），global MUST 集合超出 hard budget 時
    `overflow=true` 並顯著回報，**絕不靜默丟棄任何一條 MUST 規則**；`rune doctor` 在 global MUST
    數量超過 `must_count_warn_threshold`（預設 30）時建議整併或把可機械驗證的規則移交 linter，實作
    於 Milestone 7（CLI 輸出）與 Milestone 8（doctor 警告）。
36. **三層規範分工不進 rune 的部分**：quote style、import 排序、格式化、基本 lint、型別檢查、單元
    測試等「可機械驗證」的規則完全交給 formatter/linter/typechecker/pytest 執行，不進 rune canonical
    memory，不佔 LLM context；`CODE_STANDARDS.md` 是完整人類可讀文件，rune 只保存真正影響 agent
    行為的 MUST/SHOULD 子集，兩者不要求逐條同步。這條原則本身不需要程式碼實作，是治理慣例，記錄於
    ARCHITECTURE §7.1 供未來寫 `CODE_STANDARDS.md` 時參照。
37. **Decision 與 Constraint 的分類邊界重申**：使用者說「以後禁止用 X」→ Constraint proposal；
    「某個 library 有 bug」→ Note；「我們決定從 A 改用 B」→ Decision（必要時另外提出對應
    Constraint）。三者不可混用同一種 record type。這不是新規則，只是本輪明確點出常見誤用情境，供
    Milestone 6/7 的 propose 流程文件與 CLI help text 參照。

### 第六輪修訂（外部 code review 發現的 Milestone 1 實作落差）

本輪對已完成的 Milestone 1 程式碼做 code review，發現多處「文件說了、程式碼沒做到」的落差，逐一修正：

38. **`.rune/config.toml` 補上 `extra="forbid"`**：先前所有 config model 都是普通 `BaseModel`，
    Pydantic 預設會靜默忽略未知欄位——一個打錯字的 `[semanic]` 區塊會被整段無聲吞掉，使用者以為設定
    生效了，其實完全沒讀到。新增共用 `StrictModel` 基底類別（`model_config =
    ConfigDict(extra="forbid")`），所有 config class 改繼承它，未知欄位（含巢狀區塊內的）現在會直接
    拋出驗證錯誤，實作於 Milestone 1。
39. **Git repo 驗證改為真的問 git**：`find_repo_root` 原本只檢查 `.git` 路徑是否存在，一個偽造的
    `.git` 空目錄就能騙過去。改用 `git rev-parse --show-toplevel`（`subprocess`），失敗才視為
    `NotAGitRepoError`；同時這個做法能正確處理 git worktree（`.git` 是檔案而非目錄）等原本手動判斷
    容易漏掉的情況，實作於 Milestone 1。
40. **`config.toml`／`.gitignore`／空 JSONL 骨架補齊 atomic write**：`write_default_config`、
    `_write_gitignore`、`_touch_empty_jsonl_files` 先前直接呼叫 `Path.write_text`，違反 Milestone 1
    「canonical JSON/JSONL/TOML 全部走 atomic temp-file+rename」的要求。`canonical.py` 的
    `_atomic_write_text` 改為公開的 `atomic_write_text`，三處呼叫點都改用它，實作於 Milestone 1。
41. **資料模型補上文件早就寫明但沒真正檢查的邊界**：`MemoryRevision`/`Note`/`Proposal.revision`
    加 `Field(ge=1)`；`Edge.confidence`、`Note.importance`/`confidence` 收斂到 `[0.0, 1.0]`
    （共用 `Confidence` annotated type）；`created_at`/`generated_at`/`last_verified_at`/
    `expires_at`/`resolved_at` 等時間戳位改用共用的 `Timestamp` annotated type，驗證必須是
    ISO-8601 且 UTC offset 為零（拒絕 naive datetime 與非 UTC offset）；`Proposal.created_by` 從
    `str` 收斂為 `Literal["agent", "human"]`（proposal 只會是 agent 或 human 提出，不會是
    system-generated）。皆為既有欄位收斂型別，非新增欄位，實作於 Milestone 1，附帶單元測試（正向與
    邊界值兩種案例）。
42. **`scope_files`/`scope_symbols` 缺 FK 的張力，明確定案並記錄**：DATA_MODEL.md §5 的文件版
    schema 對這兩個 join 表的 `file`/`symbol_id` 宣告了 `REFERENCES files(path)`／
    `REFERENCES symbols(symbol_id)`，但實作故意省略——因為 `files`/`symbols` 表要到 Milestone 2
    才會被填入，而 `scopes.json` 在 M1 就可能已經帶 file/symbol membership。這是刻意的實作偏離，本輪
    在 `schema.sql`、DATA_MODEL.md、ARCHITECTURE.md 都補上明確註記：**Milestone 2 索引器落地後補上
    這兩個 FK**，並加對應回歸測試。同時發現先前完全沒有 `PRAGMA foreign_keys=ON`——即使宣告了 FK
    （例如 `symbols.file REFERENCES files(path)`），沒開這個 pragma 等於裝飾用、`ON DELETE CASCADE`
    從未真正生效。本輪已在 `materialize.connect()` 開啟，並確認既有的插入順序（父列先於子列）不受
    影響，實作於 Milestone 1。
43. **`rebuild_cache` 改為原地 transaction，不再用「另建檔案＋置換」**：這是本輪最重要的發現，起因是
    新增的併發測試（reader 開著讀取交易時觸發 rebuild）在 Windows 上直接讓 `os.replace` 拋出
    `PermissionError`——第一版的 crash-safety 修法（見上方「Milestone 1 目前狀態」說明）在有並行
    reader 時反而讓 writer 失敗，比原本要解決的問題更糟。修正為在同一個檔案內用一個 SQLite
    transaction 做「清空全部內容表（六個根表，靠 `ON DELETE CASCADE` 帶走所有衛星表）→ 重新寫入 →
    commit」，crash-safety 交給 SQLite 自己的 rollback journal，也因此天然相容 WAL 模式下的並行
    reader。實作於 Milestone 1，回歸測試：`test_reader_sees_consistent_snapshot_during_rebuild`、
    `test_duplicate_revision_raises_conflict_and_preserves_old_cache`。
44. **測試覆蓋補齊**：新增 atomic write 在 `os.replace` 前中斷的存活性測試（含檔案原本存在／不存在
    兩種情境）、git 驗證測試（假 `.git`／Unicode 路徑）、config 未知欄位拒絕測試（頂層與巢狀）、
    proposal 的 rebuild-cache 還原與核准後 current 更新測試、SQLite reader 併發快照測試。目前共
    41 個測試，實作於 Milestone 1。

### 第七輪修訂（Milestone 4 開工前，三個未鎖死的實作細節取得確認）

45. **Scope 候選不持久化，純一次性 CLI 互動**：`rune scope suggest`（heuristic 與 clustering 皆同）
    當場計算候選、當場印出、當場人類 `[y]es/[n]o` 確認，不新增任何 canonical 檔案（不做
    `scope_candidates.jsonl`）。理由：與 Decision/Constraint 的 `proposals.jsonl` 不同，後者代表
    「已有人類/agent 提出、尚待處理」的治理狀態，必須撐過重開機／cache 刪除；scope 候選在確認前只是
    一份可低成本重新計算的建議，跟 code index（files/symbols/edges）沒有 canonical 背書、每次直接
    從原始碼重新推導的精神一致，不需要為此另外設計一套持久化格式與對應的 CLI 子命令
    （`accept`/`reject`/`list-pending` 等）。若之後真的有需要跨 session 保留候選的情境，屬於未來
    版本可以再議的範圍，V1 不預先設計，實作於 Milestone 4。
46. **Clustering 候選建議可同時使用 import 與 best-effort reference edge**：ARCHITECTURE §4.3
    「Scope/Constraint 系統不得把 reference graph 當唯一依據」管的是治理系統（無人把關的自動寫入），
    clustering 候選建議一定要經人類確認才會寫進 `scopes.json`，human review 本身就是對 reference
    解析不完美的防線。只用 import edge 會讓建議少掉「同檔案沒有 import 但透過繼承/呼叫高度耦合」這種
    真實架構訊號；reference 不完美的代價僅是「建議品質變差」而非「未經確認的錯誤決策」，可接受，
    實作於 Milestone 4。
47. **新檔案 incremental 自動併入只認 import edge，且僅限單一候選**：`rune update` 對新檔案，若透過
    `edge_type=imports`（`confidence=1.0`）恰好命中一個現有、非 `locked` 的 scope，自動加入該 scope
    的 `members.files`，不需人類確認、不觸發全庫重新分群。零個/多個候選、只有 best-effort reference
    命中、目標 scope 為 `locked`、任何移除 membership 或建立新 scope 的動作，一律落回人類確認流程。
    這個判準刻意比 clustering 建議嚴格，因為 incremental 自動併入是唯一無人把關的寫入路徑，只能使用
    §4.3 明定的 high-confidence 訊號，不能讓 best-effort reference 的不確定性滲透進自動寫入動作。
    `locked` scope 永遠不受任何形式（clustering 建議或 incremental 自動併入）影響，實作於 Milestone 4。
