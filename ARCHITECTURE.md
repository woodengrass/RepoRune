# RepoRune（rune）— 架構設計

> **RepoRune**（日常稱呼／CLI 指令：`rune`）= *Repository Understanding & Navigation Engine*。
> 本文件其餘部分一律使用 `rune` 指稱這個工具本身（CLI、Python 套件、目錄名稱 `.rune/` 皆同名），
> `RepoRune` 僅在需要完整品牌名稱的場合使用（例如文件標題、對外介紹）。

狀態：**已確認（第五輪修訂）**（V1 設計，經 2026-09-06 討論確認全部開放問題）。第四輪根據對照
OpenCode 官方 plugin 文件的結果具體化 Milestone 7 設計、補上 ParserAdapter 介面契約、
import/reference 信任層級原則、semantic worker fallback policy、SQLite 併發策略，並將 scope
clustering 品質明確定位為「留待真實 repo 實驗調整」而非架構層需要鎖死的正確性需求。**第五輪新增
Global Code Standards / Hard Policy Injection 語意（新第 7 節）**：Global MUST Constraint 的定義、
Hard/Soft Bootstrap 的區分與各自的注入時機（`session.created`/`session.compacted`）、token
budget 的 overflow-not-truncate 規則——這是 agent-injection semantics 的正式組成部分，不是附加
功能，所有後續章節編號因此從第 7 節起整體後移一位（原第 7-12 節現為第 8-13 節）。本文件與
`DATA_MODEL.md`、`IMPLEMENTATION_PLAN.md` 共同構成 Milestone 1 的實作基準。任何會改變 canonical
schema、scope model、Decision/Constraint 語意、staleness 語意或 agent-injection 語意的後續變更，
仍必須重新提案並取得確認後才能實作。

## 1. 目的與非目標

RepoRune（`rune`）是一個伴隨 coding agent（V1 優先支援 OpenCode CLI）運作的 repository intelligence
系統，提供：

- 決定性、可低成本重建的程式碼結構（檔案、symbol、import、best-effort reference）
- 持久化的語意理解（scope summary），只在其依據的來源改變時才重新產生
- 持久化的治理紀錄（Decision、Constraint），須經人類核准才具權威性
- 持久化但會自動過期的工作筆記（pitfall、workaround、investigation result）
- 一套投遞機制，把「相關」的子集主動推入 agent session，而不需要 agent 自己想到要去查

rune **不是**：Git、IDE/LSP 的替代品，不是向量資料庫，不是知識圖譜資料庫，不是團隊伺服器，也不會自主做出
權威性決策。它從不修改原始碼，也從不在沒有人類核准的情況下讓 Decision/Constraint 生效。

## 2. 本專案（工具本身）的目錄結構

```text
rune/
├─ pyproject.toml
├─ ARCHITECTURE.md
├─ DATA_MODEL.md
├─ IMPLEMENTATION_PLAN.md
├─ src/
│  └─ rune/
│     ├─ core/                 # agent-agnostic 的核心邏輯（唯一含「真正邏輯」的地方）
│     │  ├─ config.py          # TOML 設定讀取與驗證
│     │  ├─ project.py         # 專案偵測、.rune/ 初始化、init 的 refuse/--force 語意
│     │  ├─ hashing.py         # content hash、git blob hash、working-tree fingerprint
│     │  ├─ index/
│     │  │  ├─ scanner.py      # include/exclude walk、變更偵測
│     │  │  ├─ treesitter.py   # 各語言 parser、symbol 擷取
│     │  │  ├─ imports.py      # 各語言 import graph 擷取
│     │  │  └─ references.py   # best-effort reference 解析
│     │  ├─ scopes/
│     │  │  ├─ model.py        # Scope pydantic model、membership 規則
│     │  │  ├─ heuristics.py   # 路徑啟發式候選 scope
│     │  │  └─ clustering.py   # graph-assisted 候選建議（僅建議，永不具權威性）
│     │  ├─ semantic/
│     │  │  ├─ provider.py     # ModelProvider protocol + OpenRouter/OpenAI/相容實作
│     │  │  ├─ worker.py       # scope summary 產生、驗證、失敗隔離、增量重生成
│     │  │  ├─ validation.py   # schema + reference + symbol 驗證（strip vs reject）
│     │  │  └─ redaction.py    # 寫入 canonical 前的機密遮蔽
│     │  ├─ memory/
│     │  │  ├─ decisions.py    # Decision revision 生命週期、proposal/approval
│     │  │  ├─ constraints.py  # Constraint revision 生命週期
│     │  │  ├─ notes.py        # Note 生命週期、TTL/staleness 規則
│     │  │  └─ staleness.py    # Decision/Constraint/Note 共用的 staleness 規則（非 semantic 的 hash 規則）
│     │  ├─ storage/
│     │  │  ├─ canonical.py    # JSON/JSONL/TOML canonical 檔案的 atomic 讀寫
│     │  │  ├─ schema_versions.py
│     │  │  └─ sqlite/
│     │  │     ├─ schema.sql
│     │  │     ├─ materialize.py  # canonical -> SQLite 重建（不呼叫 LLM）
│     │  │     └─ queries.py
│     │  ├─ retrieval/
│     │  │  ├─ search.py       # FTS5 搜尋、排序
│     │  │  └─ context.py      # bootstrap context、scope activation context 組裝
│     │  └─ update.py          # `rune update` 交易式流程協調
│     ├─ cli/
│     │  └─ main.py            # Typer app：init/update/status/search/rebuild-cache/check/doctor（含 --json）
│     └─ mcp/
│        └─ server.py          # Milestone 8：MCP tool 暴露，薄包裝
├─ tests/
│  ├─ unit/
│  └─ integration/
│     └─ fixtures/
│        ├─ ts-simple/
│        └─ python-simple/
└─ adapters/
   └─ opencode/                # 獨立 TypeScript 套件，僅作 adapter
      ├─ package.json
      └─ src/plugin.ts
```

