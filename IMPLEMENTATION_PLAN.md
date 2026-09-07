# RepoRune（rune）— 實作計畫

狀態：**已確認（第十四輪修訂）**（V1 設計）。第六輪是外部 code review 對已完成的 Milestone 1 程式碼
做的落差修正（config 驗證、git 驗證、atomic write、model 邊界、FK/併發設計），細節見文末「第六輪
修訂」。第七輪是 Milestone 4（Scopes）開工前，針對規格中未鎖死的三個實作細節（候選 scope 是否
持久化、clustering 建議的訊號來源、incremental 自動併入的信心判準）取得確認，細節見文末「第七輪
修訂」與 ARCHITECTURE.md §4.4。**第九輪是使用者轉述的 6 條 Milestone 3／4 finding 逐條重現後的
修正**（unchanged caller 的 reference edge 不會重新解析、qualified/generic 繼承 reference 被丟棄、
extends/implements target 沒有 kind 限制、scope 自動併入在 rebuild_cache 前就寫入 canonical、真實
repo 品質實驗的第二個樣本改用真正中型的 repo、補齊 locked scope 的端對端測試），細節見文末「第九輪
修訂」。**第十輪是 Milestone 5（Semantic worker）開工前，修正 `ScopeSummary` 生成失敗語意的自相
矛盾**：新增 `revision` 欄位讓失敗狀態能真正跨 session 持久化，`last_error` 收斂為清洗過的分類
字串、原始錯誤另存不進 git 的本機 log，細節見文末「第十輪修訂」與 DATA_MODEL.md §2.4、
ARCHITECTURE.md §4.5。**第十一輪是 Milestone 5 的實作記錄**：`core.semantic.{provider,worker,
validation,redaction}` 完整實作、接線進 `rune update`（`rebuild_cache` 新增 `semantic_override`
比照 Milestone 4 的 `scopes_override` 模式）、以使用者提供的 OpenRouter API key 對
`qwen/qwen3.8-flash` 做真實端到端驗證（不只 mock），細節見文末「第十一輪實作記錄」。**第十二輪是
使用者要求對 Milestone 5 做的品質複查**，逐條重現後修正 3 個問題（`reference_strip_rate` 指標被
非 list 欄位灌水、provider 層級失敗被誤用 repair prompt 重試、同一根因導致的 `provider_error`
指標誤判），細節見文末「第十二輪修訂」。**第十三輪是使用者轉述的 10 條 Milestone 5 finding，逐條
重現後修正 9 個問題**（`rebuild-cache` 誤觸發 LLM、scope 刪除造成 materialize crash、多 scope
刷新時 canonical 非原子寫入、redaction 未尊重設定且遺漏 `dependencies`、schema 驗證誤將不合法型別
默默轉換、fallback model 產生內容被誤標、provider request 缺 structured output 提示、prompt 缺
實際程式碼、六項 metrics 未持久化），記錄 1 個待討論（provider 不可用時 `possibly_stale` 的觸發
邏輯），細節見文末「第十三輪修訂」與 ARCHITECTURE.md §4.5、DATA_MODEL.md §2.4、§5。**第十四輪是
使用者轉述的第二份 Milestone 5 code review，5 條 finding 全部修正**（SQLite cache 沒有 schema
migration、scope 刪除重建卡在 orphaned、CLI 說明文字過時、docstring 用詞不精確、`compute_
source_files` 邊界案例文件澄清），細節見文末「第十四輪修訂」。將規格
§70-77 展開為具體交付項目、模組目標與各 Milestone
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

**目前狀態：已實作並通過測試，並經三輪品質複查共修正 17 個問題**（`src/rune/core/semantic/
{provider,worker,validation,redaction}.py`，175 個測試全綠，`ruff check` 全綠）。細節、真實 API
驗證結果與已知未完成項見文末「第十一輪實作記錄」（初版實作）、「第十二輪修訂」（自我複查修正 3 個
metrics/prompt 邏輯問題）、「第十三輪修訂」（使用者轉述 10 條 finding，修正 9 個、記錄 1 個待討論）、
「第十四輪修訂」（使用者轉述第二份 code review，5 條 finding 全部修正）。

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
- **逐 scope 失敗隔離，失敗狀態需持久化（第十輪修訂，取代原本自相矛盾的版本）**：`ScopeSummary`
  新增 `revision` 欄位（DATA_MODEL §2.4），失敗時**附加新的一行**而非「不附加新行」：已有成功
  產生過的 scope，複製上一筆 current revision 的完整內容、只改動 `status=stale`、`last_error`、
  `generated_at`、`source_hash`/`source_files`（更新為目前的）；從未成功產生過的 scope，附加
  `revision=1`、`status=unavailable`，內容欄位留空不虛構。`last_error` 只寫入清洗過的分類字串
  （例如 `"provider_error:TimeoutError"`），原始例外/provider 回應內容寫進不進 git 的
  `.rune/logs/semantic.log`（`rune init` 時加進 `.gitignore`）。
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
- 模擬 provider 失敗：附加一筆新 revision，內容複製自上一筆 current revision（不虛構新內容），
  `status=stale`、`last_error` 為清洗過的分類字串，不損毀 `semantic.jsonl` 既有行，不影響本次
  update 的其他 scope；`rebuild-cache` 之後這個失敗 revision 仍然是 current（驗證失敗狀態真的跨
  session 持久化，而非本次 process 記憶體裡的暫態）。
- 模擬一個從未成功產生過摘要的 scope 首次生成即失敗：附加 `revision=1`、`status=unavailable`，
  內容欄位為空，不得出現任何虛構的 `purpose`/`responsibilities` 等內容。
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
   hard bootstrap 在每個正常 LLM request 持久重繪；`session.created`/compaction 僅負責重新取得資料，
   不採用 `hard_context_generation` one-shot 計數器。
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

**Milestone 8 已實作完成（第二十五輪修訂）**：見 `src/rune/mcp/server.py`、`src/rune/core/doctor.py`、
`src/rune/core/retrieval/{symbol_search,related_context,scope_read,changes}.py`。開工前確認了兩個
規格缺口，記錄如下：

1. **「規格 §56 的十個 tool」清單在本 repo 裡查不到**（原始規格文件不在 repo 內，只剩這份文件的引用）。
   跟使用者確認後，改用使用者親自指定的 **17 個** tool 清單（`rune_status`/`rune_doctor`/`rune_search`/
   `rune_symbol_search`/`rune_scope_for`/`rune_scope_read`/`rune_bootstrap`/`rune_related_context`/
   `rune_check`/`rune_changes`/`rune_decision_get`/`rune_constraint_get`/`rune_note_get`/
   `rune_decision_propose`/`rune_constraint_propose`/`rune_note_add`/`rune_note_update`），取代原本
   「十個」的假設——這是使用者的明確決定，不是本輪自行擴大範圍。刻意不暴露：`rune update`（LLM/網路
   成本 + 非顯式觸發的 canonical 寫入）、scope CRUD/`suggest`（設計上需要人類確認，ARCHITECTURE §4.4）、
   `proposal approve`/`reject`/`edit`（核准正是治理記錄存在的人類把關點，agent 自己核准自己的提案會
   讓整個機制失去意義）。
2. **`schema_meta.materialized_from_head`/`materialized_from_tree_hash` 實際上不存在於 `schema.sql`**
   （直接查證：`schema_meta` 只存過 `schema_version` 一個 key）。canonical/cache 一致性檢查改用
   `compute_status()` 既有的 `cache_usable`/`working_tree_fresh`（`rune status` 本來就用的同一套
   freshness 計算），不是新增 schema 欄位去對照一個規格草稿提到、但從未真正實作的欄位。

**新增的 core 模組**（`rune_related_context`/`rune_scope_read`/`rune_changes` 沒有既有 core 函式可
直接包，補的是「組合既有函式」而非新業務邏輯，符合「MCP 是薄包裝層」的原則）：
- `core.retrieval.symbol_search`：結構化 symbol 查詢（name/qualified_name/kind/path，可選配合
  `fts_symbols` 全文查詢），是 `core.retrieval.search`（純文字搜尋 Decision/Constraint/Note/語意摘要）
  刻意不涵蓋 symbol 的對應功能。
- `core.retrieval.scope_read`：依 scope_id 直接讀取單一 scope 的完整 agent-facing 視圖（metadata、
  members、語意摘要、current+visible 的 Decision/Constraint/Note）。重構 `scope_for.py`，把
  per-scope-id 查詢（`scope_summary`/`scope_constraints`/`scope_notes`）抽成 `scope_for`/`scope_read`
  共用的公開函式，避免同一段 SQL 出現兩份會漂移的拷貝；新增 `scope_decisions`（`scope_for` 先前只回傳
  Constraint/Note，從未有過 per-scope Decision 查詢）。
- `core.retrieval.related_context`：`rune_related_context` 的核心，組合
  `scope_for`/`scope_decisions`/`core.retrieval.search`/`symbol_search`，依 `path`/`symbol`/`query`
  聚合並排序 scopes/constraints/decisions/notes/semantic/symbols 六個 bucket；`max_items` 對每個
  bucket 各自截斷（不是總量預算）——刻意選擇，避免要 scope 又要 symbol 的呼叫者因為其中一個 bucket
  命中很多而被犧牲。至少要給 `path`/`symbol`/`query` 其中一個，否則 `RelatedContextValidationError`。
- `core.retrieval.changes`：重用 `rune check` 既有的 working-tree diff，額外回傳**全專案**（不只本次
  diff 觸及的）`possibly_stale`/`stale` 語意摘要清單——這是刻意的設計選擇：`rune check` 的 constraint
  清單本來就是「只看這次改動」的視角，但「哪些摘要該重新生成」是一個獨立於本次 diff 的待辦清單，
  agent 問「還有什麼要處理」時應該看到全貌。
- `core.retrieval.search.search()` 新增可選 `kinds` 參數（限定只查 decision/constraint/note/semantic
  其中幾種），供 `rune_search` 的 `kinds` 過濾用；預設 `None`（全部四種）與所有既有呼叫者行為完全不變。
- `core.memory.records` 新增 `get_decision`/`get_constraint`/`get_note`（單一 record 讀取，可選
  `include_history`），供 `rune_decision_get`/`rune_constraint_get`/`rune_note_get` 使用。

**`rune doctor`**（`core.doctor.run_doctor`）：git 可用性、config 合法性、tree-sitter parser
可匯入性、canonical 檔案 schema_version（重用 `read_json_model`/`read_jsonl` 既有的
`UnknownSchemaVersionError`，不重新實作版本檢查）、cache 可開啟性 + working-tree freshness、
semantic provider 設定（**刻意只做靜態檢查**——`semantic.model` 是否為空、對應的 API key 環境變數
是否存在——不呼叫 `check_semantic_health` 的即時連線探測，因為 `rune update` 本身已經做那個探測，
`doctor` 應該保持零副作用、隨時可跑）、Global MUST 數量門檻與 hard bootstrap token 預算警告（重用
`config.bootstrap.must_count_warn_threshold`，此欄位其實在 Milestone 5 那輪就已經加進
`BootstrapConfig`，本輪才第一次被消費）、global（`scopes==[]`）但 `persistence_mode` 非
`persistent` 的可疑 constraint 警告。`semantic/provider.py` 新增 `api_key_env_var()`
公開函式，讓 `doctor` 能查詢「這個 provider 該讀哪個環境變數」而不用重複 `_ENV_VAR_BY_PROVIDER`
這份對照表，也不需要真的呼叫 `build_provider()`。

**MCP server**（`rune.mcp.server`，用官方 `mcp` Python SDK 2.x 的 `MCPServer`/`@mcp.tool()`——
確認 pip 解出的是 2.1.1，其 `FastMCP` 已改名 `MCPServer`，import 路徑改為
`mcp.server.mcpserver`）：17 個 tool 全部是 `core` 函式的薄包裝，in-process 直接呼叫（跟
OpenCode adapter 不同——adapter 是獨立 TypeScript process，只能透過 `rune` CLI 的 `--json`
輸出溝通；MCP server 是 Python，直接 import `rune.core`，不需要、也不應該再繞經 CLI 子行程）。
`console_scripts` 新增 `rune-mcp` 進入點（`pyproject.toml`）。已用臨時 repo 手動端對端驗證過：
`rune_decision_propose` 只建立 pending proposal、在核准前 `rune_search` 找不到它；核准
（透過 CLI `proposal approve`）後才找得到；`rune_note_add`/`rune_note_get`/`rune_note_update`
的寫入-讀取往返；`rune_scope_read`/`rune_related_context` 對不存在的 scope_id／缺 selector
正確丟出可讀的 `ValueError`（MCP SDK 會把它轉成 tool error 回應，不是原始 traceback）。

**已知未做到、記錄而非忽略的缺口**：
- 驗收標準要求的「MCP tool 與對應 CLI 指令產生相同資料形狀，兩者不 drift」沒有自動化測試保護——
  MCP 回傳的 dict 形狀是手動比照 CLI 對應指令的 `--json` 輸出寫的（欄位名稱、巢狀結構刻意一致），
  但沒有像 `symbol_search`/`related_context` 的 core 函式那樣做到「唯一實作、兩處呼叫」，因為 CLI
  組 JSON 的邏輯目前就內嵌在 `cli/main.py` 各指令函式裡，不是獨立可重用的函式。留給未來一輪視需要
  抽出共用的 dict-shaping 函式。
- `rune_scope_for`/`rune_related_context` 的 `symbol` 參數目前是「用 `symbol_search(query=symbol)`
  取第一個命中結果的檔案」這種 best-effort 解析，不是精確的 symbol_id 查找；同名 symbol 出現在
  多個檔案時可能解析到非預期的那個。規格只說「輸入 path 或 symbol」，沒有進一步定義 symbol 參數該是
  qualified_name 還是 symbol_id 還是自由文字，此輪先用最寬鬆的全文比對滿足「可用」，精確化留待有
  真實使用回饋後再決定。
- `symbol_search()`/`related_context()`/`scope_read()`/`changes()` 目前只有 core 函式與 MCP tool
  暴露，沒有對應的獨立 `rune` CLI 子指令（`rune symbol-search`/`rune related-context` 例外——這兩個
  為了測試方便與既有 CLI 慣例一致而補上了；`rune scope-read`/`rune changes` 沒有）。MCP server 是
  in-process 直接呼叫 core，不需要 CLI 子指令才能用，所以不補這兩個純屬範圍控制，不是遺漏。

新增 20 個 regression test（`tests/unit/test_doctor.py`、`tests/integration/test_retrieval_
{symbol_search,scope_read,changes,related_context}.py`、`tests/integration/test_mcp_server.py`、
`tests/unit/test_cli.py` 新增 4 條），365 個測試全綠、`ruff check` 全綠。真的用臨時 git repo 跑過
`rune init` -> propose -> approve -> search 的端對端流程確認 MCP 工具行為正確，不只是單元測試 mock。

## Milestone 9 — Scope Governance
**模組**：`rune.core.scopes`、`rune.cli`、`rune.core.update` 的 scope-membership 邊界。

**目的：** 將既有的 incremental auto-assignment 泛化為可審核的 scope reconciliation，讓 merge、rename、
刪除與長期演變後的 membership 有明確治理流程；不把 Scope 變成可由模型任意重算的衍生資料。

**已定決議，實作不得改變：**
- 預設 reconciliation 只處理 changed set；untouched region frozen。
- 只有 import edge + 唯一 unlocked scope 的 high-confidence add 可 auto-apply。
- removal、move、模糊重新指派與新 scope 一律輸出 review，不得靜默改寫 canonical。
- `locked` scope 與 human-authoritative membership 永遠受保護；`--full` 也只能輸出 candidate/proposal/diff。
- large churn 必須中止 auto-apply 並要求人類審查；具體 threshold 需以實作/真實 repo 資料決定，不能自行猜定。
- `ScopeMembership` per-membership provenance schema 仍是 future/V2，不納入 M9；M9 必須誠實處理現行
  schema 無法判定個別 membership provenance 的限制。

**交付項目：**
- `rune scope reconcile` 與明確 opt-in 的 `--full`。
- 穩定、可機器解析的 `AUTO`/`KEEP`/`REVIEW`/`BROKEN` 結果，以及 changed/auto/review/keep count 與
  suspicious-churn flag。
- deterministic candidate/review proposal 產生與 human review workflow；不得新增模型直接改寫 membership 的路徑。
- merge/integration worktree 使用說明與 integration reconciliation 驗收流程。

**驗收標準：**
- changed file 的唯一 high-confidence import evidence 能 auto-add；reference-only 或多候選一律 REVIEW。
- untouched、locked 與 human-authoritative membership 在 incremental/`--full` 下都不被自動移除或搬移。
- 已刪除的 human/locked target 產生 BROKEN，不靜默清除。
- 觸發 large churn 時沒有 canonical auto-write，輸出明確要求 review。
- 相同輸入在不同 `PYTHONHASHSEED`/process 下得到相同排序與結果。

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

### 第八輪實作記錄（Milestone 4）

48. **Scope 實作採 canonical CRUD 加一次性 suggestion 的最小路徑**：新增
    `core.scopes.model` 的 create/edit/delete/lock/unlock 與 import-only incremental assignment，
    `heuristics` 的共同目錄候選，及 `clustering` 的 NetworkX connected-components。候選只在
    `rune scope suggest` 的當次互動存在；接受後才以 `source=model` 寫入 `scopes.json`。Clustering
    從 SQLite `edges` 讀取 `imports`/`calls`/`extends`/`implements`，但自動併入只接受新檔案指向
    單一 unlocked scope member 的 `imports` + `confidence=1.0` edge。自我驗證涵蓋 CRUD、多 scope
    membership、locked scope、零/多候選、reference-only 命中與拒絕 suggestion 不留 canonical 副作用。
49. **真實 repo 實驗結果（2026-09-06）**：在 RepoRune 的乾淨暫存 clone（36 個索引檔）與
    `兌換碼腳本` 的乾淨暫存 clone（7 個索引檔）執行 `rune scope suggest`，全部拒絕以確認無 canonical
    副作用。RepoRune 的 path heuristic 產出 `src`（19 檔）與 `tests`（17 檔）兩個過寬候選，不宜直接採用；
    graph 產出兩個 fixture app 的 2 檔候選，人工判定合理。第二個 repo 的 graph 產出 7 個緊耦合
    application/test 檔案的單一候選，適合當起點但仍須人類拆分。此樣本共 5 個候選，2 個可直接採用、1 個
    可作起點、2 個過寬；現階段保留「建議而非正確性需求」定位，不據此鎖死 threshold，後續以更多中型
    repo 觀察是否需要將 path heuristic 從頂層目錄收斂到更細的共同前綴。**（第九輪第 55 條更新：
    這裡的第二個樣本`兌換碼腳本`只有 7 個檔案，不構成「中型 repo」，已改用 `honeypot-discord-bot`
    重跑，這條記錄的 RepoRune 樣本本身仍然有效、繼續採用，不需要重跑。）**

**下一個 session 對已完成的 Milestone 4 做自我複查發現並修正 1 個問題**（實際跑 CLI 重現後才修，
不是憑讀程式碼猜測）：

50. **中：`rune scope suggest` 在 cache 不存在時未處理即崩潰，且會留下副作用**：`scope_suggest` 直接
    `sqlite3.connect(str(layout.memory_db))` 沒有先檢查檔案是否存在，跟同一支檔案裡 `status` 指令
    已經在用的 `layout.memory_db.exists()` 防護寫法不一致。實測重現兩種情境：(1) `.rune/cache/`
    整個目錄不存在時，`sqlite3.connect` 直接丟 `OperationalError: unable to open database file`；
    (2) cache 目錄存在但 `memory.db` 被刪除時，`sqlite3.connect` 會**先靜默建立一個 0-byte 的空檔案
    當副作用**，接著在後續 `SELECT ... FROM symbols` 撞上 `OperationalError: no such table: symbols`
    才崩潰——兩種情況都是未攔截的 traceback，不是乾淨的 CLI 錯誤訊息。這不是純假設情境：
    `.rune/cache/` 本來就是 `.gitignore` 排除的衍生目錄，一個新 clone 這個 repo、還沒跑過
    `rune update`/`rune init` 的人，直接跑 `rune scope suggest` 就會踩到；既有的
    `test_scope_suggest_rejection_has_no_canonical_side_effect` 測試只覆蓋了 `init` 之後的路徑，沒
    覆蓋這個情境。修法：在 `scope_suggest` 開頭比照 `status` 指令，先檢查
    `layout.memory_db.exists()`，不存在就印出「先跑 `rune update`」的友善錯誤並 `typer.Exit(1)`，不
    進 SQLite 連線。新增回歸測試 `test_scope_suggest_before_cache_exists_fails_cleanly`：先跑一次
    `rune update` 建立 cache 再手動刪除 `memory.db`，驗證指令乾淨地以 exit code 1 結束、不留下任何
    新的 `memory.db` 副作用；透過暫時 `git stash` 掉修法本身確認這個測試在修法之前確實會失敗（而非
    誤測了不存在的東西），才確定它真的在測這個 bug。

### 第九輪修訂（使用者轉述的 6 條 Milestone 3／4 finding，逐條重現後修正）

本輪的 6 條 finding 都是使用者直接轉述（而非本 agent 自己複查發現），依專案既定方法論「外部
finding 先重現，不能看描述就信」逐條寫最小重現腳本驗證後才動手修——全部 6 條都重現成立，沒有一條是
誤報。