套件邊界規則：**`rune.cli`、`rune.mcp`、`adapters/opencode` 一律不得含商業邏輯**，只能呼叫
`rune.core`（OpenCode adapter 透過 CLI 的 `--json` 介面，見第 6 節）。這是讓未來 Codex／Claude Code 整合
只需「新增」而非「重寫」的關鍵。

## 3. 資料流（對應規格 §5）

```text
Repository（git working tree）
        │
        ▼
Change Detector（git diff + content hash + working-tree fingerprint，rune.core.index.scanner）
        │
        ▼
Code Indexer（Tree-sitter，rune.core.index.treesitter/imports）
        │
        ▼
Code Graph（files、symbols、edges — SQLite 衍生資料）
        │
        ▼
Scope System（rune.core.scopes — 手動 + 啟發式 + graph-assisted 候選）
        │
   stale 偵測（rune.core.memory.staleness / rune.core.semantic.worker，兩套不同規則見第 4.5、4.6 節）
        │
        ▼
Semantic Worker（rune.core.semantic — 便宜模型、structured output、經驗證）
        │
        ▼
Canonical Memory（.rune/ 下的 JSON/JSONL，可進 git）
        │
        ▼
SQLite Cache（.rune/cache/memory.db — FTS5，完全衍生，gitignore）
        │
   ┌────┴─────┐
   ▼          ▼
OpenCode     MCP
adapter      server
```

兩個貫穿所有模組邊界的不變量：

1. **Canonical 文字檔是 memory 唯一的 source of truth。** SQLite 永遠可以透過 `rune rebuild-cache` 從
   canonical 重建，且過程零 LLM 呼叫。兩者不一致時，以 canonical 為準。
2. **原始碼是程式碼事實唯一的 source of truth。** rune 從不「記住」某函式存在——每次 `rune update` 都重新
   從 repo 推導。Semantic summary 與 Note 可以「描述」程式碼事實，但依附在已過期／已刪除來源 hash 上的
   summary 絕不會被當作目前的事實直接呈現。

## 4. 元件職責

### 4.1 Change Detector（`core.index.scanner`）
依 config 的 include/exclude glob 走訪檔案，將每個檔案的 content hash（可用時含 git blob hash）與上次索引狀態
（`project.json` 與 SQLite `files` 表）比對，產出 added/modified/deleted 變更集。同時計算並更新
「working-tree fingerprint」（見第 10 節），因為 git HEAD 未變不代表 working tree 未變。這是唯一能判定
「什麼改變了」的模組。

### 4.2 Code Indexer（`core.index.treesitter`、`core.index.imports`、`core.index.references`）
對每個變更檔案：以對應語言的 Tree-sitter grammar 解析，擷取 symbol（function/class/method/interface/type/
variable/constant/component）、擷取 import、best-effort 擷取 reference。單一檔案解析失敗不會讓整次 update
失敗，但 `status=parse_error` 具體代表什麼**分兩種情況**（本輪修正先前「標記後跳過」這句過於簡化、與
Milestone 2/3 實際行為不符的敘述）：

1. **真的丟例外**（檔案讀不到、或 tree-sitter 的 `MISSING` 節點讓某個必要欄位變成 `None`
   導致我方 walker 出錯）：該檔案標記 `parse_error`，symbol/edge 一律為空——這才是真正的「跳過」。
2. **`adapter.has_syntax_error(source)` 為真，但擷取本身沒有丟例外**：tree-sitter 對語法錯誤採
   error-recovery（回傳含 ERROR 節點的部分樹，不丟例外），此時擷取到的 symbol 可能是從錯誤區域附近
   算出來的、不完全可信，但仍然**保留**（best effort，比照 unresolved import 的處理原則），只是把
   檔案標記 `parse_error` 讓這個不確定性對下游可見，而不是靜默當作 `ok`。

不論哪種情況都不視為致命錯誤（與 semantic 失敗隔離的精神一致，規格 §62）。另外，`status=parse_error`
會**持續存在**直到該檔案真的被重新解析：一個 content_hash 沒變的檔案（`rune update` 認定為
「unchanged」、不會再丟進 tree-sitter）必須沿用它上一次的真實狀態，不能因為「這次沒有重新解析」就
預設它是 `ok`——否則一個曾經解析失敗的檔案，只要之後沒有人去動它，反而會在下一次無修改的
`rune update` 之後看起來像是「已修好」。

Symbol rename 在 V1 視為「刪除舊 symbol + 建立新 symbol」（`symbol_id` 由 `path + qualified_name + kind`
推導，rename 自然產生新 ID），不做 fuzzy rename 追蹤——刻意選擇，避免引入誤配對風險。

**`ParserAdapter` 介面（本輪新增，解決「qualified_name 演算法要不要跨語言統一」的問題）**：每個語言的
解析邏輯實作同一組介面，但介面**只規定輸出形狀，不規定演算法**——每個語言自行決定怎麼算
`qualified_name`（例如 TS 的 namespace/computed property、Python 的 nested class/closure），不要求
跨語言一致：