51. **高：unchanged caller 的 reference edge 不會因為 import target 後來補上符合的 symbol 而重新
    解析**：`run_update`（`core/update.py`）對 `changeset.unchanged` 的檔案，原本把上一輪的
    `calls`/`extends`/`implements` edge 原樣沿用（`edges_by_path.get(...)`），跟 `imports` edge
    用同一套「不變就重用」邏輯，理由寫在舊註解裡是「兩者都不用重新解析」。這個類比是錯的：
    `imports` edge 的正確性只取決於**這個檔案自己**的 import 陳述句（沒變），但 `calls`/
    `extends`/`implements` 的正確性同時取決於**目標檔案的 symbol table**——後者即使呼叫端檔案本身
    毫無變動，也可能在同一次 `rune update` 因為另一個檔案被修改而改變。實測重現：`app/main.py`
    呼叫一個當時不存在的 `helper()`，記錄為 unresolved；之後只在 `app/main.py` 已 import 的
    `app/helpers.py` 補上 `helper` 函式（`app/main.py` 本身完全沒動），跑 `rune update`
    （`full=False`）後這條 edge 仍然是 unresolved——必須整個 `full=True` 重建才會拿到正確 target。
    這直接違反「相同狀態輸入、相同結果輸出」的 rebuild-cache 等價性保證（第 6 條記錄過的同一條保證，
    這次是 incremental 沒有追上 full rebuild，而非 hash-seed 造成不一致）。既有的
    `test_rebuild_cache_full_rescan_matches_incremental_state` 測不出這個 bug，因為它只比對
    symbols/files/edges **數量**，不比對 edge 實際指向的 target 是否一致。
    修法：`changeset.unchanged` 的檔案改成只沿用上一輪的 `imports` edge（`edge_type ==
    EdgeType.imports` 過濾），`calls`/`extends`/`implements` 一律靠新增的 `_extract_references_only`
    輔助函式重新跑一次 `adapter.extract_references`（只重新抽取 reference，不重新抽取
    symbols/imports，因為那兩者確實只取決於檔案自身內容，可以放心沿用）取得新的 raw reference，
    納入既有的 `pending_references` 統一解析流程。代價是每次 `rune update`（即使是完全無修改的
    no-op）現在都要對每個檔案重新跑一次 tree-sitter 的 reference walker，不再是純粹的 O(變更檔案數)；
    這是刻意接受的取捨——純本地解析、零 LLM/網路成本，跟既有「`full=True` 重新掃描全部檔案也可接受」
    的判斷基準一致，正確性優先於這裡的增量省下的解析成本。新增整合回歸測試
    `test_unchanged_callers_reference_edge_is_re_resolved_after_target_gains_the_symbol`
    （`tests/integration/test_update_flow.py`），並確認它在修法前確實會失敗。
52. **中：Python/TypeScript 的 qualified／generic 繼承 reference 被整個丟棄，不是記錄為
    unresolved**：`treesitter.py` 的 `_collect_references` 原本判斷 base class／heritage 節點時只
    接受裸 `identifier`/`type_identifier`，遇到 `pkg.Base`（Python `attribute`）、`Base[T]`
    （Python `subscript`）、`ns.Base`（TS `member_expression`）、`ns.Shape`（TS
    `nested_type_identifier`，這是跟 `member_expression`不同的文法節點）時，整個迴圈跳過，
    連一筆 unresolved 的 `RawReference` 都不產生——直接違反 ARCHITECTURE.md §4.3「無法解析的
    reference 仍要記錄，不能丟棄」的規則（這條規則原本是為了 unresolved import 訂的，但同一原則
    對 reference 同樣適用，Milestone 3 的既有測試已經在驗證這件事，只是沒蓋到這幾種節點形狀）。
    實測重現：`class Foo(pkg.Base)`／`class Bar(Base[T])`／TS 的 `class Foo extends ns.Base`
    在修法前都完全不產生 `extends` edge（`SELECT ... WHERE edge_type='extends'` 回傳空）。修法：
    新增遞迴的 best-effort 名稱擷取（Python 的 `_base_class_name`、TS 的
    `_heritage_type_name`），沿用既有「取最右側/最內層識別字」的慣例（跟呼叫端的
    `self.db.fetch()` 取 `fetch` 同一套邏輯）：`pkg.Base`/`ns.Base` 取 `Base`／`Shape`，
    `Base[T]`／`Comparable<Foo>`（TS `generic_type`）遞迴取被參數化的名稱本身。修完後這些案例都會
    產生一筆 `RawReference`，交給既有的 `resolve_references` 走正常的 best-effort 比對流程（可能
    resolve 成功，也可能維持 unresolved，但絕不會是「完全沒有這筆紀錄」）。新增
    `test_python_qualified_and_generic_bases_still_produce_extends_refs`、
    `test_typescript_qualified_and_generic_heritage_still_produce_refs`
    （`tests/unit/test_treesitter.py`），並確認兩者在修法前都會失敗。
53. **中：`extends`/`implements` 的 target 沒有限制必須是 class/interface，可能誤配到同名的
    function/variable**：`references.py` 的 `resolve_references` 在算 `source_symbol`（呼叫/繼承
    陳述句所在的那個 symbol）時已經正確依 edge_type 分開 `_CALLABLE_KINDS`/`_TYPE_KINDS`
    （`_find_enclosing_symbol`），但算 `target_symbol`（呼叫/繼承陳述句**指向**的那個 symbol）時完全
    沒有套用同樣的 kind 限制，`local_matches`/`candidates` 都只比對名稱。實測重現：同一個檔案裡有
    `def Base(): ...` 和 `class Foo(Base): ...`，`extends` edge 的 target 被錯誤 resolve 成那個
    **function** 的 symbol_id，而非正確地留白（因為 `Base` 根本沒有真正的類別定義）。修法：對
    `extends`/`implements` 的 target 比對加上 `s.kind in _TYPE_KINDS` 過濾（`calls` 不受影響，
    因為呼叫目標本來就可以是 function/method，甚至透過建構子呼叫 class 本身，沒有這個限制）。新增
    `test_resolve_references_extends_target_ignores_same_named_function`、
    `test_resolve_references_implements_target_ignores_same_named_variable_in_imported_file`
    （`tests/unit/test_references.py`）——特別記錄：既有的
    `test_resolve_references_extends_does_not_match_function_symbols` 這個名字聽起來像測了同一件事，
    但實際讀過之後發現它測的是 `source_symbol` 的 kind 限制（`_find_enclosing_symbol`），從未在
    `symbols_by_path` 放入一個同名 function 讓 target 比對有機會選錯，所以完全沒蓋到這條 finding，
    這正是「連自己寫的回歸測試都要驗證它是不是真的在測它宣稱要測的東西」這條方法論要抓的情況。
54. **高：Scope 自動併入（Milestone 4）在 `rebuild_cache` 之前就把新 membership 寫進 canonical
    `scopes.json`**：`run_update` 原本的順序是「算出新 membership → `save_scopes`（寫入磁碟）→
    呼叫 `rebuild_cache`」。若 `rebuild_cache` 中途失敗（canonical 衝突、SQLite 錯誤、磁碟錯誤），
    整個 `run_update` 會拋例外，但 `scopes.json` 已經帶著新 membership 落地，SQLite 卻完全沒有反映
    這次的變更——直接違反 ARCHITECTURE §4.9「決定性索引更新要嘛整體成功、要嘛整次 update 乾淨
    中止，不留部分決定性狀態」的交易要求（scope 自動併入是這次 `rune update` 呼叫裡的一部分，理應
    適用同一個 all-or-nothing 契約）。這跟 `project.json` 那個「已知限制」不是同一類問題：
    `project.json` 只是可自我修復的 freshness metadata（下次 update 一定會重新算，不依賴它），但
    `scopes.json` 是**權威 canonical 內容**，不是衍生資料，一旦提前落地又沒被對應的 cache 反映，
    不會自己修復。實測重現：monkeypatch `rebuild_cache` 讓它拋例外，`run_update` 如預期往外拋，但
    `scopes.json` 讀回來已經多了新檔案的 membership。修法：`materialize.rebuild_cache` 新增選填參數
    `scopes_override`，有給值時直接拿它 materialize（不重新從磁碟讀 `scopes.json`）；`run_update`
    改成把算好但**尚未寫入磁碟**的 `ScopesFile` 透過 `scopes_override` 傳給同一次 `rebuild_cache`
    呼叫（讓這次的自動併入結果跟其餘決定性索引一樣，在同一個 SQLite transaction 裡一起 commit），
    直到 `rebuild_cache` 成功回傳後才呼叫 `save_scopes` 把它寫進 canonical——如果 `rebuild_cache`
    失敗，`save_scopes` 根本不會被呼叫，`scopes.json` 維持失敗前的原樣。這個順序（先讓 cache 反映、
    成功後才寫 canonical）刻意跟 `project.json` 的順序理由一致（都是「先算好、最後才落地，縮小
    視窗」），但這裡多了 `scopes_override` 這一步，確保 canonical 落地之前 cache 已經跟它一致，而
    不是像 `project.json` 那樣接受一個短暫不一致的視窗——因為 `scopes.json` 不像 `project.json`
    那樣是純粹的展示用 metadata，容不下同一等級的風險。新增回歸測試
    `test_failed_rebuild_cache_does_not_leave_partial_scope_auto_assignment`
    （`tests/integration/test_update_flow.py`），並確認它在修法前會失敗（`scopes.json` 讀回來的
    membership 跟修法前的舊值不同）。
55. **低，驗收資料補齊：真實 repo 品質實驗第二個樣本改用真正的中型 repo**：Milestone 4 完成時記錄的
    第二個真實 repo 樣本（`兌換碼腳本`）只有 7 個索引檔，稱不上「中型 repo」，不滿足 IMPLEMENTATION_
    PLAN.md 自己訂的「至少 2 個真實中型 repo」驗收標準。本輪改用 IMPLEMENTATION_PLAN.md 這條驗收
    標準本身點名的範例 repo `honeypot-discord-bot`（186 個索引檔、1662 個 symbol、7422 條 edge，
    明確是中型規模）重跑一次實驗（本機 clone 到暫存目錄執行，不寫回原始 repo）。結果：路徑啟發式
    產出 7 個候選（`admin` 2 檔、`core` 13 檔、`discord_plugin_platform` 84 檔、`features` 50 檔、
    `hubs` 7 檔、`scripts` 3 檔、`tests` 25 檔），其中 `admin`/`core`/`hubs`/`scripts` 大小合理、
    像人會畫的架構邊界，`discord_plugin_platform`/`features`/`tests` 過寬（`features` 底下其實是
    十幾個彼此獨立的 feature cog，不該被合併成單一 scope，跟 RepoRune 自己實驗時觀察到的「頂層目錄
    heuristic 偏寬」問題一致，不是這個 repo 特有的）。Graph clustering 產出的結果更明確暴露既有的
    已知限制：一個由 `bot.py`/`core/` 當中樞、透過 import 把幾乎所有 feature/test 檔案串在一起的
    102 檔巨型 connected component，加上 3 個小型（2-3 檔）真正像獨立單元的候選——單一巨大 component
    完全不可用，這正是 ARCHITECTURE §4.4／IMPLEMENTATION_PLAN 已經記錄的「connected components 對
    透過中樞模組互相耦合的大型 repo效果不佳」風險在真實資料上的具體體現，不是本輪新發現的 bug，而是
    確認了「clustering 品質定位為實驗、不鎖死 threshold」這個決定本身是對的。連同 Milestone 4 原本
    記錄的 RepoRune 自身實驗，現在共 2 個真正的中型 repo 樣本，滿足驗收標準的樣本數量要求。
56. **測試缺口補齊：locked scope 的 incremental 自動併入補上端對端測試**：原本只有
    `tests/unit/test_scopes.py` 對 `assign_new_files_from_imports` 這個 core 函式的直接單元測試，
    沒有任何測試透過完整的 `run_update`（`full=False`）驗證 locked scope 在真實 SQLite materialize
    流程裡確實不會被自動寫入——單元測試測不出 `core/update.py` 自己接線接錯的情況（例如
    `scopes_override` 傳遞邏輯或呼叫順序寫錯）。新增
    `test_new_file_with_only_locked_import_scope_is_not_auto_assigned_end_to_end`
    （`tests/integration/test_update_flow.py`），對一個 `locked=True` 的 scope 跑一次完整
    `rune update`，斷言 `stats["scope_files_auto_assigned"] == 0`、SQLite `scope_files` 表裡新檔案
    沒有任何 membership、原本的 membership 也完全不變。

### 第十輪修訂（Milestone 5 開工前，修正 ScopeSummary 生成失敗語意的自相矛盾）

開工前重新檢視 Milestone 5 的既有文字時發現：「產生失敗時不附加新的 JSONL 行，只在 SQLite 投影中
維持 `status=stale`」這句話本身無法同時成立——SQLite 的 `semantic_objects` 表每次都是
`rebuild_cache` 從 `semantic.jsonl` 重新讀取算出來的，不寫 JSONL 就沒有任何東西可以讓 SQLite 投影
出「失敗」這件事，下一次 `rebuild-cache`／`rune update` 一定會讓這個狀態消失。這正是「先問使用者，
再動手」的情境：失敗狀態要不要跨 session 持久化，兩種答案都說得通，但語意差很多。

57. **`ScopeSummary` 新增 `revision` 欄位，比照 Decision/Constraint/Note 既有機制**：失敗狀態確定
    要跨 session 持久化（撐過 `rebuild-cache`），做法不是另外發明一套，而是重用專案裡已經三次驗證
    過的 revision 模式——`scope_id` 底下 `revision` 單調遞增，current = `max(revision)`，與
    Decision/Constraint/Note 共用同一套「current」定義（DATA_MODEL §1、§3）。失敗時附加新 revision：
    已有成功產生過的 scope，複製上一筆 current revision 的完整內容（比照 §2.5「系統自動附加 revision
    必須是完整 snapshot」的既有規則）、只改動 `status=stale`／`last_error`／`generated_at`／
    `source_hash`/`source_files`；從未成功產生過的 scope，附加 `revision=1`、新增的
    `status=unavailable`，內容欄位留空、不虛構（不能因為 LLM 沒回應就編一個假的 `purpose`）。實作於
    Milestone 1（model 定義補上 `revision`/`unavailable`）與 Milestone 5（worker 實際產生這些
    revision）。
58. **`last_error` 收斂為清洗過的分類字串，原始錯誤另存本機 log**：`semantic.jsonl` 是可能進 git 的
    canonical 檔案，敏感度假設等同任何原始碼——不能把 provider 的原始回應、例外訊息或 traceback 直接
    寫進去（可能夾帶 prompt injection 殘留、模型 hallucinate 片段，或未被既有 redaction pass 攔到的
    殘留內容）。`last_error` 只允許簡短分類值（`schema_validation_failed`／
    `provider_error:<ExceptionType>`／`repair_retry_failed`／`fallback_failed`），完整原始錯誤寫進
    新增的 `.rune/logs/semantic.log`——這個檔案不是 canonical、不進 git（`rune init` 時加進
    `.gitignore`，比照 `.rune/cache/`），純粹是人類本機除錯用的操作記錄，可隨時刪除，不影響任何
    rebuild 或 retrieval 邏輯。實作於 Milestone 5。

### 第十一輪實作記錄（Milestone 5）

59. **實作範圍**：`provider.py`（`ModelProvider` protocol、`OpenAICompatibleProvider` 通用實作、
    `OpenRouterProvider`/`OpenAIProvider`、`build_provider` 從環境變數解析 API key——`.rune/`
    底下任何檔案都不會被拿來讀 API key，`token.env` 這類本機開發用的檔案純粹是外部 shell/dotenv
    的慣例，不是 rune 自己的程式碼會去讀的東西）、`redaction.py`（沿用共用的 secret pattern，只套用
    在 free-text 欄位，`entry_points`/`important_symbols`/`dependencies` 這類結構化參照欄位完全不碰，
    避免把真的 symbol_id/file path 誤傷）、`validation.py`（schema 核心欄位失敗即拒絕、清單條目引用
    不存在的 file/symbol 則 strip，`dependencies` 因為可能是外部套件名稱/scope_id、無法驗證，維持
    best-effort 不做 strip）、`worker.py`（`compute_source_files` 的 union invariant、
    `needs_refresh` 的 staleness 判斷、`refresh_scope_summary` 的 fallback ladder、
    `run_semantic_refresh` 的逐 scope 迴圈 + `max_input_tokens_per_run` 預算裁切）。`rune update`
    的接線比照 Milestone 4 的 `scopes_override` 模式：算好的 `ScopeSummary` 透過新增的
    `rebuild_cache(semantic_override=...)` 參數跟其餘決定性索引一起進同一個 transaction，
    `semantic.jsonl` 的 append 與 `.rune/logs/semantic.log` 的寫入延後到 `rebuild_cache` 成功之後才
    執行——回歸測試 `test_semantic_refresh_failure_does_not_leave_partial_canonical_state` 用
    `git stash`／monkeypatch 手法確認過這個順序被破壞時測試真的會抓到。
60. **真實 API 驗證（2026-09-06，`qwen/qwen3.8-flash` via OpenRouter，使用者提供的個人 API
    key）**：不只用 mock provider 測，額外做了端到端的真實呼叫，發現一個純讀文件推導不出來的行為
    ——這個模型會先在 `reasoning` 欄位「思考」，`max_tokens` 太小時整個預算被思考過程吃光，
    `content` 回傳 `None`；`provider.py` 因此把「`content is None`」明確視為一種獨立的
    `ProviderError`（而非放給下游 JSON parse 失敗去籠統歸類），訊息裡直接提示要調高
    `max_tokens`。真實呼叫過程中還真的踩到一次 OpenRouter 上游對這個免費/共用池模型的 429
    rate-limit，`refresh_scope_summary` 的 fallback ladder 正確處理：第一次嘗試 429 失敗、
    repair-retry 再打一次 primary 就成功，`metrics.provider_error=True` 與
    `metrics.schema_success=True` 同時為真，忠實反映「這次呼叫其實中途失敗過一次，最後才成功」。
    另一次成功呼叫中，模型自己 hallucinate 了 3 個不存在的 symbol/file 到 `entry_points`，
    validation 的 strip 規則正確地把這 3 個踢掉、只留下真正存在的 2 個，其餘欄位正常保留——不是
    mock 出來的行為，是真的模型輸出被真的 strip 邏輯處理過。
61. **已知未完成／刻意延後的項目（誠實記錄，不是遺漏）**：
    - `possibly_stale` 這個 `SemanticStatus` 值目前完全沒有任何程式碼路徑會設定它——DATA_MODEL.md
      沒有具體定義它該由什麼觸發（不像 `fresh`/`stale`/`unavailable` 三者都有明確規則），保留在
      enum 裡供未來（例如「間接依賴的 scope 變了，但這個 scope 自己的 member 沒變」這種較弱的
      staleness 訊號）使用，V1 不強行發明一個用途。
    - `needs_refresh` 對「`status=stale`／`unavailable` 但 source_hash 沒變」的情境選擇每次
      `rune update` 都重新嘗試，而不是做指數退避或次數上限——如果一個 scope 持續失敗（例如
      provider 本身有問題），目前的行為是每次 `rune update` 都會再花一次 fallback ladder 的成本
      重試。這是刻意的簡化（V1 情境是小規模、少量 scope，成本可接受），但如果之後有真實中大型 repo
      report「一直重試某個壞掉的 scope 浪費錢」，退避機制會是下一步要補的東西，目前只在
      `needs_refresh` 的 docstring 裡記錄了這個取捨，沒有另外開 issue 追蹤。
    - 驗收標準裡「schema_success_rate 等六項 metrics 在正常與失敗案例下都被正確記錄」只在
      `aggregate_metrics`／`refresh_scope_summary` 的單元測試層級驗證過，沒有另外寫一個端對端測試
      斷言 `rune update` 的 CLI 輸出裡真的印得出這六個數字——`run_update` 的回傳 stats dict 已經把
      `semantic_<metric>` 展開進去，CLI 既有的 `"Indexed: " + ", ".join(...)"` 輸出格式會自動印出
      這些新 key，但這條銜接沒有專門測試鎖住。
    - `rune bootstrap`/`rune search` 等會真正「顯示」semantic summary（含 `unavailable`/`stale`
      的可見性規則）的 retrieval 邏輯是 Milestone 6 的範圍，本輪只確保寫入端（worker + materialize）
      正確，沒有涉及讀取端。
62. **補上第 61 條沒做完的兩項設定（同輪追加，2026-09-06）**：使用者問「思考強度、模型這些設定放在
    哪」時發現 `max_tokens`（單次呼叫的輸出上限）先前是寫死在 `worker.py` 函式參數預設值
    （`4000`），沒有進 `config.toml`；且完全沒有任何管道能控制 reasoning-capable 模型（例如
    `qwen/qwen3.8-flash`）的思考量，即使上一輪已經在真實 API 上親眼看過這個模型把 `max_tokens`
    燒在 `reasoning` 欄位上。已補：`SemanticConfig` 新增 `max_tokens: int = 4000`；新增
    `ReasoningConfig`（`enabled`/`effort`/`max_tokens` 三個獨立欄位，皆選填，`enabled=False` 優先
    於其他兩者）掛在 `SemanticConfig.reasoning` 底下。`provider.py` 新增 `reasoning_payload()` 把
    這個設定翻譯成 OpenRouter 的 `reasoning` request 欄位——**先用真實 API 驗證這個 request 欄位
    真的有效才動手接線**：`{"effort":"low"}` 讓 `qwen/qwen3.8-flash` 的 `reasoning_tokens` 從
    36 降到 25，`{"enabled":false}` 直接降到 0，`{"max_tokens":10}` 精確卡在 10——三種都用真實
    OpenRouter API 呼叫驗證過，不是憑 API 文件猜的。`build_provider()`／`OpenRouterProvider`／
    `OpenAIProvider` 都新增 `reasoning: dict | None` 建構參數；`update.py` 的
    `_build_semantic_providers` 把 `config.semantic.reasoning` 一併傳給 primary 與 fallback
    provider（兩者共用同一個 reasoning 設定，沒有分開設計，因為這是「整體要花多少在思考」的政策，
    不是特定模型才有的細節）。最後用 `reasoning.enabled=False` 對真實 API 端到端驗證一次：模型
    立刻回應、`output_tokens=1`、完全沒有 reasoning 開銷。新增 6 個單元測試
    （`test_provider_sends_configured_reasoning_payload` 等，`tests/unit/test_semantic.py`）與
    2 個 config 測試（`tests/unit/test_config.py`，含「未知欄位拒絕」延伸到
    `[semantic.reasoning]` 這個新的巢狀區塊）。159 個測試全綠。
63. **`max_tokens` 預設值調整為 16000（使用者要求，2026-09-06）**：原本 `4000` 是本輪一開始隨手訂的
    暫定值，使用者實際使用後要求調高，改為 `16000`——理由與第 62 條記錄的「reasoning 模型會佔用同一個
    預算」一致：預設值越低，思考佔用完 `max_tokens` 導致 `content=None`（`ProviderError`）的機率越高，
    16000 給思考留更多餘裕。同步更新 `SemanticConfig.max_tokens`、`worker.py` 兩處函式參數預設值
    （`refresh_scope_summary`/`run_semantic_refresh`，僅供未經 `config.toml` 呼叫時的保底值，
    `rune update` 一律走 config 值）、對應的 config 測試斷言。