```python
class ParserAdapter(Protocol):
    def extract_symbols(self, path: str, source: bytes) -> list[Symbol]: ...
    def extract_imports(self, path: str, source: bytes) -> list[Edge]: ...
    def qualified_name(self, node) -> str: ...   # 語言專屬邏輯，允許 Python/TS/JS 各自實作
```

Milestone 2 交付 `PythonParserAdapter`、`TypeScriptParserAdapter`、`JavaScriptParserAdapter` 三個
具體實作；架構層不對 `qualified_name` 的計算方式做任何跨語言保證，只保證輸出符合 `Symbol` model。

### 4.3 Code Graph
非獨立模組，即 `files`／`symbols`／`edges` 三張 SQLite 表，由 indexer 填入、由 retrieval 查詢。

**信任層級原則（本輪新增，工程風險最高的區塊，需要明確降低下游依賴的敏感度）**：`imports` 是
**high confidence**（來自語言的明確 import/require 語法，解析失敗率低），`references`/`calls`/
`extends`/`implements` 是 **best-effort**（TS 的 `paths`/`baseUrl`/barrel export、monorepo 套件
邊界、Python 的 relative import/namespace package/`PYTHONPATH` 都有大量無法完美解析的 edge
case）。所有 edge 都帶 confidence 分數（規格 §7）。**Scope／Constraint 系統不得把 reference graph
當唯一依據**——即使 reference 解析大範圍失準或缺漏，rune 仍必須能正常運作（scope membership 主要
靠 `scope.members` 這份 canonical 清單與 import graph，reference graph 只是輔助查詢
「誰引用了 X」，不是任何治理邏輯的必要輸入）。

### 4.4 Scope System（`core.scopes`）
負責 Scope model 與 membership（檔案層級與 symbol 層級，多對多，規格 §9–§11）。候選 scope 的三種來源：
手動（人類，一律 `locked` 直到人類解鎖）、路徑啟發式、graph-assisted clustering 建議。Clustering 永遠只
「建議」，絕不重寫 `locked=true` scope 的 membership 或 ID。新檔案優先走 incremental 分派（併入既有 scope），
不會自動觸發全庫重新分群（規格 §13）。

**Clustering 品質定位為「建議實驗」而非「正確性需求」（本輪明確定位，降低過度設計風險）**：V1 主要
使用情境是「新專案從一開始就用 rune、scope 從小數量逐步累積」，不是「丟一個 5000 檔案的陌生 repo
要求自動產出完美架構圖」。因此 clustering 演算法（NetworkX connected components / 簡單 community
detection）的參數不在架構/設計階段鎖死，而是留到 Milestone 4 用真實中型 repo 實測、觀察 candidate
scope 是否「像人會畫的架構邊界」，再調整——這是刻意不在設計文件中過度規格化的部分，因為它本質上是
經驗性問題，不是邏輯正確性問題。

### 4.5 Semantic Worker（`core.semantic`）
針對過期／缺漏的 scope summary，用該 scope 的成員檔案／symbol 組 prompt，呼叫設定的 `ModelProvider` 取得
structured `ScopeSummary`，經三層驗證（schema、路徑存在性、symbol 存在性）後才允許寫入 canonical：

- **Strip**：summary 中引用到不存在的 file/symbol 的「條目」被移除並記錄警告，不影響其餘欄位。
- **Reject**：只有當核心欄位（如 `purpose`）本身無法通過 schema 驗證時，才整份 generation 被拒絕。

失敗或被拒絕的 generation 保留舊 summary、`status=stale`、記錄 `last_error`，絕不清空或損毀既有 summary
（規格 §62）。

**Provider 視為不可靠外部依賴，fallback policy 明定為有限步驟（本輪新增）**：

```text
call primary model
  → schema/reference 驗證失敗
    → 1 次 retry（附加更嚴格的 repair prompt，指出上次失敗的具體欄位）
      → 仍失敗
        → fallback model（config 另外指定，例如更貴但更穩的模型）
          → 仍失敗
            → 保留舊 summary，status=stale，記錄 last_error，不再重試
```

不做無限 retry。每次呼叫記錄以下 metrics（寫入 SQLite 或獨立的 metrics 表，供 `rune update` 輸出
與後續比較不同 provider/model 的實際表現，而非憑印象判斷）：`schema_success_rate`、
`reference_strip_rate`（有多少比例的引用被 §4.5 strip 規則移除）、`fallback_rate`、
`provider_error_rate`、`cost`、`latency`。

Semantic staleness 判斷完全基於 **member 檔案的 content hash**（見 DATA_MODEL §2.4 的 `source_hash` /
`source_files`），與下方 4.6 節 Decision/Constraint 的 staleness 規則是兩套完全獨立的機制，不可合併。

### 4.6 Memory（`core.memory`）— Decision / Constraint / Note 的生命週期

**Current 與 Visible 是兩個不同概念（本輪修正的核心 bug，見 DATA_MODEL §1、§3）**：current revision
永遠是 `max(revision)`，與其 `status` 完全無關；是否出現在預設 retrieval（以及以何種形式）則完全由
current revision 的 `status` 另外決定。先前版本誤將兩者合併（「current = `{active,
review_required}` 中最大者」），會導致 `rev1=active, rev2=inactive` 時系統錯誤地繼續把 rev1 當作
current 呈現給 agent——這正是本輪要修正的根本性 bug。正確流程：**先算 current，再依 status 判斷是否
visible**。

**Decision/Constraint 的 staleness 絕不套用 semantic summary 那種「來源 hash 一變就 stale」規則。**
Decision 代表的是「選擇」本身（例如「用 PostgreSQL 而不是 Redis」），實作細節的變動不代表這個選擇
失效。規則如下：

| 事件 | Decision / Constraint 反應（附加新 revision） |
|------|------------------------------------------------|
| 引用的檔案內容改變（檔案仍存在） | **不附加新 revision，current 不變**——內容修改不代表決策失效 |
| 引用的 file/symbol 被刪除（scope 仍存在） | 附加新 revision，`status=review_required`：不隱藏，明確標記「需要人類覆核」 |
| 引用的整個 scope 消失 | 附加新 revision，`status=orphaned`，預設 retrieval 排除 |
| 人類／agent 提出新內容並經核准 | 附加新 revision，`status=active`，舊 revision 保留供 audit |
| 人類明確停用 | 附加新 revision，`status=inactive`，預設 retrieval 排除 |

`Constraint` 的 `persistence_mode`（`persistent`/`scope_bound`/`source_bound`/`temporary`）決定上表
「引用內容改變」一行是否真的觸發變動——關鍵是 `source_bound`/`scope_bound`/`temporary` 都需要在
**核准當下**對來源做一次 snapshot（`source_hashes`/`scope_hashes`/`expires_at`，見 DATA_MODEL §2.5），
之後才能拿「現在的狀態」與「核准時的 snapshot」比較，判斷是否偏離；沒有 snapshot 就無法實作
staleness 判斷，只知道「files 欄位列了這個路徑」，不知道核准當下的內容是什麼樣子。細節見 DATA_MODEL §6。

- **Decision**：proposal -> 人類核准 -> 附加 `active` revision。只有人類核准的動作才能建立
  「新增權威內容」的 revision；狀態轉換（`review_required`/`orphaned`）由 `core.memory.staleness`
  系統自動附加，不需要每次都經過人類核准迴圈。
- **Constraint**：同樣的 proposal/approval 流程，`persistence_mode` + snapshot 決定其對來源變動的
  敏感度。
- **Note**：agent 可直接寫入（無需核准關卡），本輪起 Note 也採 revision 機制（`id` + `revision`，
  current = `max(revision)`），依 category 有 TTL/source-hash-snapshot staleness 規則，寫入前強制跑
  機密遮蔽。**`stale` 的 Note 不會從預設 retrieval 消失**——它仍會被搜尋到，但明確標記 `[STALE]`，
  因為即使來源已變，過去的警告（例如「這裡曾有 race condition」）通常仍有價值。只有 `expired`／
  `orphaned` 預設排除；`archived` 僅在 history/audit 模式可見。

### 4.7 Storage（`core.storage`）
`canonical.py` 對每個 JSON/JSONL/TOML canonical 檔案（含新增的 `proposals.jsonl`，見第 11 節）實作 atomic
寫入（temp file + rename）與 schema version 標記。`sqlite/materialize.py` 是唯一允許寫入 `memory.db` 的
程式碼路徑，純粹是 canonical 檔案 + 重新計算的決定性索引的投影——絕不呼叫 LLM。