### 第十二輪修訂（使用者要求對 Milestone 5 做品質複查，發現並修正 3 個問題）

改完第 63 條後，使用者要求針對 Milestone 5 的實作品質做一次複查。延續本專案「先寫最小重現腳本，
確認是真的 bug 才動手」的方法論，逐一驗證，3 條都確認為真：其中 2 條（64、65）是本次複查才發現，
不是外部轉述；1 條（66）是本次複查時才注意到、但實際上是 Milestone 5 一開始就存在的既有 bug（第 60
條記錄的真實 API 呼叫當時已經觸發過這個路徑，只是沒人注意到 prompt 內容本身不對勁）。

64. **中：`reference_strip_rate` 指標在模型回傳非 list 的 `entry_points`/`important_symbols` 時會被
    嚴重灌水**：`worker.py` 算 `reference_total` 時直接對模型回傳的原始 JSON 值呼叫
    `len(parsed.get("entry_points", []) or [])`，但 `validation.py` 的 `_as_str_list` 已經對「不是
    list」的值做了防禦（視為 0 筆），兩處防禦不一致。實測重現：讓模型回傳
    `{"entry_points": "not-a-list-just-a-string"}`（一個字串而非陣列），驗證後 `entry_points` 正確
    是空陣列，但 `reference_total` 卻是 24——因為 Python 的 `len()` 對字串會算「字元數」而非「陣列
    元素數」。這只影響 metrics（供未來比較 provider/model 表現用），不影響實際寫入 canonical 的
    `ScopeSummary` 內容正確性，但仍會讓 `reference_strip_rate` 這個原本設計用來判斷「模型 hallucinate
    比例」的數字失真。修法：新增 `_reference_list_len()` 輔助函式，套用跟 `_as_str_list` 相同的
    `isinstance(values, list)` 防禦。新增回歸測試
    `test_refresh_reference_total_metric_ignores_malformed_non_list_field`，修法前確認會失敗
    （`reference_total == 24`）。
65. **中：provider 端的真正失敗（HTTP 429／網路逾時）被誤用「repair prompt」重試，而非原樣重送**：
    `refresh_scope_summary` 的 retry 迴圈把「`_attempt` 回傳 `parsed is None`」這一種情況無條件當作
    「有一個失敗的回應，需要一個 repair prompt 告訴模型哪裡錯了」，但 `parsed is None` 其實涵蓋
    兩種完全不同的情境：(1) provider 層級的真正失敗（連線都沒建立起來，`_attempt` 捕捉到
    `ProviderError`）——這種情況根本沒有「上一次的回應」可以修正；(2) 回應確實到了，但不是合法
    JSON——這種才是 repair prompt 真正該處理的情境。修法前的行為會把完整的 provider 錯誤訊息（例如
    `"provider_error:ProviderError: HTTP 429: {原始上游錯誤 JSON}"`）整段塞進下一次呼叫的 user
    prompt，變成一句對模型而言完全不知所云的「你上一次的回應驗證失敗，原因是：HTTP 429 rate
    limited」——**這不是假設情境，是第 60 條記錄的真實 API 驗證裡實際發生過的路徑**（那次是 repair
    retry 恰好還是成功了，模型夠聰明沒被那句沒頭沒尾的話搞混，但這是僥倖，不是設計上的保證）。修法：
    區分 `parsed is None` 底下的兩種原因（用 `reason.startswith("provider_error:")` 判斷），
    provider 層級失敗時原樣重送 `user_prompt`（不加任何 repair 文字），只有「回應到了但不合法」才用
    repair prompt。新增回歸測試
    `test_refresh_after_provider_error_retries_with_unmodified_prompt_not_repair_prompt`，修法前
    確認會失敗（斷言兩次 prompt 相同，修法前第二次 prompt 多了 repair 文字）。
66. **低，Milestone 5 一開始就有的既有 bug（本次複查才發現）：`response was not valid JSON`（合法
    回應但非 JSON）被誤記為 `metrics.provider_error=True`**：跟第 65 條同一個「`parsed is None`
    涵蓋兩種情境」的根因，但這條影響的是 metrics 而非 prompt 內容——`provider_error_rate` 這個
    指標的設計用意是「provider 本身不可靠的比例」，如果連「模型好好回應了、只是寫的不是 JSON」都算
    進去，這個指標就沒辦法真的用來判斷該不該換 provider。修法（跟第 65 條同一次改動）：只有真正的
    `ProviderError` 才設定 `metrics.provider_error=True`。新增回歸測試
    `test_refresh_invalid_json_response_does_not_count_as_provider_error_metric`，修法前確認會
    失敗（`provider_error` 錯誤地變成 `True`）。修復第 65 條時，第一版修法把「`parsed is None`」
    整個當成 provider error（沒有進一步區分原因），意外讓既有的
    `test_refresh_succeeds_on_repair_retry` 測試失敗——這正是「連自己剛寫的修法都要重新驗證，不能
    假設一次改對」的一個實例，發現後才補上更精確的區分邏輯。
    162 個測試全綠，`ruff check` 全綠。

### 第十三輪修訂（使用者轉述的 10 條 Milestone 5 finding，逐條重現後修正 9 個問題、記錄 1 個待討論）

使用者轉述另一份針對 Milestone 5 的 code review，10 條 finding 全部先寫最小重現腳本驗證，全部確認
為真（其中 #1、#3、#6 的一半、#7、#8 直接寫腳本重現，#2、#4、#5、#9、#10 讀程式碼即可確認邏輯缺口，
不需要另外重現）。9 條屬於純粹的 bug／既有設計缺口，直接修；1 條（#4，provider 不可用時的
`possibly_stale` 標記）使用者明確要求先記錄、不在本輪修，留待後續討論設計。

67. **高：`rune rebuild-cache` 違反自己宣稱的「zero LLM calls」**：`core.update.run_update` 的
    semantic refresh 區塊完全沒有檢查 `full` 旗標，只要 `config.semantic` 設定了 provider，連
    `rebuild-cache`（`full=True`）都會真的呼叫 LLM——實測用 FakeProvider 追蹤呼叫次數，重現
    `rebuild-cache` 呼叫了 provider 一次。修法：semantic refresh 比照既有的 scope 自動併入
    （`if not full and ...`），整段包進 `if not full:` 才執行；orphan 偵測（見下）是純結構檢查、
    無 LLM 呼叫，不受這個限制，`full=True` 也照跑。新增 4 個既有整合測試從 `full=True` 改成
    `full=False`（因為它們原本就是靠 `full=True` 意外觸發 semantic refresh 來測，修法後這個路徑
    被關掉了，測試本身需要跟著改，否則會變成沒在測任何東西的假陽性）；新增
    `test_rebuild_cache_never_calls_the_semantic_provider` 專門鎖住這個行為，修法前確認會失敗
    （`provider.call_count == 1`）。
68. **高：刪除已有 semantic summary 的 scope 會讓 materialize 因 FK violation crash**：
    `semantic_objects.scope_id` 對 `scopes(id)` 有真正的 FK，`_materialize_semantic` 先前沒有像
    `_materialize_scopes` 對 dangling file/symbol membership那樣做前置過濾——實測重現：對一個曾經
    成功產生過 summary 的 scope，刪除該 scope 後再跑一次 `rune update`，直接丟
    `sqlite3.IntegrityError: FOREIGN KEY constraint failed`，且會**持續**發生在往後每一次
    `rune update`/`rebuild-cache`，等於這個 repo 從此無法再更新，除非手動編輯 `semantic.jsonl`。
    這是本輪影響最大的一條。修法：`SemanticStatus` 新增 `orphaned`（完全比照 Decision/Constraint
    既有的 orphaned 語意，DATA_MODEL §6），`core.update` 新增 `detect_orphaned_scopes`：偵測
    「semantic.jsonl 現有 current revision 的 scope_id 不在目前 scopes.json 裡」，附加一筆內容
    複製、只改 status/generated_at 的新 revision（跟系統自動附加 revision 的既有「完整 snapshot」
    規則一致）；`_materialize_semantic` 在插入前查詢目前 `scopes` 表存在的 id，過濾掉任何已消失的
    scope_id（不只是 orphaned 狀態的，防禦性地涵蓋任何理論上不該出現的情況）。這個偵測是純結構檢查、
    無 LLM 呼叫，因此不受 #67 的 `full` 限制，`rebuild-cache` 也會正確 orphan 消失的 scope。新增
    `test_deleted_scope_orphans_its_semantic_summary_instead_of_crashing`，修法前確認會失敗
    （同樣的 `IntegrityError`）。
69. **高：多個 scope 在同一次 run 刷新時，canonical 的 append 不是原子操作**：`core.update` 原本是
    `for summary in new_semantic_revisions: append_jsonl(...)` 逐一呼叫，實測重現：兩個 scope 同時
    刷新，模擬第二個 `append_jsonl` 失敗，結果 SQLite（已經在同一個 transaction 內反映了兩個 scope
    的新內容）回報兩個 scope 都是 current，但 canonical `semantic.jsonl` 只有第一個——這是「部分
    canonical 已發佈」的不一致狀態，比單純「cache 領先 canonical」更嚴重。修法：新增
    `canonical.append_jsonl_many`，把整次 run 所有新 revision 合併成一次 atomic write（讀現有內容
    + 疊加全部新行 + 一次 rewrite），取代逐筆呼叫。新增
    `test_multiple_semantic_revisions_append_atomically_in_one_run`，確認失敗時 canonical 完全
    沒有任何新內容（不是「第一個而已」），同時明確記錄「SQLite 已經在 append 之前提交」這個接受的
    既有限制不變（跟 `project.json`/`scopes.json` 的既有 known limitation 同一類別，不是這條
    finding 要解決的問題）。
70. **高，記錄但本輪不修：provider 不可用時，已變 stale 的 scope 仍顯示 fresh**：沒有 API
    key／`semantic.enabled=false`／預算用完時，semantic refresh 整段被跳過，`needs_refresh` 判定
    為「需要刷新」的 scope 不會有任何狀態轉換，繼續顯示上一次的（可能早已過期的）`status`。
    `possibly_stale` 這個 enum 值從 Milestone 5 一開始就沒有任何觸發邏輯，第 61 條已經記錄過這個
    缺口。使用者明確表示「這個先不在本次任務修理，記下來我們等下討論」——需要決定的是：要不要在
    provider 不可用時也附加一筆不呼叫 LLM 的 `possibly_stale` revision（內容複製、只改 status），
    以及這筆 revision 要不要跟 #68 的 orphan 偵測一樣不受 `full` 限制。留待下次討論，不自己選方案
    動手。
71. **高：Provider request 完全沒有 structured output/JSON mode，只靠 prompt 文字要求**：確認
    OpenRouter 對 `qwen/qwen3.8-flash` 的真實 API 支援 `response_format: {"type": "json_object"}`
    （先實測驗證過才接線，不是照 API 文件猜）。已在 `OpenAICompatibleProvider.complete()` 加上這個
    欄位，`worker.py` 既有的容錯 JSON 擷取（處理 markdown code fence／前後綴文字）原樣保留當安全網——
    不支援這個欄位的 provider 會直接忽略，不會報錯，行為退化回今天的樣子而非變差。
72. **中：Redaction 沒有尊重 `config.security.redact_secrets`，且遺漏 `dependencies` 欄位**：
    `redact_secrets` 自 Milestone 1 存在，從未被讀取，redaction 永遠強制執行——已修正
    `redact_raw_scope_summary` 新增 `enabled` 參數，`worker.py` 接上 `config.security.
    redact_secrets`。另外，`entry_points`/`important_symbols` 因為驗證時會 strip 掉不符合已知
    file/symbol 的條目，秘密字串不可能巧合符合真實路徑，等於間接被保護；但 `dependencies`
    （可能是 scope_id 或外部套件名稱，沒有已知集合可比對）完全沒有這層保護，也沒有被排進 redaction
    的 free-text 欄位清單——已補上。兩者都用最小重現腳本驗證過（redaction 開關真的能關掉/開啟，
    `dependencies` 裡的秘密字串真的會被替換成 `[REDACTED]`）。
73. **中：Schema 驗證把不合法型別默默轉換，違反「核心欄位不合法即拒絕」規則**：`purpose` 給一個
    dict，原本會被 `str(...)` 轉成字面文字（例如 `"{'nested': 'dict'}"`）接受為有效的 `fresh`
    summary——實測重現。已改為：`purpose` 不是字串就直接拒絕整份 generation，不再嘗試轉型。清單
    欄位（`entry_points` 等）維持原本的寬鬆行為不變（非 list 視為空清單，不拒絕）——這是刻意的：
    規格只把「核心欄位」的 schema 失敗訂為拒絕整份 generation 的條件，清單型欄位的形狀問題屬於
    「best-effort、不影響其餘欄位」的既有寬鬆設計，只有 `purpose` 需要收緊。
74. **中：Fallback model 產生的內容被誤標為 primary model**：`refresh_scope_summary` 內部的
    `_validate` 閉包原本寫死 `model=primary_provider.model`，不論實際是哪個 provider 產生的內容——
    實測重現：primary 失敗、fallback 成功後，`outcome.summary.model` 仍是 `"primary-model"`。
    已改為 `_validate` 接受 `model` 參數，呼叫時傳入該次嘗試實際使用的 `provider.model`（全部失敗
    時的 `unavailable`/`stale` revision 仍標 `primary_provider.model`，因為那代表「嘗試過的
    主要模型」，不是「產生內容的模型」，語意上沒有問題）。
75. **中：Prompt 只有 symbol metadata，沒有實際程式碼內容**：模型幾乎沒有實作細節可以參考就要生出
    `purpose`/`data_flow`/`invariants` 等欄位。已利用既有的 `Symbol.start_line`/`end_line`（無需
    重新解析）讀取每個 member symbol 的實際程式碼片段附進 prompt，單一片段超過 200 行截斷並標記
    （避免一個異常大的 symbol 吃光整個 token 預算、餓死其他 symbol），讀檔失敗（檔案不存在/編碼
    錯誤）時該片段留空但不中止整次 refresh（沿用既有的 failure-isolation 原則）。`refresh_scope_
    summary`/`run_semantic_refresh` 新增必填的 `repo_root` 參數。
76. **中：六項 run-level metrics 沒有任何持久化**：先前只存在 `rune update` 回傳的 stats dict，
    無法跨 run/provider 比較。新增 SQLite `semantic_run_metrics` 表（DATA_MODEL §5），純操作性
    歷史資料，**不在**每次 materialize「清空重建」的六張根表之列（沒有 canonical 背書可以重建它，
    刻意不當成可衍生資料處理），只在這次 run 真的嘗試過 refresh（`scopes_attempted > 0`）時
    append 一行，跟其餘決定性索引一樣在同一個 rebuild_cache transaction 內寫入。整合測試驗證：
    一次成功 refresh 後有 1 行；接著一次「沒有東西要刷新」的 no-op update 不會多出空白行；
    `rebuild-cache`（不呼叫 LLM）也不會動這張表，但表本身在 `rebuild_cache` 的清空重建流程裡
    正確存活下來。

新增 12 個回歸測試（4 個既有測試從 `full=True` 改為 `full=False`、8 個全新測試，橫跨
`tests/integration/test_update_flow.py` 與 `tests/unit/test_semantic.py`），每個都用 `git stash`
（或直接對照修法前後行為）驗證過修法前確實會失敗。173 個測試全綠，`ruff check` 全綠。

### 第十四輪修訂（使用者轉述第二份 Milestone 5 code review，5 條 finding 全部修正）

同一批 Milestone 5 又收到一份新的 code review，5 條全部先重現再修，全部確認為真，沒有誤報。

77. **中：SQLite cache 沒有 schema migration，舊版 memory.db 會讓 materialize 持續 crash**：
    `schema.sql` 全用 `CREATE TABLE IF NOT EXISTS`，任何既有表格新增欄位（例如第 59-76 條剛加的
    `semantic_objects.current_revision`）都不會套用到已存在的舊 memory.db 上。實測重現：手工建一個
    缺 `current_revision` 欄位、`schema_meta.schema_version='1'` 的舊 shape memory.db，配上真的
    canonical 內容（一個 scope + 一筆 semantic summary），跑 `rune update`/`rebuild-cache` 直接丟出
    未攔截的 `OperationalError: no such column: current_revision`，且會**持續**發生在往後每一次
    呼叫，先前唯一解法是手動刪除 `.rune/cache/`。修法：`materialize.py` 新增 `CACHE_SCHEMA_VERSION`
    常數（跟 `schema_versions.CURRENT_SCHEMA_VERSION` 是兩個獨立概念，後者管 canonical 紀錄的
    `schema_version` 欄位，前者管 SQLite 衍生 cache 的表格形狀）；`rebuild_cache` 一開始比對
    `schema_meta.schema_version` 是否等於這個常數，不符（含連 `schema_meta` 表都沒有的任何舊檔案）
    就關閉連線、砍掉 `memory.db`（含 `-wal`/`-shm` 側車檔）重開——這正是 `rune rebuild-cache` 本來就
    承諾「隨時可安全丟棄重建」的同一套動作，自動觸發、不需要人類自己知道要去刪檔案。新增
    `test_rebuild_cache_self_heals_an_old_shape_memory_db`（`tests/unit/test_materialize.py`），
    修法前確認會失敗（同樣的 `OperationalError`）。
78. **中：Scope 被刪除後重建同名 scope，永遠卡在 orphaned 出不來**：`orphaned` revision 完整複製
    前一筆的 `source_hash`；scope 被刪除又用完全相同的 member 檔案重建時，`needs_refresh` 拿新算
    出來的 hash 跟 orphaned revision 裡的舊 hash 比對，兩者相等就回傳 `False`——即使這時候 provider
    完全可用，也永遠不會再嘗試刷新，`current` 停在 `orphaned`（依 Decision/Constraint 既有的可見性
    規則，`orphaned` 預設不可見）。實測重現：`needs_refresh(orphaned_revision, 相同的
    source_hash)` 確實回傳 `False`。修法：`needs_refresh` 的「無條件視為需要刷新」判斷從只看
    `unavailable` 擴大為 `unavailable` 或 `orphaned`——scope 重新出現這件事本身就是觸發條件，不該
    還要等 hash 不一致。新增 `test_needs_refresh_true_when_status_orphaned_even_if_hash_matches`，
    修法前確認會失敗。
79. **低：`rune update` 的 CLI 說明文字仍寫「Zero LLM calls」**：Milestone 5 之後，`rune update`
    正是會呼叫 LLM 的指令（`rebuild-cache` 才是零 LLM），這句話已經失真，容易誤導使用者以為
    `rune update` 也不花錢/不連網路。已更新說明文字，明確區分兩個指令：`update` 在有設定
    `semantic` 時會呼叫 LLM，`rebuild-cache` 永遠不會。純文件修正，不影響行為，不需要新測試。
80. **低：`needs_refresh` 的 docstring 用詞不精確**：原本寫「repeating that bounded attempt on the
    next rune update is intentional」，容易被讀成「失敗的 scope 下次一定會重試」，但實際上失敗
    revision 會把 `source_hash` 更新為**目前**的值（DATA_MODEL §2.4 的既有 revision 表就是這樣定義
    的），只要內容沒有再變，下次比對 hash 相等就不會重試，要等內容再變才會觸發——這是符合既有規則的
    行為，不是 bug，只是文件說法不夠精確，會誤導未來的人以為系統會無條件重試。已重寫 docstring，
    明確說明「重試由 source_hash 比對驅動，不是狀態本身」，並點出 `orphaned`（見第 78 條）是唯一
    「重新出現本身就是觸發條件、不看 hash」的例外。純文件修正。
81. **低：`compute_source_files` 對已刪除/無法解析的 member 靜默略過，可能讓非空 scope 產出空
    `source_files`**：DATA_MODEL §2.4 原本的措辭「`source_files={}` 只有在 scope 完全沒有 member
    時才合法」，字面上跟「members 非空、但每個 member 現在都指向不存在的檔案」這種情況矛盾——實測
    重現這個情況確實會發生（`compute_source_files` 對一個有 member 但檔案已刪除的 scope 回傳
    `{}`）。評估後**不改變行為**：這種情況下確實沒有真實內容可以雜湊，`{}` 如實反映現況，且仍正確
    參與 staleness 判斷（下次真的有 member 存在時會自我修復，不會卡住）；只把 DATA_MODEL §2.4 的
    措辭澄清為「沒有任何 member 貢獻出真實檔案」，不是字面上的「members 列表是空的」，並在
    `compute_source_files` 的 docstring 裡明講這個邊界案例與判斷理由。純文件修正，不需要新測試
    （行為本來就正確，只是文件講得不夠精確）。

新增 2 個回歸測試（第 77、78 條），皆用 `git stash` 驗證過修法前確實會失敗；其餘 3 條純屬文件精確化，
不涉及行為變更。175 個測試全綠，`ruff check` 全綠。

### 第十五輪修訂（確認 `possibly_stale` 觸發邏輯與 provider 健康檢查設計，尚未實作）

第 70 條記錄的「provider 不可用時 possibly_stale 沒有觸發邏輯」這個缺口，本輪跟使用者討論後定案
設計，**但故意不在本輪動手實作**——使用者要求先把決議完整寫進文件，下一個 session 開工前先讀這段，
再開始寫程式碼。這是本輪唯一的內容，沒有程式碼變更。

82. **`possibly_stale` 只在「曾經有內容、hash 對不上、這次沒 provider」時觸發，不需要另外處理
    「從沒成功過」的情況**：附加新 revision，複製前一筆 current revision 的完整內容，只改動
    `status=possibly_stale`、`source_hash`/`source_files`（更新為目前的）、`generated_at`——跟
    既有的「系統自動附加 revision 必須是完整 snapshot」規則一致。**`current=None` 或
    `status=unavailable` 的 scope 不需要為此額外附加任何 revision**：`needs_refresh` 對這兩種情況
    本來就無條件回傳 `True`，等有 provider 可用時自然會被重新嘗試生成，不必為了「這次仍然沒有內容」
    這件事另外留一筆什麼都沒變的空白 revision——這比原本考慮過的方案（連「從沒成功過」也要留痕跡）
    更簡單，也是使用者主動指出、確認採用的簡化。記錄於 DATA_MODEL.md §2.4 的失敗時 revision 語意
    表新增一列。
83. **`possibly_stale`／`stale` 的 semantic summary 在 retrieval 端絕不能把舊內容當作可信內容直接
    提供**：跟 Note 的 `[STALE]`（顯示舊內容 + 警告標記）刻意不同——理由是 semantic summary 是 LLM
    生成的長篇散文式描述，不是人工/agent 寫的簡短事實記錄，一段「看起來權威、但其實跟不上程式碼」的
    摘要比完全沒有摘要更危險（agent 可能照單全收、不會像看到「沒有資料」時那樣主動去讀原始碼確認，
    等同於增加 hallucination 風險）。因此 canonical（`semantic.jsonl`）仍然保留舊內容（稽核用途，
    且若程式碼被還原成跟舊版一致，不需要重新呼叫 LLM 就能讓舊內容重新有效），但 Milestone 6 的
    retrieval 對 `possibly_stale`/`stale` 的 scope **不得回傳舊摘要文字本身**，而是要回傳「這個
    scope 的摘要已過期，請直接讀取以下檔案確認目前實際內容：`source_files` 清單」這種明確指向真實
    原始碼、而非舊摘要文字的提示。現在先記錄下來，等 Milestone 6 做 retrieval 時直接照這個做，不
    需要重新討論這個決定本身（但 retrieval 的具體實作細節屆時仍可能需要進一步設計）。
84. **Provider 健康檢查從「靜默吞掉一切」改成三層，區分設定錯誤與執行期問題（尚未實作）**：先前
    `_build_semantic_providers` 把「使用者刻意關閉」「忘記設定」「打錯字」「暫時性網路問題」全部
    用同一套「靜默回傳 None」邏輯處理，導致一個打錯字的 model 名稱或忘記 export 的 API key 會讓
    semantic 永遠悄悄不執行、完全沒有任何提示，直到使用者自己發現——使用者認為這種基礎設定錯誤
    應該在一開始就報錯，避免問題不斷擴大（每次 `rune update` 都悄悄不做事，卻沒人知道為什麼）。
    確認設計為每次 `rune update` 開頭跑一次（不是每個 scope 各自跑）的三層檢查：
    ```text
    config.semantic.enabled != true
      → 維持現狀：使用者刻意關閉，靜默跳過，不是錯誤
    config.semantic.enabled == true：
      Step 1（純靜態檢查，不呼叫網路）：
        model 是空字串，或對應 provider 的 API key 環境變數沒設
          → 設定錯誤，不是暫時性問題：印出明確訊息告訴使用者缺什麼、怎麼補，
            這次 semantic 整段跳過，但決定性程式碼索引照常完成
      Step 2（僅 Step 1 通過才做，一次輕量連線測試呼叫）：
        回應是 rate limit（HTTP 429）
          → 提示使用者（預期內、非使用者的錯），這次跳過 semantic
            （避免後面每個 scope 都再撞一次同樣的 429，浪費呼叫）
        回應是其他失敗原因
          → retry 一次；仍失敗 → 這次跳過 semantic，但大聲失敗（明確錯誤訊息，
            代表真的有問題：key 錯誤、model 名稱 provider 端不認得等），
            決定性索引照常完成
        成功
          → 照現有方式跑每個 scope 的刷新（各自既有的 fallback ladder 不變）
    ```
    實作上需要 `provider.py` 補上能分辨「是不是 429」的機制（目前 `ProviderError` 只是一句字串，
    沒有結構化資訊可以判斷是哪種失敗，需要新增例如區分 status_code 或子類別的方式）；`update.py`/
    `worker.py` 新增這個一次性的 precheck 步驟，跟 semantic refresh 本身一樣只在 `not full` 時跑；
    CLI 需要能把這些訊息實際印給使用者看（不能只塞進回傳的 stats dict 裡，使用者當下就要看得到，
    不是要去翻資料才知道）。**這是下一個 session 開工的第一個任務**。

本輪沒有程式碼變更，175 個測試維持全綠，`ruff check` 全綠。

### 第十六輪修訂（實作第十五輪確認的三層 provider 健康檢查與 `possibly_stale` 觸發邏輯）

85. **`ProviderError` 新增 `status_code`（HTTP 狀態碼，網路層級失敗時為 `None`）與 `is_rate_limited`
    屬性**（`provider.py`）：`complete()` 的每個 `ProviderError` raise 點都補上 `status_code`；這是
    後面「是不是 429」判斷唯一需要的結構化資訊，不需要新增例外子類別。
86. **`OpenAICompatibleProvider.probe()`**：一次最小化的請求（`max_tokens=1`，強制
    `reasoning={"enabled": False}`），只確認 200/非 200，不解析 `content`——刻意不重用 `complete()`，
    因為 probe 的目的只是「金鑰跟 model 名稱有沒有用」，不是「這次能不能拿到一個完整回應」；強制關掉
    reasoning 是因為一個 thinking model 完全可能把這極小的 `max_tokens` budget 燒在 reasoning 上，
    讓 `complete()` 判成「空 content」而誤判成探測失敗。
87. **`check_semantic_health(config: SemanticConfig) -> (SemanticHealthCheck, primary, fallback)`**
    （`provider.py`，新增 `SemanticHealthStatus` enum：`ok`/`disabled`/`config_error`/`rate_limited`/
    `probe_failed`）：完整實作第十五輪確認的三層檢查。`update.py` 的 `_build_semantic_providers`
    改為委派給這個函式（回傳值從 `(primary, fallback)` 改成 `(primary, fallback, health)` 三元組），
    對應更新了 `tests/integration/test_update_flow.py` 裡 9 處 monkeypatch 這個函式的測試，改回傳
    `SemanticHealthCheck(status=ok)` 作為第三個元素。
88. **實作時發現並修正對第十五輪原始措辭的一處必要澄清**：原始設計把「model 是空字串」跟「API key
    沒設」都歸類為 Step 1 的「設定錯誤」，會讓每個從未設定過 semantic 的全新專案（`SemanticConfig`
    預設值正是 `enabled=True, model=""`）在每一次 `rune update` 都大聲失敗、exit code 1——用
    `tests/unit/test_cli.py::test_update_then_status_reports_fresh_again`（既有測試，預期 update 對
    未動過 semantic 設定的專案回傳 exit code 0）重現確認這個問題後修正：「`enabled=True` 且
    `model=""`」改歸類為等同 `disabled`（靜默跳過，不印訊息），`config_error`（連同 CLI exit code 1）
    保留給「已經設定了 model，但缺 API key 或 provider 名稱打錯」這種更貼近使用者原話「打錯字的
    model 名稱或忘記 export 的 API key」的情境。已同步更新 ARCHITECTURE.md §4.5 與 DATA_MODEL.md
    §2.4 的措辭，記錄這是實作階段發現的必要澄清，不是重新開放已確認的設計本身。
89. **`mark_possibly_stale`**（`worker.py`）：`update.py` 在 `_build_semantic_providers` 回傳
    `primary_provider is None`（即 health 不是 `ok`）時呼叫，取代原本「直接什麼都不做」的行為。對
    `scopes_for_semantic` 裡每個目前有 current summary 的 scope，若 `status != unavailable` 且
    `source_hash` 跟目前重新計算的不一致，附加一筆複製舊內容、只改
    `status=possibly_stale`/`source_hash`/`source_files`/`generated_at` 的新 revision；`current is
    None` 或 `status=unavailable` 的 scope 完全跳過（`needs_refresh` 已無條件涵蓋）。`run_update`
    的 stats 新增 `semantic_scopes_possibly_stale` 計數。
90. **CLI `update` 指令依健康狀態決定輸出與 exit code**（`cli/main.py`）：`run_update` 只在健康狀態
    不是 `ok`/`disabled` 時才把 `semantic_health_status`/`semantic_health_message` 放進 stats（這兩
    種預期、無需使用者處理的狀態完全不留痕跡）；CLI 把這兩個欄位從一般 `k=v` 那行拆出來，印出獨立
    的 `semantic: ...` 訊息，`rate_limited` 維持 exit code 0（只是提示），`config_error`/
    `probe_failed` 都回傳 exit code 1（大聲失敗），且都是在印出「Updated: ...」那行（決定性索引
    的結果）之後才失敗，確保索引本身的成功結果不會被吞掉或搞混。

新增 17 個測試（`tests/unit/test_semantic.py`：`probe()` 成功/429/傳輸錯誤、`ProviderError.
is_rate_limited`、`check_semantic_health` 的 5 種狀態、`mark_possibly_stale` 的 4 種情境；
`tests/unit/test_cli.py`：config_error 的 exit code 與訊息、possibly_stale 端到端）。每個新測試都
用 `git stash` 只還原 `src/` 的改動、保留新測試，確認測試在修法前確實會失敗（`ImportError`／
`assert 0 == 1` 等），再還原改動。192 個測試全綠，`ruff check` 全綠。

Milestone 6（Policies & Memory）是下一步；retrieval 端「`possibly_stale`/`stale` 不顯示舊摘要、
改指向 `source_files`」的規則（第十五輪決議第 2 點）屬於 Milestone 6 範圍，留到那時再做。

### 第十七輪修訂（使用者要求撤回第 88 條的簡化：`model` 空字串維持算設定錯誤）

91. **撤回第 88 條「`model` 空字串視同 disabled」的簡化**：第十六輪實作時，因為
    `SemanticConfig` 預設值正是 `enabled=True, model=""`（每個全新專案未動過 semantic 設定的
    狀態），為了不讓每個未設定過 semantic 的專案在每次 `rune update` 都大聲失敗，把「`model` 空
    字串」重新歸類為等同 `disabled`（靜默跳過）。**使用者明確不同意**：「我覺得 model 是空字串也
    是設定錯誤，因為未來當專案完工以後別人部署時就應該正確填入 api 否則無法正常使用」——這個專案
    部署完成後理當已經正確設定，`model` 空字串不該因為剛好是預設值就特殊放行，跟 API key 沒設
    一樣都是設定錯誤，應該套用「在初始啟動時就報錯」這條既有原則。已撤回，`check_semantic_health`
    的 Step 1 恢復成第十五輪原始措辭：`model` 空字串跟 API key 沒設、provider 名稱不認得，三者
    一律是 `config_error`（CLI exit code 1），只有 `semantic.enabled=false` 才是靜默跳過的
    `disabled`。
92. **連帶修正兩個假設「什麼都不設定也能正常 `rune update`」的既有測試**：
    - `tests/unit/test_cli.py::test_update_then_status_reports_fresh_again` 測的是 working-tree
      freshness 回報，跟 semantic 設定無關，只是意外依賴了「預設狀態下 semantic 不會出錯」這個
      現在已不成立的巧合。改成明確在 config.toml 寫 `semantic.enabled = false`，讓這個測試繼續
      只測它原本要測的東西。
    - `tests/unit/test_semantic.py::test_health_check_disabled_when_model_is_the_untouched_default`
      改名為 `test_health_check_config_error_when_model_is_the_untouched_default`，斷言從
      `SemanticHealthStatus.disabled` 改為 `SemanticHealthStatus.config_error`。
    兩者都先確認會因為第 91 條的程式碼改動而失敗（`assert 1 == 0`／`assert config_error is
    disabled`），改完後跟其餘測試一起全綠，不是憑空改斷言遷就程式碼。

已同步更新 ARCHITECTURE.md §4.5（第十二輪）、DATA_MODEL.md §2.4（第十一輪）的措辭，移除「`model`
空字串視同 disabled」這個已撤回的描述。192 個測試維持全綠，`ruff check` 全綠——本輪淨變更只有
`check_semantic_health` 裡一個 `if` 分支的邏輯，以及兩個既有測試的修正，沒有新增功能。

### 第十八輪修訂（Milestone 6 開工：current/visible 分離基礎 spike）

Milestone 6（Policies & Memory）的設計已在文件裡確認完畢（DATA_MODEL §1、§2.5、§2.6、§3、§6，
IMPLEMENTATION_PLAN 第 410 行起），沒有卡著的開放問題；範圍很大，跟使用者確認後決定先做一個小
spike 驗證最容易犯錯的「current 與 visible 分離」這條規則，其餘部分（proposal 流程、staleness、
orphan 偵測、單一寫入者衝突偵測、FTS5、`rune search` 排序、`rune check`）留到下一階段確認後再做。

93. **`rune.core.memory.revisions`（新模組）**：`current_revision(revisions: list[T]) -> T | None`
    純以 `max(revision)` 計算 current，完全不依 `status` 過濾候選集合；`is_decision_constraint_
    visible(status: RecordStatus)` 與 `is_note_visible(status: NoteStatus)` 分別實作 DATA_MODEL
    §3、§2.6 的可見性表格。用 PEP 695 泛型語法（`def current_revision[T: _Revisioned](...)`）讓
    同一個函式同時適用 `MemoryRevision`（Decision/Constraint）與 `Note`——兩者結構不同但都只需要
    `revision: int` 這個共同欄位，不需要各寫一份。
94. **鎖死 DATA_MODEL §3 明講的關鍵回歸案例**：`tests/unit/test_memory_revisions.py::
    test_current_revision_is_rev2_inactive_not_rev1_active`——`rev1=active, rev2=inactive` 時
    current 必須是 rev2，不能因為 rev1 的 status 在「可見集合」裡就被誤選為 current。用手動注入
    一版「naive」的錯誤實作（只在 `active`/`review_required` 狀態的 revision 裡取最大值）重新驗證
    這個測試真的會抓到這個錯誤（改完後 assert 失敗，訊息顯示誤選了 rev1），再改回正確實作——不是
    只憑直覺相信測試寫對了。另外 6 個測試涵蓋：輸入順序不影響結果、空 list 回傳 None、單一
    revision、Decision/Constraint 與 Note 各自的可見性表格、以及 Note 也能重用同一個
    `current_revision` 函式。

新增 7 個測試，199 個測試全綠，`ruff check` 全綠。這是 Milestone 6 的第一塊基礎，proposal 流程、
staleness、orphan 偵測等其餘交付項目待下一階段確認範圍與順序後再進行。

### 第十九輪修訂（使用者要求一路做到 Milestone 6 完成：proposal 流程、Note CRUD、staleness/orphan 偵測、FTS5）

使用者明確要求「接下來直接一路做直到把 M6 做完」，不再逐項確認範圍。以下每項都是照著
IMPLEMENTATION_PLAN 第 410 行起、DATA_MODEL.md §2.5/§2.5a/§2.6/§6、ARCHITECTURE.md §4.6/§4.7/§4.8
已確認的設計直接實作，沒有新的開放設計問題——唯一一處需要對照兩份文件用語做出取捨的地方在第 96 條
記錄。

95. **`rune.core.memory.hashes`**：`compute_source_hashes()`（重用 `core.semantic.worker.
    compute_source_files` 的「files ∪ symbol owning files」union 規則，供 constraint 的
    `source_bound` 與 Note 的 source-bound snapshot 共用）、`compute_scope_membership_hash()`
    （scope 的 files/symbols 排序後雜湊，刻意獨立於檔案內容，只偵測 membership 本身變動，供
    `scope_bound` 使用）。
96. **`rune.core.memory.proposals`**：`propose()`/`approve()`/`reject()`/`deactivate()`。`approve()`
    在核准當下自動計算 `source_hashes`/`scope_hashes`（讀取 SQLite 已索引的 files/symbols 與
    canonical `scopes.json`，人類完全不需手動輸入 hash），並在 `persistence_mode=temporary` 缺
    `expires_at`、`source_bound`/`scope_bound` 引用集合為空或指向不存在的 scope 時直接拒絕核准
    （`ProposalValidationError`）。`propose()` 在寫入當下就先做一次同樣的形狀驗證（decision 不能帶
    severity/persistence_mode/expires_at；constraint 必須帶 severity+persistence_mode），提早失敗，
    不必等到核准才發現無法核准的提案。`[E]dit` 走 `approve(edited_payload=...)`，`created_by` 固定
    記為 `RevisionAuthor.human`（編輯動作本身就是人類行為，即使原提案是 agent 提的）；純核准（未編輯）
    則沿用原提案 `created_by` 對應的 `RevisionAuthor`。`deactivate()` 是人類明確停用（附加
    `status=inactive` 完整 snapshot），與系統自動附加的 revision 分開的動作，`core.memory.staleness`
    永遠不會自動附加 `inactive`。
97. **`rune.core.memory.notes`**：`note_add()`/`note_update()`，不需要 proposal/approval 關卡。
    `files`/`symbols` 非空時自動計算 `source_hashes`（比照 constraint 的 approve 邏輯），這正是
    DATA_MODEL §6「只有設了 source_hashes 才 source-bound」規則的寫入端。寫入前套用既有的
    `redact_text`（Milestone 5 的 redaction 模組，重用不重寫，比照 IMPLEMENTATION_PLAN 的要求）。
    `note_update()` 附加新 revision 時預設完整帶過未指定的欄位（比照系統 revision 的 snapshot
    規則，即使這是 agent/human 動作而非系統動作，理由相同：不能讓後續讀者遺失欄位）。
98. **`rune.core.memory.staleness`**：`detect_decision_transitions()`/`detect_constraint_transitions()`
    /`detect_note_transitions()`，純結構/hash 比對，不呼叫 LLM。Decision 的觸發規則完全是存在性
    檢查（引用檔案內容改變不觸發，file/symbol 被刪除才 `review_required`，scope 整個消失才
    `orphaned`）；Constraint 的 `persistent` 模式完全比照 DATA_MODEL §6 表格字面意思「永不自動變動」
    ——連 scope/file 被刪除都不觸發，這是刻意的全面豁免，不是只豁免內容變動；`source_bound`/
    `scope_bound`/`temporary` 各自比對對應的 snapshot。已用「已經是 review_required/orphaned/
    inactive 的 revision 不重複觸發」的終止狀態集合確保冪等，不會每次 `rune update` 都重複附加一筆
    一樣的 revision。Note 的 orphan（scope 消失）優先權高於 TTL 到期，TTL 到期優先權高於
    source-hash 比對。
99. **記錄一處需要在兩份既有文件之間取捨用語的地方**：IMPLEMENTATION_PLAN 原本「Orphan 偵測」一句
    寫的是「Decision/Constraint/Note 引用的所有 scope/file/symbol 皆已從索引刪除時」附加 orphaned，
    但 ARCHITECTURE.md §4.6 的表格更精確地把「scope 整個消失」（→ orphaned）與「file/symbol 被刪除
    但 scope 還在」（→ review_required）分開描述。實作採用 ARCHITECTURE §4.6 表格的版本（更詳細、
    本輪稍早才確認）作為權威：orphaned 只在引用的 scope 真的消失時觸發，不是「所有引用都消失」這種
    更寬鬆的條件。這是調和既有文件用語，不是引入新行為。
100. **`core.update` 接線**：比照 semantic 的 orphan 偵測（`detect_orphaned_scopes`）「永遠跑，不受
    `full`/provider 是否可用影響」的既有模式，新的 decision/constraint/note revision 透過
    `rebuild_cache` 的新參數 `decisions_override`/`constraints_override`/`notes_override`（比照
    `scopes_override`/`semantic_override`）在同一次 transaction 內落地，canonical JSONL 的 append
    延後到 `rebuild_cache` 成功之後才執行（同樣的 all-or-nothing 理由）。`stats` 新增
    `decisions_transitioned`/`constraints_transitioned`/`notes_transitioned` 三個計數。
101. **FTS5 索引補上實際的寫入邏輯**：`fts_decisions`/`fts_constraints`/`fts_notes`/`fts_semantic`/
    `fts_symbols` 這五張虛擬表格自 Milestone 1 起就宣告在 schema.sql，但**從未有任何程式碼寫入過**
    （grep 確認過），代表 `rune search` 若真的接上 FTS5 查詢會永遠查到空結果——已用一個手工重現測試
    （`test_rebuild_cache_populates_fts5_indexes`）確認這個問題確實存在，修法前跑過一次真的失敗
    （`fetchone() == None`）。新增 `_materialize_fts()`：因為虛擬表格不受既有的 FK cascade 清空機制
    影響，每次 materialize 手動 `DELETE` 後重新寫入，只索引 current revision（不論 status，可見性
    過濾留給查詢層），驗證過「取代掉的舊 revision 內容不會繼續留在 FTS 索引裡」（新增第二個 regression
    case）。

新增 48 個測試：`test_memory_staleness.py` 20 個純函式測試、`test_memory_proposals.py` 15 個、
`test_memory_notes.py` 6 個、`test_memory_staleness_flow.py` 6 個端到端整合測試、`test_materialize.py`
新增 1 個 FTS5 regression test。端到端測試都透過 `python_simple_repo` fixture 跑過真實的
`init_project` → `run_update` → `rebuild_cache` 流程，不是只測純函式。FTS5 的 regression test 已用
`git stash` 只還原 `materialize.py` 確認修法前真的會失敗（`fetchone()` 回傳 `None`）。247 個測試
全綠，`ruff check` 全綠。

尚未完成：`rune.core.retrieval.search`（八層排序）、`rune check`、CLI 子命令
（`decision`/`constraint`/`note`/`proposal`/`search`/`check`）。

### 第二十輪修訂（`rune.core.retrieval.search`/`check`）

102. **`rune.core.retrieval.search`**：ARCHITECTURE §4.8 的八層排序，對五張 FTS5 表分別查詢後依
    record 目前的 status/severity/是否 scoped 分類。查詢字串一律包成 FTS5 phrase query（引號包住、
    內部引號雙寫跳脫）而非直接把使用者輸入丟給 MATCH——V1 不打算把 FTS5 完整查詢語法
    （AND/OR/NEAR/欄位過濾）暴露給 CLI 使用者，phrase query 也能避免使用者輸入的連字號/冒號等字元
    讓 MATCH 直接丟 `OperationalError`。**只有 MUST 分 global/scoped**（ARCHITECTURE §4.8 原文
    明講這輪修訂的重點就是「插入 Global/Scoped MUST 的區分」），SHOULD 與未列出的 INFO severity
    不分 global/scoped，統一算 rank 4；current+visible-with-warning 的 decision/constraint
    （`review_required`/`stale`）沒有獨立 rank，因為八層清單裡沒有定義，維持在原本 severity/type
    對應的 rank，只是額外帶 `warning` 欄位，不是自己發明一個新 rank。