**SQLite 併發策略（本輪新增，單一寫入者/多讀取者，避免讀到寫一半的資料）**：

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
```

`rune update`／`rune rebuild-cache` 是唯一的 writer，且**整次 materialize 在單一 SQLite transaction
內完成，最後一次性 commit**（而非逐表逐行各自 commit）——`rune search`／`rune check` 等 reader 因此
永遠讀到「上一次完整 materialize 完成後的一致快照」，不會讀到半個 materialization 的中間狀態。WAL
模式讓 reader 不需要等 writer 完成即可讀取舊快照，`busy_timeout` 則處理極少數 reader/writer 短暫
互鎖的情況，不需要更複雜的 lock 管理。V1 不支援多個 `rune update` 行程同時執行（該情境由
`busy_timeout` 逾時後報錯處理，不做 queue/排隊機制）。**`PRAGMA foreign_keys=ON`（本輪新增）**：
SQLite 預設不強制外鍵，宣告 `REFERENCES` 卻不開這個 pragma等於裝飾用；開啟後 `scope_files.file`／
`scope_symbols.symbol_id` 暫時不宣告 FK（見 DATA_MODEL §5 的已知偏離說明），因為 M1 的
`files`/`symbols` 表本來就是空的，等 Milestone 2 索引器填入後才補上，避免 M1 就無法 materialize
帶有 file/symbol membership 的 scope。

### 4.8 Retrieval（`core.retrieval`）
`search.py` 包裝 SQLite FTS5，涵蓋 scope summary、current decision、current constraint、active（含 stale
但標記）note、symbol，預設過濾為 current/visible（stale note 例外，見 4.6）。**排序優先級本輪修訂為
八層**（取代原本規格 §46 的六層版本，插入 Global/Scoped MUST 的區分）：

```text
1. Global MUST Constraint（scopes 為空、severity=MUST 的 current+visible constraint）
2. Scoped MUST Constraint
3. Active Decision
4. Scoped SHOULD Constraint
5. Fresh Semantic Summary
6. Fresh Note
7. Stale Note
8. Historical data（僅 history/audit 模式）
```

`Global` 與 `Scoped` 的區分完全由既有 schema 推得，**不需要新欄位**：一個 constraint 的 current
revision 若在 `constraint_scopes` 中沒有任何對應列（即 canonical `scopes: []`），就是 global；有則是
scoped。衝突解決規則（本輪新增）：內容互相矛盾時，**最新的 current Global MUST Constraint 優先於其他
任何 memory 內容**，包含 Decision 與 Scoped Constraint。`context.py` 組裝 adapter 消費的三種
payload：hard bootstrap context、soft bootstrap context（合稱取代原「project bootstrap context」，
見第 7 節）與 scope-activation context（規格 §36），皆有各自的大小上限。

### 4.9 Update Orchestrator（`core.update`）
實作規格 §49 的交易流程：決定性索引更新先提交，且要嘛整體成功、要嘛整次 update 乾淨中止（不留部分決定性
狀態）；semantic 重新產生逐 scope 個別驗證/發佈，單一 LLM 呼叫失敗不影響其他 scope（規格 §62–63）。

## 5. CLI（`rune.cli`）
Typer app，暴露 `init`（`--force`，見第 11 節）、`update`、`status`、`bootstrap --mode hard|soft`
（規格見第 7 節）、`search`、`rebuild-cache`、`check`、
`doctor`（規格 §47）。每個 subcommand 是薄的參數解析層，呼叫恰好一個 `core` 進入點。所有面向 agent
adapter 的查詢額外提供 `--json` 輸出（見第 6 節）。

## 6. OpenCode adapter 邊界（`adapters/opencode`）

TypeScript，保持薄。**本輪已對照 OpenCode 官方 plugin 文件確認以下 hook 實際存在**：
`session.created`/`session.updated`/`session.compacted`/`session.idle` 等 session event、
`file.edited`/`file.watcher.updated`、`tool.execute.before`/`tool.execute.after`、custom tool
註冊（context 帶 `sessionID`/`messageID`/`directory`/`worktree`）。Milestone 7 的核心假設成立，
以下是根據這些真實 API 具體化的設計：

- **Session 開始**（`session.created`）：依序呼叫 `rune bootstrap --mode hard --json` 與
  `rune bootstrap --mode soft --json`，分別注入 hard bootstrap（全域 MUST constraint + critical
  global decision，見第 7 節）與 soft bootstrap（project overview、新鮮度判斷等）。不在此觸發昂貴的
  `rune update`。
- **Session compaction（`session.compacted`）視為「可能失憶事件」（本輪新增，見第 7 節）**：只重新呼叫
  `rune bootstrap --mode hard --json` 並重新注入，**不**重新送出完整 soft bootstrap（避免浪費
  token）。理由：不能假設 compaction 產生的摘要保留了所有 MUST constraint 的完整內容與權威性，
  authoritative hard policy 永遠由 rune 重新送出，不依賴 agent 自己的 context compression 品質。
- **Constraint delivery 的關鍵 hook 是 `tool.execute.before`，不是 `file.edited`**（本輪修正）：
  agent 真正需要的是「碰某個檔案**之前**就看到 constraint」，`file.edited` 只在檔案已經被改完之後觸發，
  時機太晚。流程：

  ```text
  tool.execute.before
    → 依 input.tool 決定如何擷取受影響路徑：
        read / edit / write → 直接從 tool 參數取得 path
        apply_patch          → 解析 patchText 取得受影響路徑
        bash                  → V1 不嘗試精準解析 shell command（見下）
    → 對每個路徑呼叫 `rune scope-for --path ... --json`（對應邏輯在 core，adapter 只呼叫）
    → 若該 scope 在本 session 尚未注入過（`active_scope_ids` 快取，規格 §37）：
        注入 scope summary + MUST/SHOULD constraint + 相關 note
    → 放行 tool 執行（本 hook 不阻擋，只注入 context）
  ```

  `bash` 的特殊處理（本輪新增）：**V1 不嘗試解析 shell command 語意**（可能間接修改任意數量的檔案，
  精準解析的成本與可靠度都不划算）。改為在 `tool.execute.after` 用 `git diff`（或既有的
  `file.watcher.updated` 事件）偵測 bash 執行後實際發生的檔案變動，事後才做 scope 對應與（若有新
  scope 被觸及）注入——放棄「執行前精準攔截」，換取「執行後至少不漏掉變動」。
- **Custom tool**：`decision_propose`／`constraint_propose`／`note_add` 註冊為 OpenCode custom
  tool，直接呼叫 `rune decision-propose --json` 等 CLI 進入點，帶入 tool context 提供的
  `sessionID`/`directory`/`worktree` 供 CLI 定位正確的 `.rune/`。

**Hard bootstrap 的 dedup 機制不能沿用 `active_scope_ids`（本輪新增）**：同一 session 可能被 compact
多次，每次 compaction 都必須重新注入 hard bootstrap（見第 7 節），但同一次 compaction 之後、下一次
compaction 之前，不需要重複注入。因此 adapter 另外維護一個 `hard_context_generation` 計數器：
`session.created` 是 generation 1，每次 `session.compacted` 遞增一代，**每個 generation 只送一次
hard bootstrap**，與追蹤 scope 是否已注入過的 `active_scope_ids` 完全獨立，不可混用同一個旗標。

**Hard bootstrap 的注入層級應優先使用 system/instruction-level context，而非普通訊息（本輪新增）**：
若 OpenCode 的 custom context 注入 API 允許區分「系統層級指示」與「一般訊息」，hard bootstrap 必須走
前者——它的權威性應與專案本身的 instruction 相當，不能讓 agent 把它當成一般對話內容看待、可以自行
決定是否遵守。

**Adapter 與 core 之間唯一合法的介面是 CLI 的 `--json` 輸出，TS 端絕不直接 import Python 邏輯**——這是
確認的邊界規則，用來維持「adapter 零商業邏輯」的原則（規格 §55）不被破壞。Adapter 內不做 AST 解析、不碰
SQLite、不呼叫 model provider、不含 staleness 邏輯，也不嘗試解析 shell command 語意。**Core 只回傳
結構化資料（見第 7 節的 hard/soft bootstrap JSON 格式），絕不直接輸出 OpenCode 專屬的 prompt 字串**——
把資料 render 成最終注入文字是 adapter 的職責。

實作前建議先做一個很小的 spike：只驗證「`tool.execute.before` → `rune scope-for` → 注入 context」
這條最關鍵的路徑能跑通，再進入 Milestone 7 全量開發。

## 7. Global Code Standards / Hard-Soft Bootstrap（本輪新增，agent-injection semantics 的正式一部分）

這不是附加功能，而是 rune V1 agent-injection semantics 的正式組成部分，與規格 §34-38 的 constraint
delivery 機制並列。目的：確保少量、高價值、不可違反的專案硬規則，在**每個新 session**與**每次
context compaction 之後**都重新出現在 agent 面前，不依賴模型自己的 summary/compaction 是否碰巧保留
了這些規則。

### 7.1 三層規範分工

不是所有專案規範都值得佔用 LLM context：

| 層級 | 內容 | 存放位置 | 投遞方式 |
|------|------|----------|----------|
| **Mechanical** | quote style、import 排序、行長、格式化、基本 lint、型別檢查、單元測試 | `.eslintrc`/`ruff.toml`/`pyproject.toml` 等工具設定 | 完全交給 formatter/linter/typechecker/pytest 執行，**不進 rune，不佔 LLM context** |
| **SHOULD** | 偏好或最佳實務，非絕對禁止（例如「優先用 repository 抽象而非直接寫 SQL」） | rune `Constraint`（`severity=SHOULD`） | scope activation 時注入、或 `rune search` 時返回；**不需要每次 compaction 全部重送** |
| **MUST** | 無法可靠靠機械工具保證、但違反後會造成架構/相容性/安全/品質問題的規則 | rune `Constraint`（`severity=MUST`，見 7.2） | Global 者走 hard bootstrap（見 7.3），Scoped 者走既有 scope-activation 機制（規格 §36） |

人類可讀、完整、可教學的規範仍建議另外維護一份 `CODE_STANDARDS.md`（見第 8 節）；rune canonical
memory 裡只保存真正會影響 agent 行為的 MUST/SHOULD 子集，兩者不要求逐條同步。

### 7.2 Global MUST Constraint 的定義——沿用既有 schema，不需要新欄位

一個 `Constraint` 的 current revision 只要同時滿足：

```text
scopes == []           （canonical 裡沒有指定任何 scope）
severity == "MUST"
persistence_mode == "persistent"
```

就是 **Global MUST Constraint**。這三個條件都是既有 DATA_MODEL 欄位，`scopes == []` 直接反映在
`constraint_scopes` 表沒有任何對應列——**判斷「global 還是 scoped」不需要新增欄位或新的 boolean
flag，純粹是既有資料的一種讀法**。`persistence_mode=persistent` 保證這類規則「不依賴任何 file hash、
不依賴 scope membership、不因 repo refactor 自動 stale」，本來就是既有 persistence_mode 定義的一部分
（DATA_MODEL §6）。

**Global 但非 persistent 的 constraint 在語意上是可疑的**（例如 global + source_bound 但
`files=[]`）：`rune doctor` 應對這種組合發出警告，建議改回 `persistent`，但 V1 不強制阻擋核准（見
IMPLEMENTATION_PLAN.md）。

`CODE_STANDARDS.md` 與 rune Constraint 之間若需要追溯來源，`MemoryRevision` 預留（非強制填寫）
`source_document`／`source_section` 兩個欄位——這是本輪唯一需要的 schema 擴充，供人類日後查「這條
MUST 規則對應到 CODE_STANDARDS.md 的哪一段」，避免兩份文件語意漂移卻無從追蹤。

### 7.3 Hard Bootstrap vs. Soft Bootstrap

原本規格 §57 的單一「project bootstrap context」本輪拆成兩層：

**Hard Bootstrap**——短、穩定、權威，內容限定為：
- 全部 current+visible 的 **Global MUST Constraint**（定義見 7.2）
- 少數被人類標記為 `critical=true` 的 **global Decision**（`scopes=[]` 且 `critical=true`；
  `critical` 是 `MemoryRevision` 上本輪新增的 boolean 欄位，預設 `false`——**不是所有 Decision 都
  變成 hard bootstrap 的一部分**，只有明確標記的少數關鍵決策，見 7.5 的 Decision/Constraint 區分）

觸發時機（本輪明定，見 §6）：

```text
session.created   → 注入 hard bootstrap（generation 1）
session.compacted → 重新注入 hard bootstrap（generation +1）
```

**Soft Bootstrap**——只在 `session.created` 注入一次，compaction 後不自動整份重送（避免浪費
token）：project overview、主要 scope 清單、目前架構摘要、memory 新鮮度（`rune status` 的內容）、
其餘 active global Decision（未標記 critical 的）、近期相關變更。

兩者都只回傳結構化資料（見 7.6 的 JSON 格式），由 adapter 自行 render 成最終注入文字（ARCHITECTURE
§6 已述的邊界規則）。

### 7.4 Hard Bootstrap 的注入內容規則

**只注入 current 且 visible 的 global MUST**，明確排除：

```text
舊 revision（非 current）
inactive / orphaned 的 constraint
proposal（尚未核准的提案）
stale note
一般 Decision 歷史（未標記 critical 的 Decision 完全不進 hard bootstrap）
scoped constraint（無論 MUST 或 SHOULD，一律走 scope-activation，不進 hard bootstrap）
```

這與既有的「current 與 visible 分離」規則（DATA_MODEL §1、§3）完全相容——hard bootstrap 只是在
「visible 的 global MUST」這個已經定義好的集合上再做一次過濾，不引入新的可見性語意。

### 7.5 Decision 不等於 Constraint——hard bootstrap 不能把 Decision 當規則用

`Decision`（「我們選了什麼」）與 `Constraint`（「agent 不得做什麼」）語意不同，即使兩者常常成對出現
（例如 Decision「改用 PostgreSQL」搭配 Constraint「MUST：不得引入第二個持久化後端」）。Hard
bootstrap 可以包含少數 `critical=true` 的 global Decision，**但不能把整份 Decision 歷史當作規則
塞進去**——那是 Soft Bootstrap 或 `rune search` 的責任。

### 7.6 CLI 與輸出格式

新增 `rune bootstrap --mode hard --json` / `rune bootstrap --mode soft --json`（統一用 `--mode`
而非兩個獨立子命令，介面較一致）。Hard 輸出範例：

```json
{
  "mode": "hard",
  "generation_hint": "call this once per session.created / session.compacted",
  "constraints": [
    {
      "record_id": "no-business-logic-in-adapters",
      "severity": "MUST",
      "content": "Adapters must contain no business logic.",
      "source_document": "CODE_STANDARDS.md",
      "source_section": "Architecture Boundaries"
    }
  ],
  "decisions": [
    {"record_id": "canonical-storage-model", "content": "Canonical text is authoritative; SQLite is derived."}
  ],
  "estimated_tokens": 412,
  "budget_tokens": 3000,
  "overflow": false
}
```

Core **只回傳資料**，不含任何 OpenCode 專屬的 prompt 措辭——render 成最終文字是 adapter 的職責（§6
已述）。

### 7.7 Token budget：不可靜默截斷 MUST 規則

Hard bootstrap 與 soft bootstrap 各有獨立 token 預算（建議 hard 500–3000、soft 2000–8000，
`config.toml` 可調）。**若 global MUST 集合超出 hard budget，絕不允許靜默丟棄任意規則**——那等於
默默移除了一條「不可違反」的規則，語意上比完全不設 MUST 更危險。正確行為：`overflow=true` 並在
`rune update`/`rune doctor` 顯著回報，要求人類精簡或合併規則，而不是由系統自行決定哪條 MUST 不重要。

配套治理：`rune doctor` 在 global MUST 數量超過建議上限（約 5–30 條，非硬限制）時發出警告，例如
「42 條 global MUST 估計需要 6.8k token，建議整併或把可機械驗證的規則移交 linter」——目的是鼓勵
「少量、高價值、不可違反」，不是把整份 coding style 文件都升格成 MUST。

### 7.8 Machine-checkable 規則與 Constraint 的關係

若某條 MUST 規則本質上可被工具驗證（例如「型別檢查不得有錯誤」「lint 必須通過」），`Constraint` 預留
（非強制填寫）`machine_check_hint` 欄位記錄對應工具名稱（例如 `"ruff"`、`"mypy"`、`"pytest"`）。
**rune 負責提醒「規則是什麼」，實際驗證「有沒有違反」永遠交給對應工具執行**，V1 不在 rune 內建這些
工具的執行邏輯，只保留這個可追溯的關聯欄位供未來擴充（例如 `rune check` 未來可以讀這個欄位去建議
「這條 MUST 對應到跑 `ruff check`」）。

### 7.9 治理：Global MUST 一樣走 propose/approve，agent 不能直接寫入

Global MUST Constraint 沒有特殊寫入路徑——完全沿用既有的 `constraint_propose` -> 人類核准 ->
`decisions.jsonl`/`constraints.jsonl` 新 revision 流程（規格 §21、DATA_MODEL §2.5a）。使用者說「以後
禁止用 `any`」時，agent 應該提出 **Constraint proposal**（scopes=[]，severity=MUST）；使用者說「某個
套件在 Windows 上有 bug」時，那是 **Note**；使用者說「我們決定從 npm 改用 pnpm」時，那是
**Decision**（必要時另外提出對應的 Constraint「不得使用 npm/yarn」）。三者不要混用同一種 record
type，這條分類規則本身也適用於既有的 Decision/Constraint/Note 邊界，不是本輪新規則，只是本次明確
點出常見的誤用情境。

## 8. MCP server（`rune.mcp`，Milestone 8）
同一條規則：暴露 `project_bootstrap`、`project_search`、`symbol_search`、`scope_read`、`decision_search`、
`decision_propose`、`constraint_search`、`constraint_propose`、`note_search`、`note_add` 為 MCP tool，
每一個都是對 `core` 的直接呼叫。

## 9. 安全性
`core.semantic.redaction` 在每一段 agent／模型產生的內容（note 內容、semantic summary 欄位、
decision/constraint 的 rationale 文字）交給 `core.storage.canonical` 寫入之前執行——絕不事後補跑。偵測
樣式與行為依規格 §58（取代為 `[REDACTED]`，絕不整筆刪除）。API key 只從環境變數讀取；`config.toml`
絕不含機密。

## 10. Working-tree freshness（本次新增設計）
單靠 `project.json.last_indexed_head` 無法反映「HEAD 未變但 working tree 已改動多個檔案」的情境（這在
實際使用中會頻繁發生）。因此 `project.json` 額外保存：

```text
last_indexed_head        # git HEAD sha（可能為 null，例如 detached 或無 commit）
last_indexed_tree_hash   # rune 自行計算：對所有已索引檔案的 (path, content_hash) 排序後串接雜湊
```

`rune status`／`rune doctor` 以「目前重新計算的 tree_hash 是否等於 `last_indexed_tree_hash`」判斷 memory
是否新鮮，而不是只看 git HEAD 是否移動。細節見 DATA_MODEL §2.1、§5。

## 11. Config、初始化與 proposal 持久化

**`rune init --force` 的語意（本輪重新定義，收斂為完全非破壞性）**：`.rune/` 已存在時，`rune init`
預設**拒絕**執行，訊息導向 `rune update`。`--force` **不得清空或重建任何已存在且合法的 canonical
memory**——這包含且不限於：

```text
project.json      # 尤其 project_id / created_at 絕不重新產生或覆寫
scopes.json        # scope 定義、命名、membership 是昂貴的 canonical knowledge，絕不降級為 skeleton
semantic.jsonl
decisions.jsonl
constraints.jsonl
notes.jsonl
proposals.jsonl
```

`--force` 只允許：

1. 建立**缺少**的 canonical 檔案（例如某個檔案因手動誤刪而不存在時補上空骨架）
2. 修復 config/schema 版本相關的骨架問題（例如 `config.toml` 缺欄位時補預設值）
3. 刪除並重建 SQLite cache（`memory.db`——本來就設計為隨時可丟）
4. 重新建立決定性程式碼索引（files/symbols/edges——本來就每次 `update` 都會重新推導）

先前草案曾允許 `--force` 把 `scopes.json` 降級為 skeleton，這會連帶讓 scope 定義消失、semantic
summary 掛不到 scope、scoped Decision/Constraint 失去 target、clustering 要整個重來——這是不可接受
的破壞性副作用，本輪已移除。若使用者確實想要清空全部 memory 重新開始，未來提供獨立的
`rune reset`（明確、需要二次確認的破壞性指令），不與 `init --force` 混用同一套語意。
- **Pending proposal 改為 canonical 持久化**：新增 `.rune/proposals.jsonl`（append-only，格式見
  DATA_MODEL §2.5a）。Proposal 不進入正常 memory retrieval（不會出現在 `rune search`），但必須撐過
  cache rebuild、重開機、`rune rebuild-cache`、甚至 `git checkout`——因為「待你確認的提案」如果因為
  SQLite 被刪就消失，語意上與「SQLite 只是 derived cache」的核心原則矛盾。`proposals.jsonl` 是否要
  commit 進 git 由 config 的 `[proposals] commit_to_git`（預設 `false`）決定；若為 `false`，`rune init`
  會自動把該檔案加進 `.gitignore`，但檔案本身仍在本機磁碟上持久存在，不受 SQLite 生死影響。
- `.rune/config.toml` 驅動 include/exclude glob、semantic provider/model/budget、note TTL、redaction
  開關、proposal 是否進 git（規格 §60，本次擴充）。`core.semantic.worker` 用 config 提供的價格表追蹤
  每次 run 的 input/output token 與估算成本（絕不寫死特定 provider 價格，規格 §61），`rune update`
  印出摘要。**價格表本輪定位為單純估算，不追求自動同步**：`config.toml` 新增
  `[pricing] input_per_million = ...` / `output_per_million = ...`，人類手動維護，僅供
  `rune update` 印出「大概花了多少」，不是 billing-grade 的正確金額。

## 12. 已知限制：單一寫入者假設
`revision` 是單純遞增整數。V1 明確假設每個邏輯 record（Decision/Constraint/Note）同時只有一個寫入者，
不處理「兩個 git branch 各自從同一 revision 產生下一個 revision 再 merge」的衝突合併。若
`materialize.py` 解析 canonical JSONL 時發現同一 `(record_id, revision)`／`(id, revision)` 重複，
視為 canonical 衝突，中止該次 materialize 並由 `rune doctor`/`rune update` 回報，絕不自動選一筆默默
採用。細節與未來多人協作的可能演進方向（`revision_id` 改為 ULID + `parent_revision_id`）見
DATA_MODEL §8；V1 不為此預先設計。

## 13. 測試策略（另見 IMPLEMENTATION_PLAN.md 測試計畫章節）
兩個 fixture repo（`tests/integration/fixtures/ts-simple`、`python-simple`）驅動 init -> update ->
檔案變更 -> rebuild-cache 全流程的整合測試。單元測試涵蓋 hashing、canonical 解析、revision 選取、
生命週期轉換、TTL、scope membership、staleness（含 Decision/Constraint 與 semantic 兩套獨立規則）、
redaction、FTS 索引（規格 §65）。**Windows 相容性不獨立立項，併入既有整合測試**（本輪確認降低優先
級）：fixture repo 內含 Unicode 路徑、中文檔名、CRLF 換行的檔案，驗證 hashing/atomic-write/git
subprocess（UTF-8 編碼）在這些輸入下行為正確。