103. **落實 Milestone 5 第十五輪決議第 2 點：`possibly_stale`/`stale` 的 semantic summary 絕不直接
    回傳舊摘要文字**：`_search_semantic` 對這兩種 status 回傳 `possibly_stale_pointer()`（指向
    `source_files` 的提示訊息），只有 `fresh` 才回傳真正的 `purpose` 內容；`unavailable`/`orphaned`
    完全跳過（原本就沒有可用內容）。這是這句決議寫下後第一次真正落地。
104. **`rune.core.retrieval.check`**：不呼叫 git diff 第二次，直接重用 `rune status` 既有的
    「掃描工作目錄 -> 對照 `files` 表的 content_hash」diff 機制，避免兩套「什麼算是變更」的定義互相
    打架。變更檔案 -> `scope_files` 反查受影響 scope -> `constraint_scopes` 反查相關 constraint，
    只回傳 current+visible 的 constraint（`inactive`/`orphaned` 排除），依 MUST 優先排序。刻意只回傳
    constraint（IMPLEMENTATION_PLAN 原文字面只提到「相關 constraint 清單」，不含 decision），global
    constraint（`scopes==[]`）不主動塞進每次 `rune check` 的輸出——它已經由 hard bootstrap（Milestone
    7）與 `rune search` 覆蓋，`rune check` 的職責範圍限定在「這次變更牽動了哪些範圍內的規則」。

新增 11 個測試（`test_retrieval_search.py` 6 個、`test_retrieval_check.py` 5 個），皆透過
`python_simple_repo`/`git_repo` fixture 跑真實的 propose/approve/note_add → `run_update` →
查詢流程。258 個測試全綠，`ruff check` 全綠。

尚未完成：CLI 子命令（`decision`/`constraint`/`note`/`proposal`/`search`/`check`）——Milestone 6
交付項目目前只剩這一塊，做完就是完整的 Milestone 6。

### 第二十一輪修訂（CLI 子命令，Milestone 6 收尾；手動 smoke test 揪出一個 staleness 無限重複附加的 bug）

105. **CLI 子命令**：`rune decision {propose,list,deactivate}`、`rune constraint {propose,list,
    deactivate}`、`rune note {add,update,list}`、`rune proposal {list,approve,reject,edit}`、
    `rune search QUERY [--history] [--json]`、`rune check [--json]`。比照既有 `scope` 子命令的模式
    （薄的參數解析層，呼叫恰好一個 `core` 進入點，`ARCHITECTURE.md §5`）。`--created-by`/`--source`
    自訂驗證只接受 `agent`/`human`（`Proposal.created_by`/`Note.source` 底層是 `Literal`，執行期
    不會自動擋，CLI 層要自己擋，否則打錯字會被靜默當成 human 處理）。`proposal edit` 直接把使用者給
    的覆蓋欄位 merge 進 `proposal.payload`（`model_copy(update=...)`）再呼叫
    `approve(edited_payload=...)`，一次完成 DATA_MODEL §2.5a 的 `[E]dit` 流程。
106. **手動 smoke test（在暫存 repo 跑過真實 CLI 指令，不只是自動化測試）發現一個 staleness
    無限重複附加的 bug**：`detect_constraint_transitions` 的 `source_bound`／`temporary` 分支、
    `detect_note_transitions` 的 source-bound 分支，附加 `status=stale` 新 revision 時只改了
    status/created_by/created_at，**沒有把比對用的 snapshot（`source_hashes`／`expires_at` 相關判斷）
    更新為目前的值**——`stale` 本身不在終止狀態集合裡（刻意的，因為 `source_bound` 內容之後可能再變、
    需要能再次觸發），所以每一次後續 `rune update` 都會拿同一份「已經過期」的舊 snapshot 重新比對，
    永遠比對不符，於是每次都多附加一筆一模一樣的 `stale` revision，長期下來會讓 `constraints.jsonl`/
    `notes.jsonl` 無限膨脹。修法比照 Milestone 5 `core.semantic.worker` 失敗 revision 的既有規則
    （`source_hash`/`source_files` 更新為目前值，即使沒有新內容，理由完全相同）：
    - `source_bound` constraint／Note：附加 `stale` revision 時把 `source_hashes` 更新為**現在**
      算出來的值，下次比對才有正確的新基準，只有內容**再次**變動才會再觸發。
    - `temporary` constraint：`expires_at` 本身不會變，用另一種方式做冪等——只在目前 status **還不是**
      `stale` 時才觸發，避免同一個已過期的 `expires_at` 每次都被判定為「還沒處理過」。
    - `scope_bound` constraint 附加的是 `review_required`（本來就在終止狀態集合裡，不會重複觸發），
      但仍順手把 `scope_hashes` 更新為目前值，維持三種 snapshot 欄位處理方式一致，避免之後有人只
      看 `source_bound` 分支就照抄卻漏掉 `scope_bound`。
    這個 bug 沒有被純函式單元測試抓到，因為原本的測試只驗證「觸發一次」，沒有驗證「觸發後不會再重複
    觸發」——已補上 3 個新的 idempotency regression test（`test_constraint_source_bound_stale_
    transition_is_idempotent`、`test_constraint_temporary_expiry_transition_is_idempotent`、
    `test_note_source_hash_stale_transition_is_idempotent`），每個都用「連續呼叫兩次，第二次結果
    必須是空 list」的方式驗證，且用 `git stash` 只還原 `staleness.py` 確認修法前這三個測試真的會
    失敗。這是本輪唯一的功能性 bug；`rune.core.retrieval.search`/`check` 兩個模組先前的手動驗證
    沒有再發現其他問題。

新增 3 個 regression test（idempotency）。261 個測試全綠，`ruff check` 全綠。**Milestone 6
（Policies & Memory）的六項交付項目至此全部完成**：current/visible 分離、proposal 流程、Note
CRUD、staleness/orphan 偵測、FTS5 + 八層排序 search、`rune check`，以及對應的 CLI 子命令。

### 第二十二輪修訂（使用者轉述外部針對 Milestone 6 的 code review，9 條 finding，逐條重現後全部確認為真並修正）

九條全部先重現才動手，兩條標記「設計確認」的先問過使用者才動手，其餘七條是直接違反已確認設計/文件
的真 bug，不需要重新討論設計本身。

107. **中·`source_bound` 核准接受部分 snapshot**：`approve()` 先前只檢查算出來的 `source_hashes`
    是否為空，沒檢查是否**完整**——`files=["a.py", "GONE.py"]` 這種情況下 `GONE.py` 解析不到就悄悄
    被丟掉，只記錄 `a.py` 的 hash，核准照樣成功。實測重現：確實如此。違反 DATA_MODEL §2.5「key
    集合必須等於 `files ∪ owning_file(s)`，缺一即拒絕核准」。修法：核准前額外檢查
    `missing_files`/`unresolved_symbols`，只要有一個沒解析到就整個拒絕，不再只看「結果是否為空」。
108. **中·Note TTL 是死代碼**：`NotesConfig.temporary_context_ttl_days`/
    `investigation_result_ttl_days` 從 Milestone 1 就存在，但 grep 全專案零呼叫點——`note_add`
    只有呼叫者手動傳 `expires_at` 才會有值，`temporary_context`/`investigation_result` 類別的 Note
    預設永不過期，違反 M6 交付項「依 category 有 TTL」。修法：`note_add` 在 `expires_at` 未指定時，
    對這兩個類別自動從 `config.notes` 算出預設到期時間；顯式傳入的 `expires_at` 仍優先。
109. **中·`--history` 永遠看不到被取代的舊 revision 內容**：FTS5 只索引 current revision、查詢也只
    join `current_revision`——實測重現：`d1` rev1=「use redis」被 rev2=「use postgres」取代後，
    `search('redis', history=True)` 回傳空陣列，因為「redis」這個字根本從未進過 FTS 索引。違反
    M6 驗收「history flag 才顯示兩筆」與 DATA_MODEL §3「history 模式可看到全部 revision」。修法
    是結構性的：`fts_decisions`/`fts_constraints`/`fts_notes` 新增 `revision` 欄位並索引**每一筆**
    revision（不只 current），`CACHE_SCHEMA_VERSION` 隨之從 2 bump 到 3（既有機制自動觸發丟棄重建，
    不需要使用者手動介入）；`core.retrieval.search` 的查詢改成先比對 FTS 命中的
    `revision` 是否等於該筆記錄的 `current_revision`，是則走現有的 current/visible 判斷邏輯，不是
    則只在 `history=True` 時納入、標記 `warning="superseded"`、排在 `RANK_HISTORICAL`。
110. **低·系統 note revision 覆寫 `created_at`**：`staleness.py` 的 `_note_system_revision` 與
    `notes.py` 的 `note_update` 都把 `created_at` 設成 `now`，但 DATA_MODEL §2.6 對 Note 的系統
    轉換只列了「`status`、`source`、`last_verified_at`」三個會變動的欄位——`created_at`（原始建立
    時間）應該原封不動跨 revision 保留，現行實作會讓這個資訊在 current 投影中遺失。修法：兩處都
    移除 `created_at` 的覆寫。
111. **低·CLI `proposal edit` 缺第五輪新增的欄位選項**：`core.memory.proposals` 已經支援
    `critical`/`source_document`/`source_section`/`machine_check_hint`，但 CLI 的 `[E]dit` 流程
    沒有暴露對應選項，人類想在編輯時調整這些欄位做不到。已補上
    `--critical/--not-critical`、`--source-document`、`--source-section`、`--machine-check-hint`。
112. **低·`rune search`/`rune check` 對損毀 `memory.db` 無防護**：實測用一個 0-byte 檔案模擬損毀
    的 `memory.db`，兩者都直接拋出未經處理的 `sqlite3.OperationalError` traceback，使用者看不出
    該怎麼修。新增 `materialize.connect_for_read()`：連線後先確認 `schema_meta` 表可讀，讀不到就
    包裝成 `CacheUnusableError`（訊息直接告訴使用者跑 `rune rebuild-cache`），`search`/`check` 改用
    這個函式，CLI 層也接住這個例外印出乾淨訊息而非 traceback。
113. **低·`approve()` 是兩次獨立、非原子的 canonical 寫入**：先寫 `proposals.jsonl` 再寫
    `decisions.jsonl`/`constraints.jsonl`，中間若崩潰會留下「proposal 顯示已核准，但實際內容從未
    寫入任何地方」的靜默遺失狀態，且 `pending` 清單也不會再顯示它、沒有恢復路徑。修法：對調寫入
    順序，先寫決定性內容（`decisions.jsonl`/`constraints.jsonl`），再寫 proposal 自己的
    resolution——同樣沒有做到真正的原子性，但把失敗模式從「靜默遺失」改成「可偵測、可恢復」（崩潰後
    內容已經在，proposal 只是還顯示 `pending`，重新核准前人類/後續流程至少看得到異常）。用
    monkeypatch 模擬「第二次寫入失敗」重現過修法前的靜默遺失問題，也確認新順序下失敗會被正確偵測。

**兩條標記「設計確認」的項目，先問過使用者才動手：**

114. **核准/新增的 memory 在下一次 `rune update` 前對 `rune search` 不可見**：SQLite 是 derived
    cache，這點跟 scope/semantic 完全一致，但 Milestone 7 的 OpenCode adapter 刻意不自動觸發
    `rune update`（避免每次 tool call 都扒一次昂貴的 update），意味著整個 session 期間剛核准的規則
    都搜尋不到。**使用者選擇：讓 `approve()`/`note_add()`/`note_update()`/`deactivate()` 自動觸發
    一次輕量 materialize**——新增 `core.memory.records.refresh_cache()`，重用已索引的
    `read_current_code_index()`（不重新掃描/解析原始碼）搭配 `rebuild_cache()`，比完整
    `rune update` 便宜很多，只是把目前 canonical 狀態重新投影進 SQLite。每個會寫 decisions/
    constraints/notes 的動作都接上這個呼叫。
115. **`rune check` 只回傳 scoped constraint，完全不含 global constraint**：先前是我自己照字面
    解讀 IMPLEMENTATION_PLAN 做的範圍限定。**使用者選擇：改成也包含現行 current+visible 的 global
    MUST constraint**——只要有任何變更檔案，就無條件把目前的 global MUST 規則一起列出（不需要跟
    changed files 有任何 scope 關聯），因為全域 MUST 規則本來就對任何變更都相關。SHOULD/INFO
    severity 的 global constraint 不在此列，維持原本的 scoped-only 邏輯，只有 MUST 破例。

新增 22 個 regression test（`test_memory_proposals.py` +5、`test_memory_notes.py` +5、
`test_memory_staleness.py` +2、`test_retrieval_search.py` +3（含 3 個既有測試因為索引全部
revision 而改寫斷言，屬於預期行為變更，非迴歸）、`test_retrieval_check.py` +3、
`test_materialize.py` 既有 FTS5 測試改寫斷言）。每一條 finding 都先寫重現腳本確認問題真的存在，
再動手修；「approve 兩段寫入非原子」用 monkeypatch 模擬崩潰、「history 找不到舊內容」用真實
propose/approve 流程重現、「0-byte memory.db」用手工寫入空檔案重現。275 個測試全綠，`ruff check`
全綠。

### 第二十三輪修訂（使用者再轉述一份針對 Milestone 6 的外部 code review，20 條 finding，逐條確認）

20 條裡有 9 條是上一輪（第二十二輪）已經修過的問題（重複轉述，非新問題），已重新確認修法仍然有效、
未被本輪改動影響，不重複記錄。真正新確認、動手修的是以下 5 條，皆先寫重現腳本確認問題存在：

116. **高·`rune check` 漏掉直接 file/symbol-bound 的 Constraint**：`check()` 先前只透過
    `constraint_scopes` 反查（scope 路徑），一個沒有掛任何 scope、只靠 `persistence_mode=
    source_bound` 的 `files`/`symbols` 直接綁定的 constraint，即使綁定的檔案剛好變更，也完全不會
    出現在 `rune check` 的輸出。實測用 SHOULD severity（避開 Global MUST 分支誤判）重現：確實
    找不到。修法：新增兩條額外查詢路徑，直接 join `constraint_files`（檔案比對）與
    `constraint_symbols`（透過 `symbols` 表找出 symbol 的 owning file 是否在變更清單中，同時涵蓋
    symbol 被刪除的情況——symbol 被刪除必然代表其所在檔案也變了，所以檔案層級的比對本來就會涵蓋
    到），跟原本的 scope 路徑、global MUST 路徑取聯集。
117. **中·edited proposal 可以偷偷改變 `record_id` 或 `type`**：`approve(edited_payload=...)`
    先前完全不檢查 `edited_payload.record_id`/`.type` 是否跟原本要核准的 proposal 一致——實測重現：
    把 `edited_payload.record_id` 改成一個完全不相關的值，核准照樣成功，內容被寫進了錯的
    record_id，而原本的 proposal 卻顯示「已核准」，形同一次沒有任何錯誤訊息的靜默劫持。修法：
    `approve()` 在使用 `edited_payload` 時，強制檢查兩者的 `record_id`/`type` 必須與原 proposal
    相符，不符就拒絕。
118. **中·`current_by()`（`core.memory.records`）對重複 revision 不拒絕**：`materialize.py` 自己
    的 `_group_current_by_id` 早就會偵測 `(id, revision)` 重複並拒絕（`CanonicalConflictError`），
    但 `core.memory.records.current_by()`——propose/approve/note_add/staleness 全部直接呼叫、
    不經過 `rebuild_cache` 的另一條讀取路徑——完全沒有做一樣的檢查，實測重現：兩筆
    `revision=2` 的 decision 丟進去，靜默選了其中一筆，沒有任何錯誤。這代表「canonical 衝突視為
    致命錯誤」這件事只在其中一條讀取路徑上成立，另一條路徑會在資料已經損毀的情況下繼續假裝一切
    正常。修法：把同樣的重複偵測邏輯搬進 `_group_by_id`，兩條路徑現在行為一致。
119. **中·Note 的 `note_update()` 無法更新 binding/TTL/metadata**：先前只能改
    `content`/`why_persist`/`status`/`evidence`，`scopes`/`files`/`symbols`/`expires_at`/
    `importance`/`confidence` 建立後完全無法修改，形同 CRUD 裡永遠缺一角的 U。修法：全部補上
    對應的可選參數，其中 `files`/`symbols` 變動時會重新驗證並用新的值重算 `source_hashes`
    （沿用下面第 120 條的驗證邏輯）；`expires_at` 因為 Python 用 `None` 同時代表「不改」跟「清掉」
    有歧義，另外新增 `clear_expires_at: bool` 明確表達「清掉」這個意圖。
120. **中·`note_add()` 對不存在的 references 驗證不足**：跟第 107 條修過的 constraint
    `source_bound` 核准是同一類問題，只是發生在 Note 身上——`files=["app/services.py",
    "GONE.py"]` 這種情況下，`GONE.py` 解析不到就悄悄從 `source_hashes` 消失，`note_add()` 本身
    不會報錯；`scopes` 引用不存在的 scope id 也完全沒有驗證。修法：新增 `NoteValidationError`，
    `note_add`/`note_update` 都在寫入前檢查給定的 `scopes`/`files`/`symbols` 是否全部能解析，
    有一個解析不到就整個拒絕（不是「至少一個成功就好」），跟 constraint 核准的標準一致。

**確認已修過、本輪未再變動的 9 條**（第 107-115 條原始編號對照）：`source_bound` 核准接受部分
snapshot（107）、Note TTL 死代碼（108）、`--history` 找不到舊 revision（109）、系統 note revision
覆寫 `created_at`（110）、CLI `proposal edit` 缺欄位（111）、`search`/`check` 對損毀 db 無防護
（112）、`approve()` 非原子寫入（113，見下方說明其現實邊界）、核准後 cache 不同步（114）、
`rune check` 缺 Global MUST（115）。

**四條評估後判定為既有設計邊界、非本輪修復範圍，向使用者說明理由而非直接動手**：
- `approve()` 仍非嚴格原子（第 113 條的殘留部分）：上一輪已經把失敗模式從「靜默遺失」改成「可偵測、
  可恢復」，這是 V1 單一寫入者假設下、不引入完整 two-phase-commit 機制的現實上限，跟這個專案對
  `project.json`/`memory.db` 非原子性採取的態度一致（記錄為已知限制，而非追求完美原子性）。
- `rune check` 顯示的是最近一次 `rune update` 材質化後的 status，不是「假設現在就跑 update 後會
  變成什麼」的預測值——`rune status` 對工作目錄新鮮度也是用同樣「顯示已知狀態 + 另外報告差異」的
  模式，不是重新計算假設性的未來狀態，`rune check` 沿用同一設計慣例。
- `--history` 的名稱與行為並無不符：DATA_MODEL §3 明講「history 模式可看到全部 revision」，目前
  `--history` 正是做這件事（含被取代的舊 revision 內容）。
- FTS 索引只涵蓋 `content`/`rationale`/`why_persist` 等自由文字欄位，不含 `scopes`/`files`/
  `symbols`/`severity`/`category` 等結構化欄位——這些欄位本來就有各自的 SQL 直接查詢管道（`rune
  check` 剛好是最好的例子），FTS 全文檢索補的是「自然語言關鍵字搜尋」這個不同的需求，不是要取代
  結構化查詢；新檔案還沒被分進任何 scope 時 `rune check` 的 scope 路徑自然找不到對應的
  scope-bound constraint，這是正確反映現況（沒有 scope 就沒有 scope-bound 規則適用），第 116 條
  修完後，直接綁定檔案/symbol 的 constraint 已經不再受這個限制。

新增 8 個 regression test（`test_retrieval_check.py` +2、`test_memory_proposals.py` +3、
`test_memory_notes.py` +5，其中 `note_update` 相關 3 個）。每條都先用 `git stash` 只還原
`check.py`/`proposals.py`/`records.py`/`notes.py` 這四個檔案，確認新測試在修法前真的會失敗
（`ImportError`、`DID NOT RAISE`、找不到結果），才視為有效。286 個測試全綠，`ruff check` 全綠。

### 第二十四輪修訂（使用者第三次轉述外部 code review，8 條 finding，逐條重現後全部確認為真並修正）

這輪 8 條全部先寫重現腳本確認問題存在，沒有標記「設計確認」的項目——都是直接違反已確認設計/文件或
明顯的實作缺陷，不需要重新討論設計本身。

121. **中·`refresh_cache()` 在 `memory.db` 不存在時用空 code index 重建，製造出一個具誤導性的
    「存在但是空的」cache**：從未跑過 `rune init`/`rune update` 的專案直接呼叫 `note_add()`，
    `refresh_cache()` 會用空的 `CodeIndexData` 呼叫 `rebuild_cache`，建出一個 `files` 表是空的
    `memory.db`——`rune check`/`rune status` 讀到「`files` 表是空的」時，解讀成「所有檔案都是新增
    /變更」而非「根本沒有 cache，沒東西可以比對」。實測重現：`check()` 對完全沒改動過的 `a.py`
    誤報為 `changed_files`。修法：`refresh_cache()` 在 `memory.db` 不存在時直接跳過（no-op）——
    專案還沒有任何 code index 可以保留，這個「輕量」refresh 沒有安全的事可做，canonical 寫入本身
    已經成功，第一次真正的 `rune update` 會正確地把一切都材質化好。
122. **中·舊版 schema 的 cache 通過 `connect_for_read()` 後，`rune search`/`rune check` 仍會原始
    crash**：`connect_for_read()` 先前只確認 `schema_meta.schema_version` 這個欄位「可以查詢」，
    沒有拿它去跟 `CACHE_SCHEMA_VERSION` 比對——這個比對只有寫入路徑的 `_ensure_compatible_cache_
    schema` 在做。實測重現：手動把 `schema_meta` 的版本改回舊值、把 `fts_decisions` 改回舊的
    2-欄位形狀（模擬工具升級後、下次寫入觸發 self-heal 之前的窗口），`search()` 直接丟出
    `OperationalError: no such column: f.revision`，沒有被 `connect_for_read` 攔下來。修法：
    `connect_for_read()` 額外比對 `stored_version != CACHE_SCHEMA_VERSION`，不符就拋
    `CacheUnusableError`（讀取路徑沒辦法像寫入路徑一樣自動丟棄重建，只能明確告訴使用者跑
    `rune rebuild-cache`）。
123. **中低·`_refresh_cache()` 失敗時，canonical 已寫成功但 CLI 拋原始 traceback**：
    `note_add_cmd`/`note_update_cmd`/`decision_deactivate`/`constraint_deactivate`/
    `proposal_approve`/`proposal_edit` 這六個 CLI 指令的 `except` 子句都沒有涵蓋
    `CanonicalConflictError`（`_refresh_cache()` 內部 `rebuild_cache()` 在偵測到*其他*canonical
    檔案有衝突時會拋出）。實測重現：先核准一個正常的 decision，再手動在 `decisions.jsonl` 塞入一組
    無關的重複 revision 製造衝突，接著跑 `constraint deactivate`——這次要寫入的內容本身已經成功寫進
    canonical，CLI 卻印出完整 Python traceback，使用者看不出「這次操作到底成功了沒」。修法：新增
    `_handle_cache_refresh_failure()`，六個指令都在 `except CanonicalConflictError` 印出明確訊息
    （「這次寫入本身成功了，但 cache 沒能刷新」）並指向 `rune rebuild-cache`，不是原始 traceback。
124. **低·崩潰復原後重跑 `approve()` 不冪等，會附加重複內容**：第二十二輪的寫入順序調整（先寫
    `decisions.jsonl`/`constraints.jsonl`、再寫 `proposals.jsonl`）解決了「靜默遺失」，但沒解決
    「重跑會不會重複」——實測重現：模擬 `proposals.jsonl` 那次寫入失敗，proposal 仍是 `pending`
    但 decision 內容已經寫入；重新呼叫 `approve()` 卻又附加了一筆內容完全相同的 rev2。修法：
    `approve()` 寫入新 revision 之前，先比對「即將寫入的內容」與該 record_id 現有 current
    revision 的每一個核准相關欄位（`content`/`rationale`/`scopes`/`files`/`symbols`/`severity`/
    `persistence_mode`/`expires_at`/`critical`/`source_document`/`source_section`/
    `machine_check_hint`/`source_hashes`/`scope_hashes`/`created_by`/`approved_by`/`status`，
    刻意排除天生每次都會變的 `revision`/`created_at`）——全部相符時視為這就是同一次重試，直接沿用
    現有 revision，不重複附加；只要有任何一個欄位不同，就正常附加新 revision（兩次「獨立」核准
    在所有這些欄位上都恰好逐字相同，機率上幾乎等於就是同一次重試）。
125. **低·`search --json` 未輸出 `revision` 欄位**：`SearchResult` 為了第二十二輪的 `--history` 修法
    新增了 `revision` 欄位，但 CLI 的 JSON 輸出字典沒有跟著補上，機器消費者拿不到「這筆 superseded
    命中來自哪個 revision」這個關鍵資訊。修法：`--json` 輸出補上 `revision`。
126. **低·`NotesConfig` 的 TTL 沒有正值驗證**：`temporary_context_ttl_days = -1` 先前會被直接接受，
    實測重現：一個 `temporary_context` 類別的 Note 一建立就已經過期。修法：兩個 TTL 欄位都加上
    `Field(gt=0)`。
127. **低·`proposal edit --critical` 用在 constraint proposal 被靜默接受**：`critical` 是
    DATA_MODEL §2.5 明講的「Decision-only convention」，但 `_validate_payload_shape` 先前完全沒
    檢查這個欄位，`constraint_revisions` 表本來就沒有 `critical` 欄位——實測重現：把
    `critical=True` 塞進一個 constraint 的 edited payload，核准照樣成功，canonical 存下
    `critical: true`，材質化進 SQLite 時這個值就直接沒地方放，canonical 跟衍生資料從此不一致，
    沒有任何錯誤訊息。修法：`_validate_payload_shape` 新增檢查，constraint 不得設 `critical`，
    decision 不得設 `machine_check_hint`（同樣道理，`decision_revisions` 沒有這個欄位）；
    `source_document`/`source_section` 兩張表都有對應欄位，不受限制。
128. **低·CLI `note update` 沒有暴露 core 在第二十三輪新增的 `scopes`/`files`/`symbols`/
    `importance`/`confidence`/`expires_at` 參數**：core 層已經支援，CLI 沒有對應選項，造成
    core/CLI 行為漂移。修法：補上 `--scope`/`--file`/`--symbol`/`--importance`/`--confidence`/
    `--expires-at`/`--clear-expires-at`。

新增 12 個 regression test（`test_memory_notes.py` +1、`test_materialize.py` +1、
`test_memory_proposals.py` +3、`test_config.py` +1、`test_cli.py` +2，其餘散落在既有檔案內的
斷言強化）。每一條都先寫重現腳本、實際看到問題發生，才動手修——包括用 monkeypatch 模擬崩潰重現
finding 124、手動竄改 `schema_meta`/`fts_decisions` 表形狀重現 finding 122。295 個測試全綠，
`ruff check` 全綠。

## Milestone 7 開工（spike）

### 第一輪修訂（開工前 spike：`rune scope-for` + 最小 TypeScript adapter 骨架）

129. **`core.retrieval.scope_for`（新模組）+ `rune scope-for <path> --json`**：這是本輪唯一需要
    新設計的部分——ARCHITECTURE §6 先前只點名這個指令會被 `tool.execute.before` 呼叫，沒有定義
    確切 JSON 格式。設計方式完全重用既有 building block，不是憑空發明：`semantic_objects`
    （Milestone 5 的 summary，`possibly_stale`/`stale` 一樣改回傳指向 `source_files` 的提示，跟
    `rune search` 同一套規則）、`constraint_scopes`/`note_scopes`（Milestone 6 的 current+visible
    過濾規則）。**唯一的新規則**：只回傳 MUST/SHOULD constraint，INFO severity 不主動注入（
    ARCHITECTURE §7.1：INFO 只值得被動查詢，不值得每次 scope activation 都佔用 context，這點
    ARCHITECTURE §7 原本就隱含在三層規範分工表格裡，本輪只是第一次真正需要在程式碼裡落實這個
    篩選）。一個檔案可能屬於零到多個 scope，回傳陣列，空陣列是正常結果不是錯誤。細節已寫進
    ARCHITECTURE.md §6（第十三輪）。
130. **`adapters/opencode/`（新增最小 TypeScript 骨架，不是完整 adapter）**：`package.json`/
    `tsconfig.json`（`strict: true`）、`src/rune-cli.ts`（adapter 唯一允許呼叫 `rune` CLI 的
    地方，`execFile` + JSON.parse，`RUNE_CLI_PATH` 環境變數可覆寫供開發/CI 環境指向 venv 裡的
    `rune.exe`，不需要真的裝到全域 PATH——真正部署（Milestone 8 打包）預期 `rune` 就在 PATH 上）、
    `src/spike.ts`（模擬 `tool.execute.before`：呼叫 `scope-for`、用 `active_scope_ids` Set 做
    dedup、印出注入內容）。**沒有連到真實 OpenCode host**（沒有可用的 OpenCode 執行環境可以測），
    spike 驗證的是「這個機制本身走得通」，不是「真的部署進 OpenCode」。
131. **spike 對照一個手工建立、有真實資料的暫存 repo 實際跑過**：`rune init` → `rune scope create`
    → `rune constraint propose`/`approve`（MUST，scoped）→ `rune note add`（scoped）→
    `rune update`，然後跑 `npm run spike -- <repo-dir> app/services.py`，確認：
    - TypeScript 端能透過純 CLI `--json` 呼叫拿到正確的 scope/constraint/note 資料（不是
      import Python、不是碰 SQLite）
    - 同一個 scope 第二次呼叫（模擬同一 session 內再次觸及同一 scope 的檔案）被 dedup 邏輯正確
      跳過，只印一次「injecting context」

新增 8 個 Python regression test（`tests/integration/test_retrieval_scope_for.py` 7 個、
`tests/unit/test_cli.py` 1 個 CLI JSON round-trip）。TypeScript 端目前只有手動跑過的 spike
腳本，還沒有自動化測試框架（Milestone 7 全量開發時再補，這輪的目標只是驗證路徑可行）。303 個
Python 測試全綠，`ruff check` 全綠。

**尚未開始**（spike 完成時）：`rune bootstrap --mode hard|soft --json`、`core.retrieval.context`、
session-start/session-compaction hook、`tool.execute.before` 掛 constraint delivery（含
`bash` 的事後偵測）、custom tool 註冊（`decision_propose`/`constraint_propose`/`note_add`）。

### 第二輪修訂（`rune bootstrap` + `core.retrieval.context`；`core.status` 抽取）

132. **`core.status.compute_status()`（從 `cli.main.status` 抽出）**：`core.retrieval.context`
    的 soft bootstrap 需要跟 `rune status` 完全一樣的 working-tree 新鮮度計算（cache 檔案/符號數、
    tree hash 比對、modified/added/deleted 計數），與其在 `context.py` 裡重寫一份「幾乎一樣但細節
    可能悄悄分岔」的邏輯，不如把 `cli.main.status` 原本內嵌的計算搬進 `core.status`，`status()` 
    CLI 命令改成呼叫它。純粹搬移，行為不變——`status()` 原本的錯誤處理（`project.json` 缺失/無法
    讀取回傳 `None`、掃描失敗永不拋例外只回報 `working_tree_fresh=False`）逐字保留。
133. **`core.retrieval.context`（新模組）+ `rune bootstrap --mode hard|soft --json`**：落實
    ARCHITECTURE §7.3-§7.7 已經定案的設計，沒有新的設計決策，純粹是把規格轉成程式碼：
    - **Hard bootstrap**：current+visible 的 Global MUST Constraint（`severity=MUST` ∧
      `persistence_mode=persistent` ∧ `constraint_scopes` 無對應列 ∧ `status` 屬於
      `active/review_required/stale`，跟 `rune check` 的 global MUST 查詢用同一組可見狀態）＋
      `critical=true` 的 global Decision（`decision_scopes` 無對應列 ∧ `status` 屬於
      `active/review_required`，跟 `rune search` 的 `_DECISION_VISIBLE` 同一組）。`estimated_tokens`
      用 `len(text)//4` heuristic（無真實 tokenizer 依賴，只需要「大致抓超出預算」，不需要精確
      對齊任何特定模型的真實 tokenization）。**§7.7 的「不可靜默截斷」規則**：超出
      `bootstrap.hard_budget_tokens` 只設 `overflow=true`，絕不丟棄任何一條 constraint/decision——
      有 regression test（`test_hard_bootstrap_overflow_flagged_never_drops_constraints`）直接斷言
      5 條刻意撐爆預算的 MUST constraint 全部原樣回傳。
    - **Soft bootstrap**：project overview（複用 `compute_status`）、scope 清單＋摘要（只有
      `status=fresh` 的 semantic summary 才附上，`possibly_stale`/`stale` 一律 `summary=None`——
      跟 hard bootstrap 不同，soft bootstrap 沒有「不可截斷」的義務，模糊或過期的摘要不值得硬塞）、
      非 critical 的 global active Decision。**刻意省略 ARCHITECTURE §7.3 提到的「近期相關變更」**：
      整份規格裡從未定義過這個欄位該用什麼查詢產生（`core.retrieval` 沒有任何「recent changes」
      的既有機制），與其臨時發明一個新查詢形狀，不如明確記錄成範圍縮減——`rune check` 的
      working-tree diff 已經覆蓋這個專案目前唯一定義過的「什麼變了」需求。
    - CLI 輸出格式逐字對照 ARCHITECTURE §7.6 的 JSON 範例（`mode`/`constraints`/`decisions`/
      `estimated_tokens`/`budget_tokens`/`overflow`）；`--mode` 只接受 `hard`/`soft`，其餘值
      exit 1。

新增 13 個測試（`tests/integration/test_retrieval_context.py` 12 個、`tests/unit/test_cli.py` 1 個
CLI JSON round-trip，涵蓋 hard/soft 兩種 mode）。316 個 Python 測試全綠，`ruff check` 全綠。

**尚未開始（此輪結束時）**：session-start/session-compaction hook（呼叫這裡新增的
`rune bootstrap`）、`tool.execute.before` 掛 constraint delivery（含 `bash` 的事後偵測）、
custom tool 註冊（`decision_propose`/`constraint_propose`/`note_add`）——這幾項全部卡在 OpenCode
真實 plugin API 與 ARCHITECTURE.md §6 既有設計之間發現的落差，見下一輪修訂前必須先跟使用者確認的
問題。

### 第三輪修訂（跟使用者確認兩層 API 落差後，完成 Milestone 7 全量開發）

134. **確認 hook 形狀落差的因應方式與目標 API 介面**：使用者選擇「改用真實的 `event` hook +
    discriminated union」與「目標鎖定 classic `Hooks` interface（不投入時間探索 v2/effect API）」。
    細節見 ARCHITECTURE.md §6 第十五輪修訂。
135. **開發過程中發現第二層更深的落差、進一步跟使用者確認**：`tool.execute.before` 真實型別的
    `output` 只有 `{args: any}`——完全沒有任何形式的文字/訊息注入通道，只能修改「這次 tool 呼叫
    自己的參數」。型別定義裡唯一的 system-level 注入通道是標記為 experimental 的
    `experimental.chat.system.transform`（`output: {system: string[]}`，每次 LLM 呼叫前執行）。
    使用者給出具體設計指示：**不要做成一次性 queue-drain**，改成每個 session 持續維護狀態（hard
    bootstrap／active scopes／pending events 三個桶），`system.transform` 每次都重新 render 整份
    狀態；**已有 entry 時絕不建立第二個 `output.system` 元素**（部分 OpenAI-compatible provider 拒絕多個
    system-role 訊息），改成原地覆寫既有 marker 區塊或附加到最後一個既有字串；
    `client.session.prompt({noReply:true})` 只留作未接入的降級 fallback，不是 V1 主要機制。細節
    與完整設計理由見 ARCHITECTURE.md §6.1（第十六輪）。
136. **`adapters/opencode/src/rune-context.ts`（新模組，純邏輯、零 OpenCode/CLI 依賴）**：
    `RuneSessionContext` 類別（`setHardBootstrap`/`addActiveScope`/`enqueuePendingEvent`/`render`）
    + `mergeRuneBlock()`（`<!-- rune-context:start/end -->` marker 原地覆寫邏輯）+
    `renderSoftBootstrap()`。刻意設計成不依賴任何 OpenCode 型別或 `rune` CLI，純粹是「拿到 rune
    回傳的結構化資料 -> 渲染成文字」，方便獨立單元測試而不需要 mock 整個 Hooks/PluginInput 介面。
137. **`adapters/opencode/src/plugin.ts`（新模組，真正的 `Plugin` 匯出）**：`createRuneHooks()`
    接受一個可替換的 `RuneClient` 介面（`scopeFor`/`bootstrapHard`/`bootstrapSoft`/
    `changedFilesFromGitStatus`），生產環境用真的 CLI-backed 實作，測試用假的——這個介面切分純粹
    是為了讓 `plugin.test.ts` 不用打真實 `rune` CLI 就能測 hook 邏輯。實作 `event`（session.created
    seed hard+soft bootstrap，session.compacted 只重新 seed hard bootstrap）、`tool.execute.before`
    （非 bash：從 `extractPathsFromToolArgs` 取路徑，呼叫 `scope-for` 累加進 active scopes）、
    `tool.execute.after`（bash：`git status --porcelain` 事後偵測變動檔案，同樣累加進 active
    scopes）、`experimental.chat.system.transform`（render 目前狀態、`mergeRuneBlock` 寫入）、
    `tool`（`decision_propose`/`constraint_propose`/`note_add` 三個 custom tool，直接呼叫對應
    CLI）。`injectViaPromptFallback()` 文件化但未接入主流程。
138. **`adapters/opencode/src/tool-paths.ts`（新模組，純邏輯）**：`extractPathsFromToolArgs()`
    從 `tool.execute.before` 的 `output.args` 猜測 `filePath`/`path`/`file_path` 欄位名、
    `apply_patch` 解析 `*** Add/Update/Delete File:` 標記。**這組欄位名稱沒有真實 OpenCode host
    可以驗證**（使用者本輪明確指示不需要驗證），刻意 fail open：猜錯欄位名的後果是「這次 tool 呼叫
    沒有觸發任何 context 注入」，不是注入到錯的路徑——比默默用錯路徑安全。
139. **`rune-cli.ts` 補齊 `bootstrapHard`/`bootstrapSoft`/`decisionPropose`/`constraintPropose`/
    `noteAdd`/`changedFilesFromGitStatus` 六個新的 CLI wrapper**，對照 Python CLI 的實際旗標
    （`rune bootstrap --mode hard|soft`、`rune decision propose ... --json`、`rune constraint
    propose ... --json`、`rune note add ... --json`）。
140. **Python CLI 端補上三個 `--json` 旗標**：`decision propose`/`constraint propose`/`note add`
    先前只有人類可讀輸出，custom tool 需要拿到 `proposal_id`/`record_id`/`status`（或
    note 的 `id`/`category`）才能回報給 agent，本輪各自加一個 `--json`，輸出格式跟其餘 CLI 命令的
    `--json` 慣例一致。

新增 20 個 TypeScript 測試（`rune-context.test.ts` 11 個、`plugin.test.ts` 6 個、
`tool-paths.test.ts` 5 個，Node 內建 `node:test`，`adapters/opencode` 下 `npm test`
執行）涵蓋使用者明確要求的六個場景：全域 MUST 每次 LLM 呼叫都在、scoped constraint 跨呼叫持續存在、
不重複累積 Rune block、compaction 後 hard bootstrap 確實換新、不產生第二個 system 訊息、不同
session 狀態互不外洩。新增 4 個 Python regression test（三個 `--json` 旗標的 CLI round-trip）。
317 個 Python 測試全綠、`ruff check` 全綠；20 個 TypeScript 測試全綠、`tsc` 編譯無錯誤。**依然沒有
連到真實 OpenCode host**（使用者本輪明確指示不需要）——`extractPathsFromToolArgs` 的參數欄位名稱、
`experimental.chat.system.transform` 的實際行為（是否真的每次呼叫前執行、`output.system` 的實際
用法）都只驗證到「型別檢查通過」，沒有驗證到「真的在 OpenCode 裡跑起來符合預期」，這是 Milestone 7
交付前最後需要用真實 host 驗收的部分。

### 第四輪修訂（純設計文件修訂，不含任何程式碼變更——部署模型、`protocol_version`、git worktree／
多 agent 協作、scope reconciliation 治理）

使用者要求針對 Milestone 7 spike 完成後浮現的幾個部署/協作層級問題，先只修訂 ARCHITECTURE.md／
DATA_MODEL.md／IMPLEMENTATION_PLAN.md／HANDOFF.md 四份文件，本輪明確不寫程式碼。逐項對照現有文件
確認無矛盾（見下方「一致性檢查」）後，新增以下正式設計決議：

141. **部署模型與安裝單位**（ARCHITECTURE.md 新第 14 節）：`rune` core（Python）與
    `adapters/opencode`（TypeScript）同 repo、不同 installation unit，具體 package 名稱本輪不鎖死。
    確認「machine-level install once，per-repo `rune init`」的部署模型；OpenCode plugin 啟動時
    偵測 `.rune/` 是否存在決定是否啟用（不存在則 silent no-op），**plugin 絕對不可自動執行
    `rune init`**——這是使用者明確導入 governance 的動作，不是 plugin 可以代為決定的事。CLI
    discovery 沿用既有的 PATH + `RUNE_CLI_PATH` override（既有實作行為，本輪只是正式記錄成部署
    設計）。確認 V1 不引入 daemon；MCP server（Milestone 8）保留作未來 generic client 整合手段，
    不是 OpenCode V1 的必要依賴。
142. **`protocol_version` adapter/core 相容性契約**（ARCHITECTURE.md 新第 6.2 節）：所有面向
    adapter（未來含 MCP）的 `--json` 輸出頂層應包含 `protocol_version: int`，語意上是「adapter/core
    介面版本」，跟 RepoRune 套件版本、`CACHE_SCHEMA_VERSION`、canonical `schema_version` 都是各自
    獨立的版號，只在輸出形狀變動到舊版 adapter 會解析錯誤的程度才遞增。Adapter 遇到不支援的
    `protocol_version` 必須大聲失敗，明確提示「core/adapter 版本不相容」，不允許 schema 不相容時
    silently best-effort parse。**本輪只定義設計，未修改任何現有 CLI `--json` 輸出**——列為
    Milestone 7 收尾前必須補齊的 contract，見下方「尚待實作」清單。
143. **Git worktree／多 agent 協作模型**（ARCHITECTURE.md 新第 16 節）：確認 worktree 場景下
    derived state（cache/logs）per-worktree、gitignored、不參與 merge；canonical memory 跟著
    branch 走、正常參與 git merge；同一 logical record 的 revision 衝突沿用既有「單一寫入者假設」
    （第 12 節、DATA_MODEL §8），**不自動 resolve，不改成 ULID revision graph**。定義 parallel
    worktree 允許做（讀 memory、改程式碼、建 Note、建 proposal）與不應該做（同時批准同一筆
    Decision/Constraint、把 branch-local semantic refresh 當成 merge 後權威狀態）的邊界；定義
    integration worktree 的角色（merge 程式碼、resolve canonical 衝突、review/approve 治理
    proposal、跑最終 `rune update`／semantic refresh／scope reconciliation）。
144. **Merge reconciliation 的 provenance 原則**（ARCHITECTURE.md §16.5，正式架構原則）：
    "Merge reconciliation follows provenance." Derived/reproducible state（file/symbol/graph
    index、衍生 SQLite cache、semantic summary、受 §4.4 規則限制的 auto-inferred scope
    membership）可以在 merge 後重新產生；human-authoritative state（已核准 Decision/Constraint、
    human-authoritative scope 定義、`locked`/human-confirmed membership）不可由 AI 根據 merged
    code 自動重新發明，只能 merge 或明確 reconcile。AI 對治理層衝突可以提 reconciliation
    proposal，但不能自己成為 authoritative——與既有第 7.9 節「Global MUST 一樣要走 propose/approve」
    同一條原則的延伸適用。
145. **Scope Membership Reconciliation**（ARCHITECTURE.md §4.4 新增小節，泛化既有第 4.4 節第 3 點
    的 incremental 自動併入規則，**本輪最重要的新規則**）：明確 reconciliation（含未來
    `rune update`／`rune scope reconcile`）預設必須 incremental，範圍限定在 changed set（新增/
    修改/刪除/重新命名的檔案與 symbol），沒有實際變更、只作為 evidence 使用的鄰近節點——
    **untouched region frozen**，不因為被讀取為 evidence 就連帶被改動 membership。Auto-apply 沿用
    既有 high-confidence 判準（import edge + 恰好單一候選 + unlocked），**不因為「模型現在覺得更像
    另一個 scope」就自動搬移既有 membership**，沒有 deterministic 證據一律落回 review proposal。
    `locked` scope 與 human-confirmed membership 絕對保護，即使未來的 `--full` 模式也不能繞過，
    輸出只能是 candidate/proposal/diff，不能靜默改寫 canonical membership。**Large churn
    guardrail**：非預期的大量 membership 變更必須中止自動套用、要求人類審查，**本輪刻意不鎖死具體
    threshold 數值**（絕對數量 vs. changed-file-based 比例都是候選做法，留給未來實作/config 決定）。
    概念上定義 `AUTO`/`KEEP`/`REVIEW`/`BROKEN` 四種 reconciliation 結果分類供未來 CLI 輸出格式
    參考。**明確保留現有已實作行為**：Milestone 4 已實作的「`rune update` 對新增檔案 incremental
    自動併入」繼續生效，不因本輪新增 worktree/merge 討論而改變或停用；只補上一句
    multi-worktree 情境下的釐清——branch-local 的自動併入結果會隨 git merge 帶入 integration
    worktree，但 integration reconciliation 仍需在 merged state 上重新驗證這筆 membership 是否
    依然滿足既有的自動寫入條件，不是「因為之前寫過了就自動視為有效」。
146. **Scope membership 缺乏 per-membership provenance——記錄為 future/V2 schema 限制**
    （DATA_MODEL.md 新第 9 節）：現行 `ScopeMembers`（`files`/`symbols` 純清單）無法回答某條
    membership 是 human／model／auto 哪一種來源、是否 human-confirmed，這是 §4.4/§16.6 的
    reconciliation 規則依賴到、但現行 schema 沒有欄位可以精確表示的限制。記錄未來可能的
    `ScopeMembership` 概念草稿（`scope_id`/`target_type`/`target`/`source`/可能的
    locked/evidence/include-exclude 欄位）與「human exclude」的具體需求（避免使用者明確排除的
    檔案被 auto inference 每次重新建議加回），**本輪與 Milestone 7 皆不修改 `Scope`/`ScopeMembers`
    Pydantic model 或 SQLite `scope_files`/`scope_symbols` 表結構**。
147. **OpenCode adapter 的並行開發邊界**（HANDOFF.md 新增小節，M7 實作指引）：`adapters/opencode/**`
    可由另一個 agent 並行開發，範圍不得碰 `src/rune/**`、Python tests、canonical/data model 商業
    邏輯。Plugin 的職責邊界收斂為「呼叫 CLI、解析 JSON、render OpenCode 專屬 context、注入、session
    state、dedup、path extraction、host hooks、custom tool wrapper」，不得反過來去 Python core
    補業務邏輯；若 plugin 需要尚未存在的 CLI endpoint，先定義 TypeScript interface、在 plugin
    測試裡 mock，等 integration 階段才真正接線——這是既有 `RuneClient` 介面設計（見上方第三輪第 137
    條）已經在遵循的模式，本輪把它明確寫成給未來並行開發者的指引，不是新的程式碼結構。

**一致性檢查（使用者要求逐項確認）**：對照 ARCHITECTURE/DATA_MODEL/IMPLEMENTATION_PLAN/HANDOFF
現有內容，沒有發現與上述新決議矛盾之處——`adapters/opencode/` 的目錄結構、CLI discovery
（PATH/`RUNE_CLI_PATH`）、既有 incremental scope 自動併入規則、DATA_MODEL §8 的單一寫入者假設，
本輪新增內容都是既有設計的正式化或泛化，不是推翻既有決議；沒有發現需要使用者裁決的矛盾，因此本輪
未使用 AskUserQuestion。

**尚待實作（本輪只完成設計，以下皆未動程式碼，不要誤讀為已完成）**：
- Milestone 9：`rune scope reconcile`（含 `--full`）CLI 命令與 `AUTO`/`KEEP`/`REVIEW`/`BROKEN` 輸出格式，
  以及 large-churn threshold 的具體數值（第 145 條，ARCHITECTURE §4.4）。
- `ScopeMembership` per-membership provenance schema——future/V2，非近期待辦（第 146 條，
  DATA_MODEL §9）。

本輪未新增任何測試（沒有程式碼變更可測）；317 個 Python 測試、20 個 TypeScript 測試維持上一輪的
綠燈狀態不變。

## 使用者轉述的 code review，涵蓋 Milestone 6/7 core（6 條 finding，逐條重現後全部確認為真並修正）

148. **（高）`model_copy(update=...)` 繞過 Pydantic 驗證，可毒化 canonical**：`note_update`
    （`notes.py`）與 CLI 的 `proposal edit`（`main.py`，最終流入 `approve()`）都用
    `current.model_copy(update=updates)`／`payload.model_copy(update=updates)` 組出新 revision——
    Pydantic 明文記載 `model_copy` 不會重新跑 validator。親自重現：`note update --expires-at
    2026-09-07T12:00:00`（naive timestamp，沒有 tzinfo）成功把這行寫進 `notes.jsonl`，直到下一次
    `refresh_cache` 重讀該檔案（`read_jsonl` 才會 validate）才炸開，且**炸開之後 `note list`/
    `note add` 全部 raw `ValidationError`，canonical 已經永久壞掉，只能手改 JSONL**。`proposal
    edit --expires-at` 走同一個 bug class（CLI 端 `proposal.payload.model_copy(update=updates)`）。
    `--importance 9.9`/`--confidence -2.0`（超出 `Field(ge=0.0, le=1.0)`）親自重現也是同一機制，
    一樣先寫入後炸。修法：`storage/canonical.py` 新增 `validated_copy()`（`model.model_dump() |
    updates` 再丟回 `model_validate()`，強制重新跑一次每個欄位的 validator），取代 `notes.py`／
    `approve()` 內的裸 `model_copy`；`approve()` 額外在收到 `edited_payload` 時無條件用
    `validated_copy(payload, {})` 重新驗證一次——這保護的是 `approve()` 這個唯一合法寫入路徑本身，
    不管呼叫者（目前是 CLI，未來可能是 MCP 或其他呼叫端）用什麼方式組出 `edited_payload`，都無法
    繞過驗證。4 個 regression test（`test_note_update_rejects_naive_expires_at_without_poisoning_
    canonical`、`test_note_update_rejects_out_of_range_importance_and_confidence`、
    `test_approve_rejects_edited_payload_with_naive_expires_at_without_poisoning_canonical` 等）
    直接斷言拒絕後 canonical 檔案行數不變、current revision 未受影響。
149. **（中）0-byte／損毀 `memory.db` 讓 `rune status`、`bootstrap --mode soft`、所有寫入指令
    原始崩潰**：`rune search`/`check`/`bootstrap`（透過 `connect_for_read`）已經有「memory.db
    壞了就給清楚錯誤、指向 `rune rebuild-cache`」的乾淨契約，但 `core.status.compute_status`
    直接用裸 `sqlite3.connect()`（不是 `connect_for_read`），`read_current_code_index`
    （`core.update` 的 incremental diff、以及每次 propose/approve/note 寫入後
    `refresh_cache` 都會呼叫）也是裸 `connect()`。親自重現：把 `memory.db` 寫成 0-byte 後，
    `rune status` 拋 `no such table: files`；`note add` 經 `refresh_cache` ->
    `read_current_code_index` -> `rebuild_cache` 一路原始崩潰。修法分三層：
    (1) `status.py` 改用 `connect_for_read`，抓到 `CacheUnusableError` 時比照既有的「掃描失敗」
    分支，回報零值而不是崩潰；(2) `read_current_code_index` 用 `sqlite3.DatabaseError` 包住
    `connect()` 呼叫與查詢區塊，壞掉時回傳空的 `CodeIndexData`（跟「memory.db 還不存在」同一種
    處理），**刻意比 `connect_for_read` 窄**——只抓「根本打不開」，不抓「舊 schema_version」，
    因為 `core.update` 依賴這個函式能讀到舊 shape 的 cache，讓 `rebuild_cache` 自己的
    `_ensure_compatible_cache_schema` 之後才 self-heal；(3) `rebuild_cache`（寫入路徑，
    `memory.db` 「永遠可以安全丟棄重建」這個既有原則真正的執行者）自己的 `connect()` 呼叫也會被同一種
    壞檔案炸到，補上同款 `except sqlite3.DatabaseError` 分支，直接丟棄重建——跟
    `_ensure_compatible_cache_schema` 已經在做的「舊 shape 就丟棄重建」是同一招，只是提前攔截在
    連線失敗這一步。**修的過程中發現 `connect()` 本身還有一個 Windows-only 的連環 bug**：PRAGMA
    失敗時沒有關閉已經建立的 `sqlite3.Connection`，這個洩漏的檔案 handle 會讓
    `rebuild_cache` 接下來想刪除壞檔案時在 Windows 上收到 `PermissionError`（POSIX 不會，因為
    POSIX 允許刪除仍被開啟的檔案）——親自用真實 Windows 環境重現，修法是 `connect()` 的 PRAGMA
    區塊包 `try/except BaseException: conn.close(); raise`。3 個 regression test
    （`test_status_survives_a_corrupt_memory_db`、`test_rebuild_cache_self_heals_a_corrupt_
    memory_db`、`test_read_current_code_index_survives_a_corrupt_memory_db`）涵蓋讀路徑與寫路徑
    兩邊，讀路徑保持乾淨零值、寫路徑確實 self-heal 出一個可用的新 cache。
150. **（低，隨第 148 條一併修）`note_add`/`propose` 的驗證錯誤是原始 pydantic traceback**：
    `Note(...)`／`MemoryRevision(...)` 直接建構失敗時，沒有走這兩個模組其餘所有拒絕路徑共用的
    `NoteValidationError`/`ProposalValidationError` 清楚錯誤契約，CLI 因此印出完整 pydantic
    traceback 而非一行訊息。因為建構失敗發生在任何 `append_jsonl` 之前，這條本身不會毒化
    canonical，純粹是錯誤訊息品質問題，隨第 148 條的修法一起用 `try/except ValidationError` 包起來
    改拋網域例外。
151. **（中）`approve()` 崩潰後重試，若 `--by` 不同，冪等檢查會失效並附加重複 revision**：
    `_APPROVAL_CONTENT_FIELDS`（判斷「這是不是同一次重試」的欄位比對）原本包含 `approved_by`。
    親自重現：模擬第二次 canonical 寫入（`proposals.jsonl` 的 resolve）崩潰，`decisions.jsonl`
    已經成功寫入 revision 1（`approved_by=alice`），proposal 仍顯示 `pending`；用不同的 `--by bob`
    重新執行 `approve()`，因為 `approved_by` 對不上（`alice` vs `bob`），冪等檢查判定「不是同一次
    重試」，又附加了一筆內容相同的 revision 2（`approved_by=bob`）。修法：把 `approved_by` 從
    `_APPROVAL_CONTENT_FIELDS` 移除——這個欄位記錄的是「這次呼叫是誰執行的」，不是「核准了什麼
    內容」，崩潰復原的真實情境本來就可能是不同的人接手完成一個卡住的核准。修好後重試只會重用
    already-written 的 revision 1（`approved_by` 仍是原本的 `alice`），`resolved_proposal.
    resolved_by` 正確記成 `bob`（誰完成了這次收尾）。1 個 regression test 直接斷言重試後
    revision 數量不變、原始 `approved_by` 不變、新的 `resolved_by` 正確。
152. **（低，評估後判定不修，明確記錄為已知邊界）第 151 條修法讓一個既有的、更罕見的
    false-positive 邊界稍微變寬**：兩個內容逐欄位相同、record_id 相同的**獨立**新 proposal
    （例如同一個修法被複製貼上提案兩次），若兩者都被核准，第二次核准會被冪等檢查誤判成「這是第一次
    的重試」，靜默重用第一筆 revision，而不是報錯或附加第二筆——proposal 狀態顯示 `approved`，但
    `payload` 指向的是第一筆的 revision。這與程式碼註解原本宣稱的「這組比對基本上只有同一次重試才會
    全欄位相同」不完全相符。移除 `approved_by` 後，兩個獨立核准即使由不同人執行也會撞上這個
    false positive（先前至少同一個人才會撞上）。**評估後判定不修**：正確修法需要在
    `MemoryRevision` 上追蹤「這個 revision 是被哪個 `proposal_id` 核准出來的」，是 schema 變更，
    跟這輪「修一個 model_copy 驗證繞過的 bug」不對稱；且兩個 proposal 內容本來就逐欄位相同，
    無論哪種行為 canonical 裡的最終內容都一樣，只是歸屬的 proposal 記錄不同，實務影響趨近於零。
    已在 `_APPROVAL_CONTENT_FIELDS` 旁明確記錄這個 accepted false-positive，不是被忽略的 bug。
153. **（低）CLI `note update` 無法用空清單取代 scopes/files/symbols/evidence**：`main.py` 用
    `evidence or None` 決定要不要覆寫這幾個欄位——但 Typer 的 repeatable list option 預設值也是
    `[]`，導致「使用者根本沒加這個旗標」跟「使用者想清空這個清單」在 CLI 端無法區分（兩者都收到
    `[]`），而 core 層的 `note_update` 其實已經支援用空清單明確覆寫（`None` 才是「不動」）。跟每個
    選項的 help 文字（"Replaces the full … list"）矛盾。修法：仿照既有的 `--clear-expires-at`，
    新增 `--clear-evidence`/`--clear-scopes`/`--clear-files`/`--clear-symbols` 四個旗標，只有明確
    傳入時才把對應欄位改成 `[]`，否則維持先前「沒傳這個旗標就不動」的行為不變。1 個 CLI regression
    test 直接讀 `notes.jsonl` 斷言 `--clear-evidence` 後新 revision 的 `evidence` 確實是 `[]`。

新增 10 個 regression test（`tests/integration/test_memory_notes.py` 3 個、
`tests/integration/test_memory_proposals.py` 3 個、`tests/unit/test_materialize.py` 2 個、
`tests/unit/test_cli.py` 2 個），每一條都先寫重現腳本、實際看到問題發生（包括手動竄改
`memory.db` 成 0-byte、monkeypatch `append_jsonl` 模擬崩潰）才動手修。327 個 Python 測試全綠、
`ruff check` 全綠。這輪沒有觸及 canonical schema、scope model、Decision/Constraint 語意、
staleness 語意或 agent-injection 語意——全部是既有已定案行為（「validator 必須真的擋住不合法
資料」「memory.db 永遠可以安全丟棄重建」「崩潰重試不該重複寫入」「CLI 選項要能做到 help 文字說的
事」）的正確性修復，不是新設計決策，因此本輪未修改 ARCHITECTURE.md/DATA_MODEL.md。

### Milestone 7 真實 OpenCode host 驗收（完成）

154. **觀察到的 host contract 與最小修正**：在 OpenCode `1.18.29`、`@opencode-ai/plugin` `1.18.29`、
     Windows host Node `v24.3.0`、`coreloop/gpt-5.6`，temporary Rune-enabled git repo 成功載入正式 adapter。
     acceptance sentinel 被模型回覆，證明 `experimental.chat.system.transform` 的原地 system mutation 真正進入
     model context，且 `output.system` 前後維持一個 entry。真實 `read` 是
     `args.filePath=<absolute Windows path>`，原 adapter 原樣呼叫要求 repo-relative 的 `rune scope-for`，讓
     scoped activation fail open；修成安全的 repo-relative POSIX normalisation（repo 外 fail-open）後，read
     scope-a 的下一個 request 真實看到 scope-a MUST。真實修改工具是 `apply_patch` 的 `args.patchText`；補齊
     `*** Move to:`。另補 `.rune/` enablement check 與 acceptance-only safe logging/sentinel。
155. **Soft/title compatibility rule 與 fallback 結論**：cold startup 觀察到 title-generation 可能走
     transform；使用者確認只接受完整 marker `You are a title generator. You output ONLY a thread title. Nothing else.`
     判定 internal title call。命中時 Rune no-op、不 inject 或 consume soft；normal request 才 render hard/
     scoped 並一次性 consume soft。禁止 first-transform/model/timing/短字串 heuristic；未來 host 提供正式
     discriminator 優先替換。`session.prompt({noReply:true})` 真 host 測試仍產生額外 assistant turn，故不接
     production fallback。新增 title/soft、partial marker、path normalisation、Move marker、`.rune` no-op tests；
     TypeScript `npm test`（含 tsc）26 tests 全綠。
156. **真 host acceptance 完成**：使用者以 OpenRouter `nvidia/nemotron-3-super-120b-a12b:free` 在新的
     Rune fixture 重跑驗收。正式 plugin 載入後，read 啟用 scoped MUST、獨立 session 只收到 global MUST，
     證明 state isolation；`note_add`、`decision_propose`、`constraint_propose` 都寫入 canonical 並回傳
     protocol-versioned 結果；UI compact 後 global 與已啟用 scope rule 都仍重新注入。free provider 偶發
     502 overload，但 OpenCode retry 後完成，非 adapter failure。host 沒有獨立 write/edit tool，故以真實
     `apply_patch` path validation 作為該 host surface 的覆蓋；M7 可標記完成。
157. **Artifact / duplicate-plugin 根因與最小修正**：fresh `opencode run --print-logs` 證明 host 每次建立新 instance，
     project config 唯一 local file entry，沒有 Rune npm/cache entry；雖有三個既存 `opencode.exe` process，fresh run 不 reuse
     它們。source 與 `dist/plugin.js` 都含 fingerprint `directory-normalization-host-debug-20260907-a` 和
     `pluginDirectoryPath()`，而 host 實際印出該 fingerprint 及 `file:///C:/Users/maste/PycharmProjects/pmem/adapters/
     opencode/dist/plugin.js`。根因是 OpenCode 將 configured entry module 的每個 function export 視為 plugin factory：
     `plugin.js` 亦 export unit-test helpers，host 依序執行，`createRuneHooks` 把 PluginInput object 誤作 directory，造成
     `[object Object]`；`injectViaPromptFallback` 還造成 `client.session.prompt` undefined。新增只 default-export 的
     `src/host-entry.ts`，acceptance config 改指向 `dist/host-entry.js`；Node proof entry exports 只有 default，fresh host
     顯示 `directoryType=string`、normalized real path，session.created bootstrap 不再失敗。runtime executable/local SDK 都為
     `1.18.29`；`~/.config/opencode/node_modules` 有獨立 `1.18.16`，記為非根因 version skew。最後 LLM request 因 provider
     HTTP 503 中止，故 bash/scope live revalidation 仍待 provider 恢復後執行。
158. **OpenRouter bash/scope 真 host 回歸**：改用 `openrouter/z-ai/glm-5.3` 後，fresh wrapper entry 成功載入；bash
     實際修改 `scope_a.py`，`tool.execute.after` 的 `git status` 回報 repo-relative `scope_a.py`（以及預期的 Rune
     untracked files），沒有 `[object Object]`。同 session 的下一個 `system.transform` 維持一個 system entry，模型回覆
     `GLOBAL_ACCEPTANCE_MUST SCOPE_A_ACCEPTANCE_MUST`，證實 hard bootstrap、bash post-change scope activation 與 scoped
      constraint injection 均恢復。第 157 條的 HTTP 503 僅為舊 provider 可用性，不再是 M7 host blocker。

### Milestone 7 correctness and contract fixes

159. **Symbol-only scope activation/check 漏失**：`ScopeMembers` 明確支援 files 與 symbols 的多對多 membership，
semantic worker 也會由 symbol 追溯 owning file；但 `scope_for()` 與 `check()` 只查 `scope_files`。親自以
`UserService.get_user` 建立 members.symbols 唯一的 scope，確認開啟/修改 `app/services.py` 完全找不到該
scope，導致 scoped constraint 不會被注入或列入 `rune check`。修法是兩條 query 都加入
`scope_symbols JOIN symbols` 的 owning-file 路徑；新增兩個 integration regression tests。
160. **Cache health 與 scope suggest 診斷**：損毀 `memory.db` 時，`status` 雖將 count 歸零，卻仍只用
`project.json` tree hash 報 fresh；這會把「可用且最新」錯報為「只有原始碼未變」。新增 `cache_usable`，並讓
fresh 必須同時滿足可用 cache。`scope suggest` 同時從 raw SQLite connect 改用 `connect_for_read()`，維持其餘
讀取命令的可操作 cache error。新增/更新 CLI regression coverage。
161. **Proposal canonical secret safety and edit diagnostics**：Note/semantic 已在進 canonical 前依設定遮蔽 secret，
但 Decision/Constraint proposal 未遮蔽，且 edited payload 可在核准時直接寫入。`propose()` 和 `approve()`
現均接受並套用 `redact_secrets`；CLI 從 config 傳入。`proposal edit` 同時補齊其他 canonical write command
既有的 cache-refresh-failure 診斷，避免寫入成功後顯示 raw traceback。新增 proposal redaction regression test。
162. **Adapter/core `protocol_version` contract 完成**：`core.protocol.PROTOCOL_VERSION=1`；所有現有 `--json`
輸出均是頂層 object 並帶有 `protocol_version`，原本 array top-level 的 `rune search --json` 改為
`{"protocol_version": 1, "results": [...]}`。TypeScript adapter 解析 JSON 時驗證整數版本相等，不符或缺失即
 丟出明確 core/adapter mismatch error；新增 TS regression test。這是 ARCHITECTURE §6.2 已定設計的實作，未
 改變 canonical schema、scope model 或 agent-injection semantics。
163. **Scanner I/O failure 不可偽裝成刪除**：新增 `scan_files_with_issues()` 與 derived
   `IndexedFileStatus.scan_error`。暫時無法 `stat`/讀取的既有檔案保留 last-known file/symbol/edge facts，
   新檔暫不索引；恢復可讀時即使 hash 未變也強制重新解析。單元與 integration regression coverage 確認 update
   不 abort、不誤刪且可恢復。
164. **Deferred canonical write failure 丟棄 cache**：SQLite transaction 與多 canonical files 無跨檔 atomic
   transaction；使用者確認不導入 two-phase commit。`rebuild_cache()` 成功後，任一 deferred scopes/JSONL/
   project write 失敗即刪除 `memory.db` 與 sidecars，避免 readers 看見 cache 超前 canonical；下次 update 從
   source/canonical 完整重建。更新 project JSON 與 semantic append failure regressions。

## Milestone 9 — Scope Governance（開工於獨立 worktree，`main` 同時有另一個 session 在做 Milestone 8）

**範圍邊界（開工前重申，見本檔案 Milestone 9 一節與 ARCHITECTURE.md §4.4）**：只實作已定的
`rune scope reconcile` 規則，不得擴大成重新 clustering 全 repo，不得自動搬移/移除 human 或 locked
membership，large-churn threshold 由本輪決定並記錄理由（不能不設）。per-membership provenance 明確是
future/V2，不屬本輪。

165. **"Changed set" 的具體定義，本輪決定：canonical membership 與目前已 materialize 的 code index 之間的
   落差，不是重跑一次工作目錄 git diff**。ARCHITECTURE §4.4 只定了「changed set」這個概念，沒有定義
   `rune scope reconcile` 該怎麼計算它——`rune update`/`rune check`/`rune status` 各自用「掃描工作目錄、跟
   `files` 表的 content_hash 比對」算出 added/modified/deleted，但那是「這次 `rune update` 該處理哪些檔案」
   的問題，跟 `rune scope reconcile` 要回答的問題不同：後者假設呼叫時 `rune update` 已經在目標樹（通常是
   merge 後的 integration worktree，ARCHITECTURE §16.4 明講的 workflow）上跑過、`memory.db` 已經反映最新
   code index，`rune scope reconcile` 要問的是「這份已經最新的 code index，跟現有 `scopes.json` membership
   之間哪裡對不上」。具體定義：目前 code index 裡存在、但不屬於任何 scope 的檔案/symbol，視為「新增」（沿用
   Milestone 4 既有的 high-confidence import 規則）；scope membership 指向、但目前 code index 裡已經不存在
   的檔案/symbol，視為「刪除」。這個定義的好處是它**不需要**額外重跑一次 diff 邏輯（重用
   `read_current_code_index`，`core.scopes.reconcile` 因此完全不碰 `core.index.scanner`），而且天然滿足
   「untouched region frozen」：任何內容改變但 scope membership 關係沒變的檔案，根本不會被這個落差比較挑出
   來，不需要另外寫一條「排除已經 assigned 的檔案」的規則。程式碼與這段推理見
   `src/rune/core/scopes/reconcile.py` 模組 docstring。
166. **`AUTO`/`REVIEW`/`BROKEN` 的 high-confidence import 判準完全重用 Milestone 4 既有規則，不放寬也不另立
   一套**：`scopes/model.py` 的 `assign_new_files_from_imports` 原本把「哪些檔案算某 scope 的成員」與
   「單一 high-confidence import 候選」兩段邏輯內嵌在同一個函式裡，`rune update` 用它來寫入、`rune scope
   reconcile` 需要同一套判斷邏輯做唯讀分類。抽成兩個共用函式（`member_files_by_unlocked_scope`、
   `high_confidence_import_candidates`），`assign_new_files_from_imports` 改呼叫它們，`test_scopes.py`
   既有的兩個回歸測試（唯一候選才 auto-assign、ambiguous/reference-only 一律拒絕）維持全綠，證明重構沒有
   改變既有行為。
167. **「human-authoritative」的判定，在缺乏 per-membership provenance 的現行 schema 下，本輪決定用
   `scope.locked OR scope.source == human` 當代理指標，兩者任一成立就保護整個 scope 的每一筆
   membership**：DATA_MODEL §9 明講 schema 沒有辦法回答「這一筆 membership 是誰加的」，只能在「整個
   scope」這個較粗的粒度做判斷。單獨用 `locked` 不夠：`rune scope unlock` 之後，一個原本人類建立
   （`source=human`，`create_scope` 預設 `locked=True`）的 scope 會變成 unlocked，但它的 membership
   本質上仍然是人類決定的，不能因為使用者手動解鎖（通常是為了允許 Milestone 4 既有的 incremental
   auto-assign *新增*成員）就連帶讓它已有的 membership 失去「刪除時必須進 BROKEN 而非靜默清除」的保護。
   反過來，單獨用 `source == human` 也不夠：`locked` 是使用者可以獨立切換的顯式訊號（`rune scope
   lock`/`unlock`），一個 model/auto 來源但被使用者手動 lock 的 scope，理當也要受保護。兩者任一即保護，
   是這兩個訊號各自代表「人類已經對這個 scope 的 membership 做出過判斷」的合取，不是交集——用交集
   （兩者都要）會讓「human 建立但已解鎖」的 scope 失去保護，明顯不對。已用專門的回歸測試鎖住這個決定
   （`test_deleted_target_in_unlocked_human_scope_is_still_broken`），並用刻意注入的錯誤實作
   （`_is_protected` 永遠回傳 `False`）驗證這條測試（與另外三條）真的會抓到這個回歸，revert 後全部轉綠才算
   驗證通過。
168. **Large-churn threshold：本輪決定為絕對數量 20（`config.scopes.reconcile_large_churn_threshold`），比較
   `must_count_warn_threshold=30`（Milestone 8，ARCHITECTURE §7.7）的推理方式，但選了更保守的數字，理由
   記錄如下**：`must_count_warn_threshold` 保護的是「每次 LLM 呼叫都要重複讀一次的 global MUST 規則清單」，
   後果是 token 成本持續累積、閱讀負擔變高，是一個持續性、可逆的成本（超過門檻只是印警告，不阻止任何寫入）。
   `rune scope reconcile` 的 AUTO 寫入是一次性、結構性地改變 `scopes.json`——ARCHITECTURE §4.4 開頭明講
   「Scope 是穩定、漸進累積的 project knowledge，不是可以隨時重新計算的衍生資料」，且 V1 的預期使用情境是
   「新專案從一開始就用 rune，scope 從小數量逐步累積」（§4.4 開場白），不是「丟一個大 repo 一次分完」。在這個
   預期使用情境下，一次 reconcile 產生二十個以上高信心新增，已經是不尋常的批量事件（例如一次合併帶進大量
   之前沒被任何 scope 認領的檔案），即使每一筆個別而言都符合 high-confidence 規則，仍然值得暫停讓人類看過
   一次，而不是逐條自動接受——這是「個別判準正確」與「批量本身有風險」兩件事，guardrail 管的是後者。因此
   選了比 `must_count_warn_threshold` 更小、更保守的絕對數字：20。之所以是絕對數量而非
   changed-set-relative 比例（ARCHITECTURE §4.4 point 5 原文兩者都列為候選做法），是因為「changed set 本身
   多大算合理」在 V1 沒有真實資料可以校準比例分母該怎麼定義（用「這次 reconcile 檢查了幾個檔案」當分母，
   還是「目前 scopes.json 總 membership 數」，兩者語意不同、都缺乏依據），而 AUTO 寫入次數本身是使用者能
   直接理解、之後能憑真實使用經驗調整的絕對量——這與 `must_count_warn_threshold=30` 選型時「沒有真實資料，
   先給一個可推理、非隨意猜測的保守值，留給後續真實使用觀察再調」的精神一致，不是重新發明一套判斷方法。
   `>` 而非 `>=`（剛好等於門檻值仍允許自動套用，見 `test_churn_threshold_boundary_is_inclusive`），因為門檻
   本身已經是「多少算太多」的判斷，卡在門檻上不該被當成超標。用刻意注入的錯誤實作
   （`suspicious_churn` 永遠為 `False`）驗證 `test_large_churn_blocks_every_auto_write` 真的會抓到「guardrail
   被關掉」這個回歸。
169. **`--full` 模式：本輪決定它擴大分類範圍（含 KEEP 逐筆列出，供完整 audit）但永遠不寫入，即使遇到
   AUTO-eligible 的高信心單一候選也一樣**：ARCHITECTURE §4.4 point 4 的原文「Full reconciliation…且輸出只能
   是 candidate/proposal/diff，不得靜默改寫 canonical membership」沒有明講 `--full` 到底擴大了什麼分類範圍
   （因為當時只是預留 CLI flag、未實作）。本輪決定：預設模式的 entries 只列出真正需要人類注意的項目
   （AUTO/REVIEW/BROKEN），KEEP 不逐筆列出（只計數），避免在大 repo 上每次 reconcile 都印出成千上萬行「沒事」
   的訊息；`--full` 才逐筆列出 KEEP，用於「我想確認每一筆現有 membership 現在的狀態」這種主動稽核情境。
   這個決定的直接後果是：預設模式與 `--full` 模式的差異純粹是「輸出詳盡度 + 是否允許寫入」兩個維度的組合，
   不是「檢查範圍」的差異——兩者用的都是同一個 canonical-vs-index 落差（見第 165 條），沒有為 `--full`
   另外實作一套「重新掃描/重新 cluster 整個 repo」的邏輯，這正是本輪範圍邊界明確禁止的事。
   `test_full_mode_never_applies_even_high_confidence_auto`、CLI 層的
   `test_scope_reconcile_full_never_writes` 鎖住這個決定。
170. **明確決定不做、且記錄理由的一項（ARCHITECTURE §16.6 收尾段落暗示、但與本輪範圍邊界衝突）**：
   §16.6 最後一段提到，branch-local 時期已經自動併入的 membership，merge 後若因為 import graph 多了其他
   worktree 帶進的候選而不再滿足「唯一候選」，理論上「這筆 membership 就不再自動視為有效，需要落回人類
   審查」——字面上這意味著 reconciliation 應該重新驗證**已經是 scope 成員**的檔案是否仍然唯一滿足
   high-confidence 規則，不是只處理「目前完全沒有 scope 的檔案」。本輪**沒有實作這個重新驗證**：要做到這件
   事，必須對每一筆現有 membership（而不只是未分配檔案）重新跑一次「它現在還唯一滿足高信心規則嗎」的檢查，
   等於對整個 `scopes.json` 的既有內容做一次全面重新分類——這正是本輪範圍邊界明講「不得擴大成重新
   clustering 全 repo」與「untouched region frozen」兩條要擋住的事，而且現行 schema（DATA_MODEL §9）根本
   無法區分一筆既有 membership 是「當初被 Milestone 4 incremental auto-assign 自動寫入、理論上可以重新驗證」
   還是「人類透過 `rune scope edit --add-file` 手動加的、不該被重新驗證」——沒有這個區分，對「所有現有
   membership」一視同仁地重新驗證，實質上就是把每一筆 unlocked/非 human-source scope 的既有成員都變成
   「每次 reconcile 都可能被踢出去重新分類」的狀態，這已經違反 §4.4 開頭「Scope 是穩定、漸進累積的 project
   knowledge」的核心原則。這是本輪明確判斷「架構文件字面上暗示、但與同一份文件更上位的原則衝突」的情況，
   記錄下來但不動手，留給有 per-membership provenance schema（DATA_MODEL §9 的 V2 候選）之後再處理，不在
   `rune scope reconcile` 現有的 read_current_code_index-based 落差比較中悄悄擴大範圍。

   **（第 171 條撤回本條的「不做」結論，見下方新增段落——複查後發現第 170 條的推理有漏洞：不需要
   per-membership provenance schema 也能做到範圍精確的重新驗證，用 git 本身讀 `scopes.json` 在
   merge 前後的差集即可界定「這次 merge 引入了哪些 membership」，見下方「Merge-affected 重新驗證」
   一輪的完整說明。）**

**交付項目完成情況**：`rune scope reconcile` CLI（`scope_app` 底下的 `reconcile` 子命令）與明確 opt-in 的
`--full`；`AUTO`/`KEEP`/`REVIEW`/`BROKEN` 的穩定 `--json` 輸出（`protocol_version`、
`auto_count`/`review_count`/`keep_count`/`broken_count`、`suspicious_churn`、逐筆 `entries`，每筆帶
`scope_id`/`target_type`/`target`/`classification`/`reason`/`applied`/`candidate_scope_ids`，比照
`search --json`/`scope-for --json` 既有的頂層 object + `protocol_version` 慣例）；deterministic
candidate/review 產生（`core.scopes.reconcile.reconcile()`，純函式、不寫入，寫入交由 CLI 呼叫
`save_scopes`，沿用 `core.update` 既有的「先算好再交易性寫入」模式）；沒有新增任何模型直接改寫 membership
的路徑（`reconcile()` 本身不呼叫任何 LLM/model provider）。merge/integration worktree 使用流程與 integration
reconciliation 驗收流程記錄在 ARCHITECTURE.md §4.4 新增小節（「Reconciliation CLI 與 merge/integration
worktree 使用流程」）與 §16.4 的既有段落互相參照。

**測試**：新增 13 個 `tests/unit/test_scope_reconcile.py`（AUTO/REVIEW/BROKEN/KEEP 各分類、locked 與
`source=human`-but-unlocked 兩種保護訊號分開測試、large-churn guardrail 含邊界值測試、`--full` 永不寫入、
symbol-level membership、跨 `PYTHONHASHSEED`/process 的 determinism 回歸測試，比照
`test_references.py` 既有寫法）與 4 個 `tests/unit/test_cli.py` 端到端 CLI 測試（`--json` round-trip、
`--full` 唯讀、BROKEN 保留 canonical、cache 不存在時的乾淨錯誤）。所有新測試都用刻意注入的錯誤實作
（`_is_protected` 永遠 `False`、`suspicious_churn` 永遠 `False`）手動驗證過會抓到對應的回歸，不是只信任
第一次寫對就通過。347 → 351 個 Python 測試全綠（本輪淨新增 17 個），`ruff check` 全綠。

**已知限制（誠實記錄，非本輪疏漏）**：
- ~~第 170 條記錄的「既有 membership 在 merge 後重新驗證」未實作，需要 per-membership provenance
  （future/V2）~~ **已在後續一輪實作為 `rune scope reconcile --since <ref>`，見下方「Merge-affected
  重新驗證」段落——第 170 條「需要 provenance schema」的判斷已撤回。**
- 沒有評估 zero-evidence 未分配檔案被列為 REVIEW 在大型既有 repo 上首次執行 `rune scope reconcile` 時是否
  會產生大量雜訊（例如測試檔案、設定檔天生就不會有 import edge 指向任何 scope）——V1 定位仍是「新專案從
  頭用 rune」，尚未有真實中大型 repo 的第一次執行經驗回饋，記錄供未來調整（例如替 REVIEW 加一個
  `no_evidence` 子分類，讓使用者能選擇性地忽略/批量 dismiss，而不是每篇都要人工看過）。

**Merge 前 code review 一輪（另一個 session 代使用者複查，兩處修正、一處記錄不動手）**：

- **（中，已修）CLI 訊息順序**：`rune scope reconcile` 的人類可讀輸出原本
  `if suspicious_churn: ... elif applied: ... elif full: ...` 依序判斷，導致 `--full` 搭配剛好超過
  churn threshold 的樹時，印出「Suspicious churn... No changes were written -- review required」——
  這句話本身沒錯（確實沒寫入），但把「沒寫入」的原因歸給 churn guardrail，而實際上 `--full` 本來就
  絕對不寫入、跟 churn 完全無關。已改為先判斷 `full`，`--full` 一律印「N AUTO candidate(s) found
  (--full, output-only)」，不再被 `suspicious_churn` 的訊息搶在前面。
- **（低，已修）模組 docstring 用詞不精確**：`reconcile.py` 開頭原本寫「a file/symbol present in the
  current index but a member of no scope is exactly the 'added file' case... reconcile applies that
  same rule」，但程式碼只對「未分配的檔案」跑 AUTO/REVIEW 分類迴圈，從未對「未分配的 symbol」跑過
  ——三個獨立 review 角度都各自發現這個字面矛盾。重新確認過 Milestone 4 原始的 incremental
  auto-assignment 規則本來就只認 file-level import edge，沒有 symbol-level 的對應高信心規則，所以
  程式碼行為（AUTO/REVIEW-for-new 只認檔案）才是對的；已改的是 docstring 措辭，讓它精確描述現況，
  不再暗示 symbol 也有相同的「新增即分類」路徑。**是否要替「從未被任何 scope 以 symbol 形式收錄」的
  symbol 也加一個 REVIEW 分類，本輪判斷為需要使用者確認的範圍擴張（可能在大型既有 repo 上對幾乎每個
  未特別用 symbol 層級收錄的函式都跳出 REVIEW，雜訊風險比照上一條「zero-evidence 未分配檔案」的疑慮，
  但量級可能大得多——大多數 scope 只用 `members.files`，`members.symbols` 是特例用法）**，記錄下來但
  不動手，需要下一輪跟使用者確認後才實作。既有 membership 被刪除的方向（BROKEN/REVIEW-for-removal）
  對檔案與 symbol 本來就對稱處理，不受這條影響。
- **（低，記錄不動手）`reconcile()` 對未分配檔案的迴圈是 O(未分配檔案數 × edges 總數)**（`high_
  confidence_import_candidates` 每次呼叫都重新掃一次 `code_index.edges`），在既有大型 repo 第一次執行
  `rune scope reconcile` 時可能明顯變慢；`assign_new_files_from_imports`（`rune update` 既有的
  incremental 路徑）不受影響，因為它只在小得多的「這次新增的檔案」delta 上跑。這是效能問題，不是正確性
  問題，且與上一條「zero-evidence 雜訊」問題同源（首次在大型既有 repo 執行的體驗尚未有真實回饋）——記錄
  下來，留待有真實大型 repo 執行數據後再決定是否要把 `edges` 依 `source_file` 預先分組成 dict。

351 個測試維持全綠（這輪只改訊息順序與註解文字，不影響任何既有測試的斷言），`ruff check` 全綠。

**Merge-affected 重新驗證（撤回第 170 條，實作 ARCHITECTURE.md §16.6）**：

使用者直接引用 §16.6 的規範文字追問「M9 這部分做得如何」，逐字比對後發現第 170 條的「不做」判斷站不住
腳。§16.6 要求的其實是：

> After merge, Rune MUST revalidate only merge-affected auto-inferred scope memberships against the
> merged repository state. Human-confirmed or locked memberships are never automatically reassigned;
> conflicting or unverifiable cases become REVIEW/BROKEN rather than being silently changed.

這比第 170 條理解的「對所有現有 membership 重新分類，等於重新 clustering 全 repo」窄得多——關鍵字是
**merge-affected**（只限這次 merge 引入的，不是全部）跟 **auto-inferred**（只限非人類確認的）。第
170 條當時的推理卡在「schema 沒有 per-membership provenance，沒辦法只挑 auto-inferred 的出來」，但
這個推理忽略了一個現成的機制：`.rune/scopes.json` 本身就是 git 追蹤的檔案。「這次 merge 引入了哪些
membership」不需要 schema 層的 provenance 就能精確界定——直接比較 `scopes.json` 在呼叫者指定的
`since`（merge 前的某個 ref，通常是 merge base）當時的內容，跟現在 canonical 內容的差集即可：人類
另外用 `rune scope edit`/`create` 加的東西是獨立一次 commit，天然不會落在「`since` 到 `HEAD` 之間」
這個差集裡，完全不需要另外分辨「這筆 membership 是不是自動寫的」。

**已實作為 `rune scope reconcile --since <ref>`**（`core.scopes.reconcile._merge_affected_entries`/
`_scopes_json_at_ref`）：
- 用 `git show <since>:.rune/scopes.json`（不是 working-tree diff）讀出 `since` 當時的
  `scopes.json` 內容；`since` 不是合法 git ref 時丟出新的 `InvalidRefError`（CLI 轉成乾淨的
  exit code 1，不是原始 traceback）。
- 只處理現在是 unlocked、`source != human` scope 成員、但 `since` 當時不是的 file membership——
  沿用既有的 `_is_protected` 保護代理指標，跟 BROKEN 分類共用同一套判斷，不是另外發明一套。
- 對每筆這樣的 membership，用現在（merged 後）的 import graph 重跑既有的
  `high_confidence_import_candidates`；仍唯一命中同一個 scope 就不產生任何 entry；命中零個、多個、
  或命中別的單一 scope，一律 `REVIEW`（附上目前候選 scope id 與說明文字，標明是 merge-affected）。
  **沒有新的 AUTO 路徑**——這些 membership 已經存在，唯一的問題只是「還要不要相信它」，不是「要不要
  寫入」，`reconcile()` 既有的 AUTO-only 寫入路徑完全不受這段邏輯影響。
- 未帶 `--since` 時完全不跑這段邏輯（純 opt-in，預設行為不變）：reconcile 本身沒辦法在事後自動判斷
  「這是不是一次 merge」（`git merge` 完成後 `.git/MERGE_HEAD` 就消失了），猜錯 base ref 會漏掉真正
  的 case，或誤把不相關的歷史當成 merge 影響，所以要求呼叫者明確給 `since`。
- 只處理 file membership，不處理 symbol membership——理由與第 171 條之前那輪（docstring 精確化）
  記錄的原因相同：symbol-level 從來沒有對應的高信心自動推斷規則，沒有「這筆 symbol membership曾經是
  自動寫的」這種情境需要重新驗證。

ARCHITECTURE.md §16.6 新增對應段落、§17 第 2 點更新為「已實作」，撤回「需要 provenance schema 才能做」
的舊結論。新增 7 個回歸測試：`tests/unit/test_scope_reconcile.py` 5 個（merge-affected 證據改變後
變成 REVIEW、證據仍然吻合時不產生 entry、protected scope 被跳過、不帶 `since` 時完全不跑這段邏輯的
baseline、非法 ref 丟 `InvalidRefError`）+ `tests/unit/test_cli.py` 2 個端到端測試（真實檔案／真實
git commit／真實 `rune update` 掃描整個流程走一遍、非法 ref 的 CLI 錯誤路徑）。403 個測試全綠
（396 → 403），`ruff check` 全綠。
