# RepoRune（rune）— 架構設計

> **RepoRune**（日常稱呼／CLI 指令：`rune`）= *Repository Understanding & Navigation Engine*。
> 本文件其餘部分一律使用 `rune` 指稱這個工具本身（CLI、Python 套件、目錄名稱 `.rune/` 皆同名），
> `RepoRune` 僅在需要完整品牌名稱的場合使用（例如文件標題、對外介紹）。

狀態：**已確認（第十七輪修訂）**（V1 設計，經 2026-09-06 討論確認全部開放問題）。第四輪根據對照
OpenCode 官方 plugin 文件的結果具體化 Milestone 7 設計、補上 ParserAdapter 介面契約、
import/reference 信任層級原則、semantic worker fallback policy、SQLite 併發策略，並將 scope
clustering 品質明確定位為「留待真實 repo 實驗調整」而非架構層需要鎖死的正確性需求。第五輪新增
Global Code Standards / Hard Policy Injection 語意（新第 7 節）：Global MUST Constraint 的定義、
Hard/Soft Bootstrap 的區分與各自的注入時機（`session.created`/`session.compacted`）、token
budget 的 overflow-not-truncate 規則——這是 agent-injection semantics 的正式組成部分，不是附加
功能，所有後續章節編號因此從第 7 節起整體後移一位（原第 7-12 節現為第 8-13 節）。**第六輪在
Milestone 4（Scopes）開工前，針對規格中未鎖死的三個實作細節取得確認並補上第 4.4 節對應段落**：
scope 候選（heuristic/clustering）不持久化、純一次性 CLI 互動；clustering 候選建議可同時使用
import 與 best-effort reference edge（因為一定經人類確認，不牴觸 §4.3 的治理層信任原則）；新檔案
incremental 自動併入既有 scope 只認 import edge 且僅限單一候選（無人把關的寫入路徑，必須用
high-confidence 訊號）。**第七輪在 Milestone 5（Semantic worker）開工前，修正第 4.5 節一個自相矛盾**：
`ScopeSummary` 新增 `revision` 欄位（比照 Decision/Constraint/Note），讓生成失敗的狀態能真正跨
session 持久化，而不是先前版本宣稱的「只在 SQLite 投影中維持，不寫 JSONL」（這句話與「SQLite 投影
每次都是從 canonical 重新算出來」互相矛盾）；`last_error` 收斂為清洗過的分類字串，原始錯誤改寫進
不進 git 的 `.rune/logs/semantic.log`。**第八輪是 Milestone 5 完成後的品質複查**：修正 9 個問題
（`rune rebuild-cache` 誤觸發 LLM 呼叫、scope 刪除造成 materialize crash、多 scope 刷新時 canonical
非原子寫入、redaction 未尊重設定開關且遺漏 `dependencies` 欄位、schema 驗證誤將不合法型別默默轉換、
fallback model 產生內容被誤標為 primary、provider request 缺少 structured output 提示、prompt 缺少
實際程式碼內容、六項 metrics 未持久化），細節見第 4.5 節與文末「第十三輪修訂」；`possibly_stale` 的
觸發邏輯經使用者要求刻意不在本輪修，留待後續討論。**第九輪是使用者轉述的第二份 Milestone 5 code
review，5 條 finding 全部確認為真並修正**：SQLite cache 沒有 schema migration（舊版 memory.db
會讓 materialize 持續 crash）、scope 刪除後重建同名 scope 永遠卡在 orphaned、`rune update` 的 CLI
說明文字仍宣稱 zero LLM calls、`needs_refresh` 的 docstring 用詞不精確、`compute_source_files`
的邊界案例澄清（不改變行為，只精確化文件措辭），細節見第 4.5 節與文末「第十四輪修訂」。**第十輪
確認了先前記錄但延後的 `possibly_stale` 觸發邏輯與 provider 健康檢查設計**：`possibly_stale` 只在
「曾經有內容、hash 對不上、這次沒 provider」時觸發，`possibly_stale`/`stale` 在 retrieval 端絕不
直接回傳舊摘要文字、只能指向真實原始碼；provider 健康檢查改成三層（設定錯誤直接攔下要求修正、
rate limit 提示使用者、其他錯誤 retry 一次後大聲失敗但不擋決定性索引）。**第十一輪完成第十輪確認
設計的實作**：`check_semantic_health`（三層健康檢查）、`ModelProvider.probe()`、`ProviderError.
status_code`/`is_rate_limited`、`mark_possibly_stale`、CLI 依健康狀態決定 exit code。實作時一度把
「`enabled=True` 但 `model` 為空字串」（每個全新專案的預設狀態）改歸類為靜默跳過，但使用者明確
要求改回：專案部署完成後就應該正確填入設定，`model` 空字串也必須算設定錯誤、大聲失敗，**第十二輪
撤回這個簡化，恢復成第十輪原始措辭**——`model` 空字串跟 API key 缺一樣，只要 `semantic.enabled=
true` 就是設定錯誤，不因為是預設值就特殊放行；受影響的既有測試（假設「什麼都不設也能正常跑
update」的測試）改成明確加上 `semantic.enabled=false` 才算未使用 semantic 這個前提。細節見第 4.5
節與文末「第十五輪修訂」「第十七輪修訂」。**第十三輪是 Milestone 7 開工前的 spike，見第 6 節**：
新增 `rune scope-for <path> --json`（第 6 節先前只點名這個指令、沒有定義確切 JSON 格式，本輪補上），
用一個最小的 TypeScript adapter 骨架（`adapters/opencode/`）實際跑過
「`tool.execute.before` → `rune scope-for` → 注入 context」這條路徑，包含同一 scope 第二次不重複
注入的 dedup 邏輯，確認整條路徑可行才進入 Milestone 7 全量開發。**第十四輪落實第 7 節早已定案的
Hard/Soft Bootstrap 設計**：新增 `core.retrieval.context`（`build_hard_bootstrap`/
`build_soft_bootstrap`）與 `rune bootstrap --mode hard|soft --json`，純粹是把 §7.3-§7.7 的規格轉成
程式碼，沒有新的設計決策；同時把 `cli.main.status` 內嵌的新鮮度計算抽成 `core.status.
compute_status()`，供 soft bootstrap 重用同一份計算而非另寫一份可能悄悄分岔的邏輯。**第十四輪同時
記錄一個尚未解決的落差**：安裝官方 `@opencode-ai/plugin` npm 套件後對照其真實 TypeScript 型別定義，
發現第 6 節先前記錄的 hook 形狀與實際 API 不完全相符（沒有獨立的 `session.created`/
`session.compacted`/`file.edited` hook key，而是單一 `event` hook 搭配 discriminated union；
`tool.execute.before`/`tool.execute.after` 沒有帶 `directory`/`worktree`/`messageID`），且同一套件
內存在第二套平行的「v2/effect」plugin API。**第十五輪跟使用者確認後修正第 6 節**：改用真實的
`event` hook + discriminated union 描述 session 生命週期事件；`tool.execute.before`/`after` 缺少
的 `directory`/`worktree` 改成在 `Plugin` 工廠函式的 `PluginInput` 只捕捉一次、透過閉包重用（CWD
在單一 session 內不會變）；確認目標鎖定 classic `Hooks` interface，不投入時間探索 v2/effect API；
custom tool 註冊的部分先前記錄本來就正確，不需修正。**第十六輪發現並解決第二層落差、完成 Milestone
7 全量開發**：`tool.execute.before` 實際上完全無法注入文字（只能改自己的 tool 參數），型別定義裡
唯一的 system-level 注入通道是 experimental 的 `experimental.chat.system.transform`；跟使用者確認
後改用「每個 session 持續維護 hard bootstrap/active scopes/pending events 狀態、每次 LLM 呼叫前
重新 render 整份 context、原地覆寫既有 system 字串而非新增陣列元素」的設計（細節見新第 6.1 節），
`client.session.prompt({noReply:true})` 只留作未接入的降級 fallback。新增
`adapters/opencode/src/{rune-context,plugin,tool-paths}.ts` 與對應的 `node:test` 測試（20 個全綠），
`rune-cli.ts` 補上 `bootstrapHard`/`bootstrapSoft`/`decisionPropose`/`constraintPropose`/`noteAdd`/
`changedFilesFromGitStatus`；CLI 端的 `decision propose`/`constraint propose`/`note add` 補上
`--json` 輸出供 custom tool 使用（317 個 Python 測試全綠）。**第十七輪是純設計文件修訂，不含任何
程式碼變更**：補上 Milestone 7 完成後浮現的幾個部署/多 agent 協作層級的設計問題——安裝單位與部署
模型（新第 14 節：machine-level install once、per-repo `rune init`、plugin 自動偵測 `.rune/` 且絕不
自動 init、V1 不引入 daemon）、adapter/core 之間的 `protocol_version` 相容性契約（新第 15 節，M7
的待完成 contract，本輪只定設計不改 CLI）、Git worktree／多 agent 協作模型與 merge reconciliation
的 provenance 原則（新第 16 節）、把既有 §4.4 的 incremental scope 自動併入規則泛化成完整的 Scope
Membership Reconciliation 設計（§4.4 新增小節，AUTO/KEEP/REVIEW/BROKEN 分類與 large-churn guardrail
皆為未來待實作項，不是本輪程式碼交付）。這些新增內容全部標記為設計決議或 future backlog，**沒有
一項在本輪被實作**，實作進度仍以文中各處明確標示的「已實作」/「未實作」為準。本文件與
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
3. **cache 不得超前 canonical。** `rebuild_cache()` 已 commit 後，若任何延後的 canonical write 失敗，
   rune 立即刪除 `memory.db` 及其 sidecar。個別 canonical 檔案仍可能已有部分成功寫入，因此下次
   update/rebuild 從 canonical 重新推導，不承諾跨多檔 transaction。

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

1. **真的丟例外**（tree-sitter 的 `MISSING` 節點讓某個必要欄位變成 `None`
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

Scanner 走訪時若 path 存在但 `stat` 或讀取失敗，這是獨立的 `status=scan_error`，不是刪除或
`parse_error`。既有 index 保留最後已知的 file/symbol/edge facts；新 path 暫不索引；成功讀取後即使
content hash 未變也強制重新解析以恢復狀態。整次 update 繼續執行並回報 `files_scan_errors`。

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

**Milestone 4 開工前確認的三個設計決策（本輪新增，2026-09-06 討論確認）**：

1. **候選 scope（heuristic + clustering）不持久化，純粹是一次性 CLI 互動**：`rune scope suggest`
   當場重新計算候選、當場印出、當場人類 y/n，不落地成任何新的 canonical 檔案（不新增
   `scope_candidates.jsonl` 之類的東西）。這與 Decision/Constraint 的 `proposals.jsonl`
   刻意不同——後者需要撐過重開機／cache 刪除，因為「已核准內容尚待確認」本身就是需要持久化的治理狀態；
   而 scope 候選在人類確認前只是「一次可低成本重算的建議」，跟 code index（files/symbols/edges）沒有
   canonical 背書、每次重新從原始碼推導的精神一致——關掉終端候選就消失，下次跑 `rune scope suggest`
   重新算一次即可，不需要為此另外設計一套持久化格式與 CLI 子命令。
2. **Clustering 候選建議可以同時使用 import 與 best-effort reference edge 做圖聚類**：這**不違反**
   §4.3「Scope/Constraint 系統不得把 reference graph 當唯一依據」的原則——那條原則管的是**治理系統**
   （Decision/Constraint 的 membership 判斷、無人把關的自動寫入），而 clustering 候選建議本身**一定**
   要經人類確認才會寫進 `scopes.json`（見下方 CLI 流程），human review 本身就是對 reference 解析
   不完美的天然防線。只用 import edge 會讓 clustering 建議少掉「同檔案沒有 import 但透過繼承/呼叫
   高度耦合」這種真實存在的架構邊界訊號，reference 解析不完美的代價是「建議品質變差」，不是「系統做出
   未經確認的錯誤決策」，可接受。
3. **新檔案的 incremental 自動併入只認 import edge，且只在單一候選時才自動寫入**：`rune update` 對
   一個新檔案，若透過 `edge_type=imports`（`confidence=1.0`）**恰好**命中一個現有、非 `locked` 的
   scope 成員，直接把該檔案自動加進該 scope 的 `members.files`，不需要人類確認——這被視為「維護既有
   scope 的完整性」而非「建立新的治理判斷」。以下情況一律落回人類確認的 CLI 流程，不自動寫入：
   零個候選 scope、多個候選 scope（模糊）、只有 best-effort reference edge 命中（沒有 import edge）、
   目標 scope 是 `locked`、任何會移除既有 membership 的動作、任何會建立新 scope 的動作。這個判準刻意
   比 clustering 建議嚴格：clustering 建議永遠有人類把關，但 incremental 自動併入是**無人把關的寫入
   路徑**，因此只能用 §4.3 明定的 high-confidence 訊號（import edge），不能讓 best-effort reference
   的不確定性滲透進一個沒有人類審查的自動寫入動作。`locked` scope 永遠不受任何形式（clustering
   建議或 incremental 自動併入）影響，這是既有規則的延伸適用，不是新規則。

**Scope Membership Reconciliation（第十七輪新增，純設計、未實作 CLI）——把上面第 3 點既有的
incremental 自動併入規則，泛化成適用於任何「repo 在背後大幅變動後才被 rune 看到」的一般情境（多
worktree merge 是最主要的觸發場景，見第 16 節，但規則本身不限定於 worktree）**：

**核心原則：Scope 是穩定、漸進累積的 project knowledge，不是可以隨時重新計算的衍生資料。**
`rune update`／未來的 `rune scope reconcile` 在任何情況下都**不得**把整個 repository 重新理解一遍、
造成大規模 membership churn——這與 §4.4 開頭「Clustering 品質定位為建議實驗」的既有原則是同一件事的
兩面：clustering 建議永遠只影響「這次要不要建立新 scope」，而 reconciliation 管的是「既有 scope
的既有 membership 要不要因為 repo 變了就跟著變」，兩者都刻意迴避「AI 一次性重新設計整個架構圖」這種
高風險、低必要性的操作。

1. **Reconciliation 預設必須是 incremental，範圍限定在 changed set**：新增、修改、刪除、
   重新命名（V1 若無法區分 rename 與「刪除+新增」，比照現有規則沿用 delete+add 語意即可，不用為此
   新增偵測邏輯）的檔案／symbol，稱為 *changed set*。**只有 changed set 才是 reconciliation 的
   auto-apply 對象**；沒有變更、只是被當作判斷 evidence 用的鄰近節點（例如 changed 檔案的
   import/importer、owning scope、一層 graph neighbor）**不得**因為它被讀取為 evidence，就連帶被
   改動 membership——這條規則稱為 **untouched region frozen**：沒有實際變更的既有 file/symbol，
   membership 預設凍結，一般 incremental reconciliation 不得修改。
2. **Auto-apply 沿用 §4.4 第 3 點既有的 high-confidence 判準，不放寬**：import edge、恰好命中單一
   既有 unlocked scope，才能自動寫入；best-effort reference 不足以支撐 unattended write；任何
   removal／move／模糊的重新指派，一律落回人類審查，不自動做。**不可僅因「模型現在覺得某檔案更像
   另一個 scope」就自動搬移既有 membership**（例如 `foo.ts` 原本屬於 `authentication`，reconciliation
   後模型認為更像 `session`）——沒有明確的 deterministic/high-confidence 證據時，只能產生 review
   proposal，不能自動先移除再新增。
3. **`locked` scope 與 human-confirmed membership 的保護，reconciliation 不得繞過**：任何形式的
   auto reconciliation（incremental 或未來的 `--full`）都不得修改 `locked` scope 的 membership；
   human 標記過的 membership 不得被自動移除或搬移。若一個 human/locked membership 指向的
   file/symbol 已不存在，reconciliation 必須產生警告／要求人類審查，**不得由 AI 自行悄悄清掉**——
   現有 `Scope`／`ScopeMembers` schema（DATA_MODEL §2.3）沒有 per-membership 層級的
   provenance/locked 欄位可以精確表示「這一條 membership 是誰加的、能不能被自動改」，這是本輪發現、
   但刻意不在這裡動 schema 的限制，記錄為 future improvement，見 DATA_MODEL.md §9。
4. **Full reconciliation 必須是明確 opt-in（未來可能的 `rune scope reconcile --full`），且輸出只能
   是 candidate/proposal/diff，不得靜默改寫 canonical membership**——即使是 `--full` 模式，
   `locked`／human-authoritative membership 一樣受保護，不因為使用者選了 `--full` 就取消這條防線。
5. **Large churn guardrail**：若一次 incremental reconciliation 推導出的 membership 變更量明顯超出
   changed set 應有的合理範圍，判定為 suspicious，中止自動套用，要求人類審查。**本輪刻意不鎖死具體
   threshold**（絕對數量、changed-file-based 比例都是候選做法）——架構層只鎖死「非預期的大量 churn
   絕不能自動套用」這條不變式，數值交給未來實作/`config.toml` 決定，避免現在鎖一個沒有真實資料佐證
   的數字。
6. **概念上的四種 reconciliation 結果分類**：`AUTO`（新檔案依既有規則自動併入某 scope）、`KEEP`（既有
   human-authoritative membership 不變）、`REVIEW`（模糊的重新指派/移除/多個候選 scope，需要人類決定）、
   `BROKEN`（human/locked membership 指向的目標已消失）。連同 changed files 數、auto-applied 數、review
   數、維持不變的既有 membership 數、suspicious churn 旗標一起呈現——**Milestone 9 已實作**
   `rune scope reconcile`，見下方「Reconciliation CLI 與 merge/integration worktree 使用流程」小節。

### Reconciliation CLI 與 merge/integration worktree 使用流程（Milestone 9 實作完成）

`rune scope reconcile`（`core.scopes.reconcile.reconcile()`）把上面第 1-6 點的規則落地為可執行的命令：

- **輸入來源是目前已 materialize 的 code index，不是重新掃描工作目錄**：`reconcile()` 讀
  `read_current_code_index()`（`memory.db` 裡的 `files`/`symbols`/`edges`），拿它跟 canonical
  `scopes.json` 的既有 membership 比對出落差——這代表呼叫 `rune scope reconcile` 前必須先跑過
  `rune update`，讓 code index 反映你想要 reconcile 的那棵樹（見下方 workflow）。這個設計選擇的完整
  理由見 IMPLEMENTATION_PLAN.md Milestone 9 決策記錄第 165 條。
- **落差的兩個方向**：code index 裡存在、但不屬於任何 scope 的檔案/symbol，套用第 3 點既有的
  high-confidence import 規則分類為 `AUTO`（唯一候選）或 `REVIEW`（零個/多個候選，或只有 best-effort
  reference 證據）；scope membership 指向、但 code index 裡已經不存在的檔案/symbol，依 scope 是否
  `locked` 或 `source == human`（第 167 條：兩個訊號任一成立即保護，理由見決策記錄）分類為 `BROKEN`
  （受保護，只警告不清除）或 `REVIEW`（未受保護，候選移除仍需人類確認）。任何內容有變但 scope
  membership 關係沒變的既有成員，不在這個落差裡，因此天然滿足「untouched region frozen」，不需要另外
  過濾。
- **預設模式（無 `--full`）**：只列出 `AUTO`/`REVIEW`/`BROKEN`（`KEEP` 只計數，不逐筆列出），`AUTO`
  項目在寫入前檢查 large-churn guardrail（`config.scopes.reconcile_large_churn_threshold`，預設
  20——理由見 IMPLEMENTATION_PLAN.md 決策記錄第 168 條），沒有觸發就直接把 AUTO 變更寫進
  `scopes.json`；觸發了就整批不寫，`suspicious_churn=true`，要求人類審查。
- **`--full`**：逐筆列出包含 `KEEP` 在內的完整分類（供人工稽核目前每一筆 membership 的狀態），但**永遠
  不寫入**，即使有 `AUTO`-eligible 的高信心單一候選也只回報、不套用——這與 §4.4 point 4「Full
  reconciliation 輸出只能是 candidate/proposal/diff」一致，理由見決策記錄第 169 條。
- **`--since <ref>` 的限制**：它只重新檢查指定 ref 之後加入、且位於 unlocked/non-human scope 的檔案
  membership；沒有 per-membership provenance，無法證明該 membership 真的是 AUTO 所寫，也不重新分類
  其他既有 membership。任何不再唯一符合的結果只會列為 `REVIEW`，絕不自動移動或移除。

**Merge/integration worktree 的具體使用流程**（銜接第 16.4 節「integration worktree 的角色」，第 6 步
「執行最終的 scope reconciliation」在此展開）：

1. Integration worktree 完成程式碼 merge、resolve 掉 16.2 節描述的 canonical revision 衝突。
2. `rune update`（merge 後的樹上重新跑一次決定性索引；不需要 `--full`/`rebuild-cache`，除非 code
   index 本身需要重建）——這一步之後，`memory.db` 反映的就是 merge 後的完整程式碼結構。
3. `rune scope reconcile --json`：檢視 `auto_count`/`review_count`/`broken_count`，確認
   `suspicious_churn` 是否為 `true`。
   - `suspicious_churn=true`：**不要**重跑加大 threshold 蒙混過去——先讀 `entries` 裡的 `AUTO`
     項目，理解為什麼一次出現這麼多高信心新增（常見原因：某個 worktree 帶進一批先前完全沒有任何
     scope 的新檔案），視情況用 `rune scope edit`/`create` 手動處理一部分，或調整
     `config.scopes.reconcile_large_churn_threshold`（需要明確理由，不是預設繞過）。
   - `suspicious_churn=false`：AUTO 項目已經自動寫入 `scopes.json`；檢視 `REVIEW`/`BROKEN` 項目，逐筆用
     `rune scope edit`（重新指派/移除）或 `rune scope lock`/`unlock`（調整保護狀態）處理，`BROKEN`
     的目標若確認已經永久移除，用 `rune scope edit --remove-file`/`--remove-symbol` 明確清除——這仍是
     人類決定的動作，`rune scope reconcile` 本身不會自動做。
4. 重新執行 `rune scope reconcile --json` 確認 `review_count`/`broken_count` 已經降到預期（通常是
   0，除非刻意保留待觀察的項目），再 commit `scopes.json`。
5. 若使用 `--full` 做完整稽核（例如 merge 涉及大量檔案、想確認每一筆既有 membership 目前狀態），流程
   相同，只是第 3-4 步不會有任何 canonical 寫入，純粹是人類參考用的報告。

### 4.5 Semantic Worker（`core.semantic`）
針對過期／缺漏的 scope summary，用該 scope 的成員檔案／symbol 組 prompt，呼叫設定的 `ModelProvider` 取得
structured `ScopeSummary`，經三層驗證（schema、路徑存在性、symbol 存在性）後才允許寫入 canonical：

- **Strip**：summary 中引用到不存在的 file/symbol 的「條目」被移除並記錄警告，不影響其餘欄位。
- **Reject**：只有當核心欄位（如 `purpose`）本身無法通過 schema 驗證時，才整份 generation 被拒絕。

失敗或被拒絕的 generation 保留舊 summary 的**內容**、絕不清空或損毀既有 summary（規格 §62），但**失敗
狀態本身必須持久化**（本輪修正，見 DATA_MODEL §2.4）：`ScopeSummary` 新增 `revision` 欄位，失敗時
附加新的一行——已有成功產生過的 scope，複製上一筆 current revision 的完整內容、只改動
`status=stale`、`last_error`、`generated_at`、`source_hash`/`source_files`（更新為目前的，供下次
staleness 判斷比對）；從未成功產生過的 scope，附加 `revision=1`、`status=unavailable`，內容欄位
留空、不虛構。這解決了先前版本「失敗不寫 JSONL，只在 SQLite 投影中維持 status=stale」的自相矛盾
（SQLite 投影本身就是從 `semantic.jsonl` 重新算出來的，不可能反映一個從未落地的狀態）。

**`last_error` 只允許清洗過的分類字串，絕不放原始 provider 回應或例外訊息（本輪新增）**：
`semantic.jsonl` 是 canonical、可能進 git 的檔案，敏感度假設等同任何原始碼——不能把 provider 的原始
回應內容（可能夾帶 prompt injection 殘留、模型 hallucinate 出的片段）直接寫進去。`last_error` 收斂
為簡短分類值（例如 `"schema_validation_failed"`、`"provider_error:TimeoutError"`、
`"repair_retry_failed"`、`"fallback_failed"`）。完整、未清洗的原始錯誤（例外訊息、traceback、
provider 回應片段）寫進 `.rune/logs/semantic.log`——這個檔案**不是 canonical、不進 git**（`rune
init` 時比照 `.rune/cache/` 加進 `.gitignore`），純粹是本機除錯用的操作記錄，可隨時刪除，不影響
任何 rebuild 或 retrieval 邏輯。

**Provider 視為不可靠外部依賴，fallback policy 明定為有限步驟（本輪新增）**：

```text
call primary model
  → schema/reference 驗證失敗
    → 1 次 retry（附加更嚴格的 repair prompt，指出上次失敗的具體欄位）
      → 仍失敗
        → fallback model（config 另外指定，例如更貴但更穩的模型）
          → 仍失敗
            → 附加新 revision：status=stale（已有內容時複製舊內容）或
              status=unavailable（從未成功過），記錄清洗後的 last_error，不再重試
```

不做無限 retry。每次呼叫記錄以下 metrics（寫入 SQLite 或獨立的 metrics 表，供 `rune update` 輸出
與後續比較不同 provider/model 的實際表現，而非憑印象判斷）：`schema_success_rate`、
`reference_strip_rate`（有多少比例的引用被 §4.5 strip 規則移除）、`fallback_rate`、
`provider_error_rate`、`cost`、`latency`。

Semantic staleness 判斷完全基於 **member 檔案的 content hash**（見 DATA_MODEL §2.4 的 `source_hash` /
`source_files`），與下方 4.6 節 Decision/Constraint 的 staleness 規則是兩套完全獨立的機制，不可合併。

**Milestone 5 完成後的品質複查修正的問題（本輪新增）**：

1. **`rune rebuild-cache` 絕不觸發 semantic refresh**：`rebuild-cache`（`core.update.run_update(...,
   full=True)`）在自己的 `--help` 文字與模組 docstring 裡都明講「zero LLM calls, zero network,
   local parsing only」，但實際上 semantic refresh 的呼叫完全沒有檢查 `full` 旗標，只要
   `config.semantic` 設定了 provider 就會照跑——已實測重現這個違反（配置好 provider 後執行
   `rebuild-cache`，provider 真的被呼叫了）。修法：semantic refresh 比照既有的 scope
   auto-assignment（`if not full and ...`），只在 `full=False`（一般 `rune update`）時執行。
2. **Scope 被刪除時，既有的 semantic history 必須 orphan，而非讓 materialize crash**：
   `semantic_objects.scope_id` 對 `scopes(id)` 有真正的 FK，刪除一個曾經有 summary 的 scope 後，
   後續每一次 `rune update`/`rebuild-cache` 都會因為試圖插入一列指向不存在 scope 的 row 而丟出
   `IntegrityError`——已實測重現。修法完全比照 Decision/Constraint 既有的 orphaned 語意（DATA_MODEL
   §6）：`ScopeSummary` 新增 `status=orphaned`，`core.update` 偵測到「semantic.jsonl 現有 current
   revision 的 scope_id 不在目前 scopes.json 裡」時附加一筆內容複製、只改 status 的新 revision；
   materialize 時這類 summary（以及任何 scope_id 已不存在的 summary）一律被過濾，不寫入
   `semantic_objects`。
3. **多個 scope 在同一次 run 裡刷新時，canonical 的 append 必須是單一原子操作**：先前是逐一呼叫
   `append_jsonl`，若第二個 scope 的 append 失敗，SQLite（已經在同一個 transaction 裡反映了全部新
   revision）就會領先 canonical，且是「部分 canonical 已發佈」的不一致狀態（兩個 scope 都刷新成功，
   但只有第一個真的寫進 semantic.jsonl）——已實測重現。修法：新增 `append_jsonl_many`，把整次 run
   所有新 revision 合併成一次 atomic write。
4. **Redaction 必須尊重 `config.security.redact_secrets`，且涵蓋 `dependencies` 欄位**：
   `redact_secrets` 這個設定自 Milestone 1 就存在，但從未被任何程式碼讀取，redaction 永遠強制執行；
   `dependencies`（可能是 scope_id 或外部套件名稱，無法比對 known_files/known_symbol_ids）先前完全
   沒有經過 redaction，是唯一沒有被 strip 邏輯間接保護、也沒有被文字 redaction 保護的欄位。兩者都已
   修正。
5. **Schema 驗證不得把不合法型別默默轉換成合法值**：`purpose` 若不是字串（例如模型回傳一個
   dict），先前會被 `str(...)` 轉成字面文字接受為有效 summary，違反「核心欄位 schema 不成立即拒絕
   整份 generation」的既有規則。已改為型別不符時直接拒絕。
6. **Fallback model 產生的內容不得被標成 primary model 產生**：`ScopeSummary.model` 先前無論實際是
   哪個 provider 產生的內容都寫死 `primary_provider.model`，讓 provider/model audit 資料失真。已改為
   記錄實際產生內容的那個 provider 的 model 名稱。
7. **Provider request 加上 best-effort 的 `response_format: {"type": "json_object"}`**：確認過對
   OpenRouter 的真實 API 有效（qwen/qwen3.8-flash 回傳合法 JSON），worker.py 原有的文字 slicing
   解析仍保留作為安全網——一個不支援這個欄位的 provider 會直接忽略它，不會報錯，行為退化回今天的
   樣子而非變差。
8. **Prompt 加入 symbol 的實際程式碼片段，不再只有 metadata**：先前 prompt 只有 symbol
   名稱/kind/signature，模型幾乎沒有實際實作內容可以參考。已利用既有的 `Symbol.start_line`/
   `end_line` 只截取每個 member symbol 的程式碼範圍（而非整檔，避免不必要的 token 成本），單一
   symbol 超過 200 行時截斷並標記，讀檔失敗時該 symbol 的片段留空但不中止整次 refresh。
9. **六項 run-level metrics 新增 SQLite 持久化**：先前只存在 `rune update` 回傳的 stats dict，無法
   跨 run／provider 比較。新增 `semantic_run_metrics` 表（DATA_MODEL §5），純操作性歷史資料，不受
   `rebuild_cache` 「清空重建」影響（只在真的嘗試過 refresh 時 append 一行），但會隨 `memory.db`
   整個被刪除重建而消失（接受，因為沒有 canonical 背書可以重建它）。

**`possibly_stale` 觸發邏輯與 provider 健康檢查（第十一輪已實作）**：先前發現 provider 不可用（沒有
API key／`semantic.enabled=false`／預算用完）時，一個已經變 stale 的 scope 完全跳過、不會有任何狀態
轉換，`possibly_stale` 這個 enum 值從 Milestone 5 一開始就沒有任何觸發邏輯。第十輪討論確認以下設計，
第十一輪實作完成：

1. **`possibly_stale` 只在「曾經有過內容、現在 hash 對不上、但這次沒有 provider 可用」時觸發**：
   附加新 revision，內容複製舊的（比照既有的「系統自動附加 revision 必須是完整 snapshot」規則），
   只改 `status=possibly_stale` 與 `source_hash`/`source_files`（更新為目前的）。**「從沒成功過
   （`unavailable`／`current=None`）+ 這次也沒 provider」不需要額外處理**——`needs_refresh` 對
   `current is None` 或 `status=unavailable` 本來就無條件回傳 `True`，等哪次真的有 provider 可用
   時自然會被抓到需要生成，不必為了「這次仍然沒有內容」這件事另外留一筆什麼都沒變的空白 revision。
2. **`possibly_stale`／`stale` 在 retrieval 端絕不能把舊內容當作可信內容直接提供**：跟 Note 的
   `[STALE]`（顯示舊內容 + 警告標記）刻意不同——semantic summary 是 LLM 生成的長篇散文式描述，不是
   人工/agent 寫的簡短事實記錄，一段「看起來權威、但其實跟不上程式碼」的摘要比起完全沒有摘要更危險
   （agent 可能照單全收，不會像看到「沒有資料」時那樣主動去讀原始碼確認）。因此 canonical
   （`semantic.jsonl`）仍然保留舊內容（稽核用途，且若程式碼被還原成跟舊版一致，不需要重新呼叫 LLM
   就能讓舊內容重新有效），但 Milestone 6 的 retrieval 對 `possibly_stale`/`stale` 的 scope
   **不得回傳舊摘要文字本身**，而是要回傳「這個 scope 的摘要已過期，請直接讀取以下檔案確認目前
   實際內容：`source_files` 清單」這種明確指向真實原始碼、而非舊摘要文字的提示。這個決定現在先
   記錄下來，等 Milestone 6 做 retrieval 時直接照這個做，不需要重新討論。
3. **Provider 健康檢查改成三層，每次 `rune update` 開頭跑一次（不是每個 scope 各自跑），區分「設定
   錯誤」與「執行期問題」**：先前 `_build_semantic_providers` 把「使用者刻意關閉」「忘記設定」
   「打錯字」「暫時性網路問題」全部用同一套「靜默回傳 None」邏輯處理，導致一個打錯字的 model 名稱
   或忘記 export 的 API key 會讓 semantic 永遠悄悄不執行、完全沒有任何提示，直到使用者自己發現。
   實際設計（`rune.core.semantic.provider.check_semantic_health`）：
   ```text
   config.semantic.enabled != true
     → 維持現狀：使用者刻意關閉，靜默跳過，不是錯誤
   config.semantic.enabled == true：
     Step 1（純靜態檢查，不呼叫網路）：
       model 是空字串，或對應 provider 的 API key 環境變數沒設，或
       provider 名稱不是已知的 openrouter/openai
         → 這是設定錯誤，不是暫時性問題：印出明確訊息告訴使用者缺什麼、
           怎麼補（例如「semantic.model is empty」或「OPENROUTER_API_KEY
           is not set」），這次 semantic 整段跳過，但決定性程式碼索引照常
           完成，CLI 對這個狀態回傳 exit code 1（「在初始啟動的時候就
           報錯」，不能讓錯誤悄悄擴大；`model` 空字串跟 API key 沒設一樣
           算設定錯誤，不因為是預設值就特殊放行——見下方「第十二輪修訂」）
     Step 2（僅 Step 1 通過才做，一次輕量連線測試呼叫，`ModelProvider.
     probe()`：max_tokens=1、強制關閉 reasoning，避免 thinking model 把
     這一點點 budget 燒在 reasoning 上而誤判為探測失敗）：
       回應是 rate limit（HTTP 429，`ProviderError.is_rate_limited`）
         → 提示使用者（預期內、非使用者的錯），這次跳過 semantic，CLI
           印出訊息但 exit code 維持 0
           （避免後面每個 scope 都再撞一次同樣的 429，浪費呼叫）
       回應是其他失敗原因
         → retry 一次；仍失敗 → 這次跳過 semantic，但大聲失敗
           （明確錯誤訊息，代表真的有問題：key 錯誤、model 名稱
           provider 端不認得等），決定性索引照常完成，CLI 對這個狀態
           也回傳 exit code 1（「大聲失敗」）
       成功
         → 照現有方式跑每個 scope 的刷新（各自既有的 fallback ladder 不變）
   ```
   `ProviderError` 新增 `status_code`（HTTP 狀態碼，網路層級失敗時為 `None`）與
   `is_rate_limited` 屬性；`update.py` 在 `not full` 時跑這個一次性 precheck；CLI
   的 `update` 指令把 `run_update` 回傳的 `semantic_health_status`/`semantic_health_message`
   從一般 stats k=v 那行拆出來，另外印一行、且依狀態決定 exit code，不會被埋沒在一堆數字裡。

   **第十二輪修訂：`model` 空字串維持算設定錯誤，撤回第十一輪一度做的簡化**。第十一輪實作時，
   因為 `SemanticConfig` 的預設值正是 `enabled=True, model=""`——也就是每一個從沒碰過 semantic
   設定的全新專案都會落在這個狀態——曾經一度把這個狀況重新歸類為等同 `disabled`（靜默跳過），
   理由是若照 Step 1 原字面意思處理，每一個未設定過 semantic 的專案都會在每次 `rune update` 大聲
   失敗。**使用者明確不同意這個簡化，予以撤回**：這個專案完工、被別人部署使用時，理當已經正確
   填入 API 設定，`model` 空字串不該被當成「還沒設定、暫時放行」的特例，而應該跟 API key 沒設一樣
   算設定錯誤——這才是「在初始啟動時就報錯，避免錯誤不斷擴大」這條既有原則該套用的地方，不能因為
   空字串剛好是預設值就例外處理。連帶影響：先前假設「什麼都不設定也能正常 `rune update`」的既有
   測試（例如 CLI 層驗證 working-tree freshness 的測試），現在必須明確在 config.toml 寫
   `semantic.enabled = false` 才能維持「不使用 semantic」這個前提，不能再依賴預設值的巧合。

**第二輪品質複查（外部 review 轉述，本輪新增）修正的 5 個問題**：

1. **SQLite cache 沒有 schema migration，舊版 memory.db 會讓 materialize crash**：`schema.sql`
   全用 `CREATE TABLE IF NOT EXISTS`，任何既有表格新增欄位（例如本 milestone 的
   `semantic_objects.current_revision`）都不會被套用到已存在的舊 memory.db——實測重現：手工造一個
   缺 `current_revision` 欄位的舊 shape memory.db，只要 canonical 有真的 semantic 內容需要
   materialize，`rune update`/`rebuild-cache` 就會丟出未攔截的
   `OperationalError: no such column: current_revision`，且會**持續**發生，先前唯一解法是手動刪除
   `.rune/cache/`。修法：新增 `CACHE_SCHEMA_VERSION` 常數（與 `schema_versions.
   CURRENT_SCHEMA_VERSION` 是兩個獨立概念——後者管的是 canonical JSONL/JSON 每筆紀錄自己的
   `schema_version` 欄位，前者管的是 SQLite 衍生 cache 本身的表格形狀），`rebuild_cache` 開始時比對
   `schema_meta.schema_version` 是否等於這個常數，不符（含全新／古老到連 `schema_meta` 表都沒有的
   檔案）就直接關閉連線、砍掉 `memory.db`（含 `-wal`/`-shm` 側車檔）再重開——這正是
   `rune rebuild-cache` 本來就承諾「隨時可安全丟棄重建」的同一套動作，只是自動觸發，不需要人類自己
   知道要去刪檔案。
2. **Scope 被刪除後重建同名 scope，會永遠卡在 orphaned 出不來**：`orphaned` revision 完整複製前一筆
   的 `source_hash`；scope 被刪除又用完全相同的 member 檔案重建時，`needs_refresh` 拿新算出來的
   hash 跟 orphaned revision 裡的舊 hash 比對，兩者相等就回傳 `False`——即使這時候 provider 完全
   可用，也永遠不會再嘗試刷新，`current` 停在 `orphaned`（依 Decision/Constraint 既有的可見性規則，
   `orphaned` 預設不可見）——已實測重現 `needs_refresh` 回傳 `False`。修法：`needs_refresh` 的
   「無條件視為需要刷新」判斷從只看 `unavailable` 擴大為 `unavailable` 或 `orphaned`——scope
   重新出現這件事本身就是觸發條件，不該還要等 hash 不一致。
3. **`rune update` 的 CLI 說明文字仍寫「Zero LLM calls」**：Milestone 5 之後，`rune update`
   正是會呼叫 LLM 的指令（`rebuild-cache` 才是零 LLM），這句話已經失真。已更新說明文字，明確區分
   兩個指令：`update` 在有設定 `semantic` 時會呼叫 LLM，`rebuild-cache` 永遠不會。
4. **`needs_refresh` 的 docstring 用詞不精確**：原本寫「repeating that bounded attempt on the next
   rune update is intentional」，容易讀成「失敗的 scope 下次一定會重試」，但實際上失敗 revision會把
   `source_hash` 更新為**目前**的值，只要內容沒有再變，下次比對 hash 相等就不會重試，要等內容再變
   才會觸發——這是符合 DATA_MODEL §2.4 revision 表的既有行為，不是 bug，只是文件說法不夠精確。已
   重寫 docstring，明確說明「重試由 source_hash 比對驅動，不是狀態本身」，並點出 `orphaned`
   （見上第 2 點）是唯一「重新出現本身就是觸發條件、不看 hash」的例外。
5. **`compute_source_files` 對已刪除/無法解析的 member 靜默略過，可能讓非空 scope 產出空
   `source_files`**：DATA_MODEL §2.4 原本的措辭「`source_files={}` 只有在 scope 完全沒有 member
   時才合法」，字面上跟一個「members 非空、但每個 member 現在都指向不存在的檔案」的 scope 矛盾——
   已實測重現這個情況確實會發生。評估後**不改變行為**：這種情況下確實沒有真實內容可以雜湊，`{}`
   如實反映現況，且仍正確參與 staleness 判斷（下次真的有 member 存在時會自我修復）；只把
   DATA_MODEL §2.4 的措辭澄清為「沒有任何 member 貢獻出真實檔案」，不是字面上的「members 列表是
   空的」，並在 `compute_source_files` 的 docstring 裡明講這個邊界案例與判斷理由，供未來覆查對照。

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

TypeScript，保持薄。**第十四輪對照官方 `@opencode-ai/plugin` npm 套件（v1.18.29）的真實 TypeScript
型別定義，修正先前幾輪憑官方文件描述、但實際跟型別不完全相符的部分**：早先版本以為
`session.created`/`session.compacted`/`file.edited` 是各自獨立的 hook key，且 `tool.execute.before`
的 input 帶 `directory`/`worktree`/`messageID`——兩者皆錯。真實 API 如下（`Hooks` interface，
`index.d.ts`）：

- **Session 生命週期事件走單一 `event` hook，不是分開的具名 hook**：`event?: (input: { event: Event
  }) => Promise<void>`，`Event` 是 discriminated union（`@opencode-ai/sdk` 定義），本輪需要的三種：
  `{type: "session.created", properties: {info: Session}}`（`Session.directory` 可用）、
  `{type: "session.compacted", properties: {sessionID}}`（**沒有** `directory`）、
  `{type: "session.idle", properties: {sessionID}}`（同樣沒有 `directory`）。Adapter 內用
  `input.event.type` 做 switch 分派，不是註冊三個不同的 hook 函式。
- **`tool.execute.before`/`tool.execute.after` 的 input 只有 `{tool, sessionID, callID}`
  （`after` 多一個 `args`），沒有 `directory`/`worktree`/`messageID`**：這點先前記錄錯誤。**確認的
  因應方式**：CWD 在單一 session 內不會變，所以 `directory`/`worktree` 改成在 `Plugin` 工廠函式收到
  的 `PluginInput`（`(input, options) => Promise<Hooks>` 的 `input` 參數，型別裡本來就有
  `directory`/`worktree`）**只在 plugin 載入時捕捉一次**，之後所有 hook（`event`/
  `tool.execute.before`/`tool.execute.after`）透過閉包重用同一份，不需要每次 hook 呼叫都重新取得。
- **Custom tool 註冊的部分先前記錄正確、本輪確認無需修正**：`tool()` 工廠回傳的 `ToolDefinition`
  執行時拿到的 `ToolContext` 確實帶 `sessionID`/`messageID`/`agent`/`directory`/`worktree`，跟先前
  ARCHITECTURE 記錄的一致。
- **同一套件內還有第二套「v2/effect」plugin API**（`dist/v2/effect/plugin.d.ts`，建構在 `effect`
  函式庫上），與這裡描述的「classic `Hooks` interface」並存。**本輪確認：目標鎖定 classic `Hooks`
  interface**——它是先前 spike 已經驗證過的介面、看起來是文件化的穩定介面，v2/effect 目前未知是否
  為穩定公開介面或仍在實驗階段，不投入時間先探索它。

以下是根據上述真實 API 具體化的設計：

- **Session 開始**（`event` hook 收到 `type: "session.created"`）：依序呼叫 `rune bootstrap --mode
  hard --json` 與 `rune bootstrap --mode soft --json`，分別注入 hard bootstrap（全域 MUST
  constraint + critical global decision，見第 7 節）與 soft bootstrap（project overview、新鮮度
  判斷等）。不在此觸發昂貴的 `rune update`。
- **Session compaction（`event` hook 收到 `type: "session.compacted"`）視為「可能失憶事件」（見第
  7 節）**：只重新呼叫 `rune bootstrap --mode hard --json` 並重新注入，**不**重新送出完整 soft
  bootstrap（避免浪費 token）。理由：不能假設 compaction 產生的摘要保留了所有 MUST constraint 的
  完整內容與權威性，authoritative hard policy 永遠由 rune 重新送出，不依賴 agent 自己的 context
  compression 品質。
- **Constraint delivery 的關鍵 hook 是 `tool.execute.before`，不是 `file.edited`**：
  agent 真正需要的是「碰某個檔案**之前**就看到 constraint」，`file.edited`（`event` hook 收到的另一
  種 `type`）只在檔案已經被改完之後觸發，時機太晚。流程：

  ```text
  tool.execute.before
    → 依 input.tool 決定如何擷取受影響路徑：
        read / edit / write → 直接從 output.args（tool 參數，pre-execution 可變）取得 path
        apply_patch          → 解析 output.args 裡的 patchText 取得受影響路徑
        bash                  → V1 不嘗試精準解析 shell command（見下）
    → 對每個路徑呼叫 `rune scope-for --path ... --json`（對應邏輯在 core，adapter 只呼叫；`--path`
      用 plugin 載入時捕捉的 `directory`，見上）
    → 若該 scope 在本 session 尚未注入過（`active_scope_ids` 快取，規格 §37）：
        注入 scope summary + MUST/SHOULD constraint + 相關 note
    → 放行 tool 執行（本 hook 不阻擋，只注入 context）
  ```

  `bash` 的特殊處理：**V1 不嘗試解析 shell command 語意**（可能間接修改任意數量的檔案，
  精準解析的成本與可靠度都不划算）。改為在 `tool.execute.after` 用 `git diff`（`directory` 一樣沿用
  plugin 載入時捕捉的值）偵測 bash 執行後實際發生的檔案變動，事後才做 scope 對應與（若有新
  scope 被觸及）注入——放棄「執行前精準攔截」，換取「執行後至少不漏掉變動」。
- **Custom tool**：`decision_propose`／`constraint_propose`／`note_add` 註冊為 OpenCode custom
  tool（`Hooks.tool` 欄位），直接呼叫 `rune decision-propose --json` 等 CLI 進入點，帶入
  `ToolContext` 提供的 `sessionID`/`directory`/`worktree` 供 CLI 定位正確的 `.rune/`。

**Hard bootstrap 的 dedup 機制不能沿用 `active_scope_ids`（本輪新增）**：同一 session 可能被 compact
多次，每次 compaction 都必須重新注入 hard bootstrap（見第 7 節），但同一次 compaction 之後、下一次
compaction 之前也必須持續可見。因此 adapter **不維護** `hard_context_generation` 計數器：
`session.created` 與每次 `session.compacted` 都重新取得 hard bootstrap，並在每個正常 LLM request 重繪
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

**`rune scope-for <path> --json` 的輸出格式（第十三輪 spike 確認，先前只點名這個指令、沒有定義確切
JSON 格式）**：一個檔案可能屬於零、一或多個 scope（DATA_MODEL §4），所以回傳一個陣列，成員不存在
任何 scope 是正常結果，不是錯誤。每個 scope 附上它的 semantic summary（Milestone 5，`possibly_stale`
/`stale` 一樣不回傳舊摘要文字本身、改用指向 `source_files` 的提示，跟 `rune search` 完全同一套規則）、
current+visible 的 MUST/SHOULD constraint（INFO severity 不主動注入，見 7.1：INFO 只能透過
`rune search` 查到，不值得每次 scope activation 都佔用 context）、current+visible 的 note：

```json
{
  "path": "app/services.py",
  "scopes": [
    {
      "scope_id": "app",
      "name": "App",
      "description": "",
      "summary": "Handles user account lookups.",
      "summary_status": "fresh",
      "constraints": [
        {"record_id": "no-bare-except", "severity": "MUST", "content": "...", "status": "active", "warning": null}
      ],
      "notes": [
        {"id": "...", "category": "pitfall", "content": "...", "status": "active", "warning": null}
      ]
    }
  ]
}
```

`adapters/opencode/` 的 `src/rune-cli.ts` 是 adapter 唯一允許呼叫 `rune` CLI 的地方（第十三輪 spike
新增 `src/spike.ts` 模擬早期版本設計，已被下方第十六輪的真實 `src/plugin.ts` 取代）。

### 6.1 注入機制（第十六輪，跟使用者確認後定案）——為什麼不是一次性訊息，而是持續重繪的 system block

**`tool.execute.before` 本身無法注入任何文字**：對照真實型別定義（見上方第十五輪修正），這個 hook 的
`output` 只有 `{args: any}`——可變的是「這個 tool 呼叫自己的參數」，不是任何形式的訊息或 context
通道。這代表舊版設計（本輪之前）假設的「`tool.execute.before` 觸發時直接注入一則訊息」在真實 API
下不成立。**型別定義裡唯一找得到的 system-level 注入通道是 `experimental.chat.system.transform`**
（`(input: {sessionID?, model}, output: {system: string[]}) => Promise<void>`，每次呼叫 LLM 前都會
執行一次）——標記 `experimental`，但目前是唯一選項。

跟使用者確認後定案的機制，**不是單純的「一次性 queue，drain 後清空」，而是持續狀態＋每次重繪**：

- **每個 session 維護一份持續狀態**（`RuneSessionContext`，`adapters/opencode/src/rune-context.ts`，
  純邏輯、不依賴任何 OpenCode/CLI 型別，方便獨立單元測試）：
  - `hard`：目前的 hard bootstrap（全域 MUST constraint + critical decision）——`session.created`
    設定一次，**每次 `session.compacted` 整個替換**（不是疊加），確保 compaction 後永遠是最新一份。
  - `activeScopes`：本 session 目前已觸及的 scope 集合，每個 scope 附完整 constraint/note——
    `tool.execute.before`/`tool.execute.after`（bash 事後偵測）觸及新 scope 時加入，**不因
    compaction 清空**（這點比舊版設計更安全：因為每次 LLM 呼叫都重繪整份 context，scope 的
    constraint 不可能被 compaction 悄悄漏掉，不像舊版設計依賴「只注入一次」的訊息可能被 compaction
    摘要掉）。
   - `pendingEvents`：一次性佇列，目前只用來放 soft bootstrap（`session.created` 才 enqueue 一次，
     compaction 不重新 enqueue，符合 §7.3「soft bootstrap 只在 session.created 注入一次」）。真實
     OpenCode 1.18.29 classic Hooks 未公開 request-kind discriminator；V1 因此使用真 host 觀察到的完整
     title system prompt marker `You are a title generator. You output ONLY a thread title. Nothing else.` 作為
     **host compatibility rule**（不是 Rune core semantics）：任一 `output.system` entry 含完整 marker 時，
     視為 internal title-generation，Rune transform 完全 no-op，不注入 hard/scoped/soft，也不 drain pending
     soft。marker 不存在才視為 normal session request，正常 render hard/active scopes，並在 render 成功後
     一次性送出 soft。不得以 first-transform、model identity、時間或短字串猜測；若未來 OpenCode 公開正式
     request discriminator，優先改用正式欄位。
- **`experimental.chat.system.transform` 每次呼叫都重新 render hard bootstrap 與 active scopes**，用一組固定的
  `<!-- rune-context:start/end -->` marker 包裹，`mergeRuneBlock()`
  （同檔案）**原地覆寫**已存在的 marker 區塊，或附加到現有的最後一個 system 字串——**絕不對
   `output.system` 陣列建立第二個元素**（空陣列時建立唯一元素是必要的 host fallback；實際 host 目前傳入
   一個元素）。這保證：同一份 context 重繪 N 次，`system` 陣列長度不變、
  不重複、也不會意外製造出第二個 system 角色訊息。
- **`client.session.prompt({noReply: true})` 不作 V1 的 soft bootstrap delivery**：真實 host 驗證發現它仍會
  產生額外 assistant turn，違反「不觸發模型回覆」的前提。它保留為未接入 helper；若未來
  `experimental.chat.system.transform` 對 hard/scoped context 不可靠或被移除，是否使用或擴大 fallback 仍需
  先重新確認 agent-injection semantics。

`RuneSessionContext`／`mergeRuneBlock()` 是純邏輯，`adapters/opencode/src/rune-context.test.ts`
用 Node 內建 `node:test` 涵蓋：全域 MUST 每次呼叫都在、scoped constraint 跨多次呼叫持續存在、
`mergeRuneBlock` 不會重複累積、不會產生第二個 system 元素、pending event 只出現一次即清空、
overflow 只加警告不丟規則。`adapters/opencode/src/plugin.test.ts` 用一個可替換的 `RuneClient`
介面（不打真實 `rune` CLI）覆蓋同一組行為，外加 compaction 後 hard bootstrap 確實換新、以及不同
session 之間狀態互不外洩。`extractPathsFromToolArgs()`（`tool-paths.ts`）從 `tool.execute.before`
的 `output.args` 猜測 `filePath`/`path`/`file_path` 欄位名稱、`apply_patch` 解析
`*** Add/Update/Delete File:` 標記；真實 OpenCode host 已驗證 `read` 的 `filePath` 與
`apply_patch` 的 `patchText` shape，未知 tool shape 仍維持 fail-open（不注入到錯的路徑）。

### 6.2 Adapter/Core protocol compatibility——`protocol_version`

**問題**：Python `rune` CLI 是 machine-level 安裝一次（見第 14 節），OpenCode adapter 是獨立的 npm
套件，兩者各自升級、版本號彼此無關。若 adapter 假設某個 `--json` 輸出一定長某個形狀，而使用者升級了
其中一邊，schema 不相容時**絕不能靜默 best-effort 解析**——那等於用錯誤或不完整的資料驅動 agent
行為，比直接失敗更危險。

**設計決議**：

- 所有面向 adapter（未來也包含 MCP，見第 8 節）的 `--json` 輸出，其頂層物件應包含
  `protocol_version: int`，例如：
  ```json
  {
    "protocol_version": 1,
    ...
  }
  ```
- **`protocol_version` 是 adapter/core 介面版本，不是 RepoRune/`rune` 套件本身的版本號**——套件版本
  可以頻繁變動（bug fix、功能新增），只要輸出的 JSON 形狀沒變，`protocol_version` 就不動；只有當
  既有欄位的意義、必要性、或形狀改變到「舊版 adapter 解析新版輸出會出錯或誤解」的程度，才遞增。這條
  跟現有 `CACHE_SCHEMA_VERSION`（materialize.py，管的是 SQLite 衍生 cache 形狀）、canonical
  `schema_version`（管 JSONL 檔案形狀）是同一種精神在不同層級的應用：**「形狀變了就換版號，讀者版號
  不符就拒絕、絕不假裝相容」**，不是本輪發明新原則，只是把既有精神延伸到 adapter/core 這條邊界。
- **adapter 遇到不支援的 `protocol_version` 必須大聲失敗**，錯誤訊息需明確指出「core 與 adapter
  版本不相容，需要升級其中一方」，不得吞掉錯誤或嘗試用舊邏輯硬解析新形狀（或反之）。這與
  ARCHITECTURE 其餘地方反覆出現的「大聲失敗優於靜默錯誤」原則（例如第 7.7 節 hard bootstrap
  overflow、`CanonicalConflictError`）完全一致。
- **已實作為 version 1**：所有現有 adapter-facing `--json` output 都是頂層 object 並包含此欄位；
   adapter 缺失或不相容時明確拒絕解析。未來改變 JSON contract 時依上述規則遞增版本。

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
session.created   → 取得 hard bootstrap；每個正常 LLM request 重繪
session.compacted → 重新取得 hard bootstrap；每個正常 LLM request 重繪
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
DATA_MODEL §8；V1 不為此預先設計。**第十七輪確認：git worktree 是這條限制最主要的真實觸發場景**
（同一 repo 多個 worktree 平行工作，本質上就是「多個 branch 各自推進」的具體案例），第 16 節記錄
worktree／多 agent 協作的完整 workflow，但這裡的 invariant 本身不因此改變——worktree 場景不觸發任何
新的 schema 或 revision 機制設計，仍然是「偵測到衝突就拒絕、要求人類解決」。

## 13. 測試策略（另見 IMPLEMENTATION_PLAN.md 測試計畫章節）
兩個 fixture repo（`tests/integration/fixtures/ts-simple`、`python-simple`）驅動 init -> update ->
檔案變更 -> rebuild-cache 全流程的整合測試。單元測試涵蓋 hashing、canonical 解析、revision 選取、
生命週期轉換、TTL、scope membership、staleness（含 Decision/Constraint 與 semantic 兩套獨立規則）、
redaction、FTS 索引（規格 §65）。**Windows 相容性不獨立立項，併入既有整合測試**（本輪確認降低優先
級）：fixture repo 內含 Unicode 路徑、中文檔名、CRLF 換行的檔案，驗證 hashing/atomic-write/git
subprocess（UTF-8 編碼）在這些輸入下行為正確。

## 14. 部署模型與安裝單位（第十七輪新增，純設計，本輪不改任何程式碼）

Milestone 7 完工後才第一次需要正式回答「這個工具實際上怎麼裝到使用者機器上」，本節記錄確認結果。

### 14.1 Repo 結構與 package 邊界

`rune` core（Python）與 `adapters/opencode`（TypeScript）放在同一個 Git repository，但是**獨立的
installation unit**——這是既有第 2 節目錄結構早就長這樣（`src/rune/` 與 `adapters/opencode/` 平行），
本輪只是把「為什麼可以同 repo 不同安裝單位」正式寫下來，不是新的目錄結構決定：

- Python 端：`rune` CLI／`RepoRune` package（`pyproject.toml`，`pip install`／未來可能的
  `pipx install`）。
- TypeScript 端：OpenCode adapter（`adapters/opencode/package.json`，npm 套件）。

**具體 package 名稱本輪不鎖死**——`pyproject.toml`／`package.json` 目前的 `name` 欄位（`rune`、
`rune-opencode-adapter`）是開發階段用的內部名稱，正式發布時的公開套件名稱是獨立的產品/命名決策，
不屬於這輪的架構討論範圍，等真的要發布時再確認。

### 14.2 Machine-level install once，per-repo `rune init`

**Rune 應用程式本身（`rune` CLI）是機器層級安裝一次，不是每個 project 各裝一份**：

```text
User machine
├─ rune executable / RepoRune package        （裝一次）
├─ OpenCode Rune adapter                      （裝一次）
├─ repo A/.rune/                              （per-repo state）
├─ repo B/.rune/
└─ repo C/.rune/
```

每個 project 只需要 **project-local 的 Rune state**：跑 `rune init` 建立 `.rune/`，canonical
memory／`config.toml`／local cache 全部落在該 repo 目錄下（既有第 2、11 節已經是這個設計，本輪只是
明確標註「安裝」與「初始化」是兩個不同層級的操作，不要混為一談）：

> Install Rune once. Initialize Rune per repository.

### 14.3 OpenCode plugin 的偵測與啟用規則

OpenCode plugin **也應該只安裝一次**，不是每個 project 各自複製一份 plugin 程式碼。Plugin 啟動
（`Plugin` 工廠函式收到 `PluginInput`，見第 6.1 節）時：

1. 從 OpenCode 提供的 `directory`/`worktree` 判斷所在的 repository。
2. 偵測該 repository 是否存在 `.rune/`。
3. 存在 → 啟用 Rune integration（第 6、7 節描述的整套 hook 行為）。
4. 不存在 → **silent no-op**——不報錯、不提示、不影響 OpenCode 其他功能，就當作這個 repo 沒有裝
   Rune 一樣正常運作。

**Plugin 絕對不可自動執行 `rune init`。** `rune init` 是使用者明確選擇導入 Rune governance（Decision/
Constraint/Note 治理、scope 系統）的動作，不是「plugin 偵測到沒有 `.rune/` 就自動幫你建一個」——這
會在使用者完全不知情的狀況下，把一個治理系統的初始狀態悄悄種進他們的 repo，違反本專案處處強調的
「human-in-the-loop、不自動做有持久後果的決定」精神（同一原則見第 7.9 節 Global MUST 一樣要走
propose/approve、第 4.4 節 clustering 永遠只建議）。

### 14.4 CLI discovery

Adapter 呼叫 `rune` CLI 的方式（`adapters/opencode/src/rune-cli.ts`，第 6 節）：

- **預設從 PATH 呼叫 `rune`**——對應「機器層級安裝一次」的預期部署方式。
- **保留 `RUNE_CLI_PATH` 環境變數覆寫**——主要供開發、測試、CI，或還沒把 `rune` 放進全域 PATH 的
  特殊部署情境使用，不是正式部署的主要路徑。

這是既有實作已經做的事（`rune-cli.ts` 從 Milestone 7 spike 就這樣寫），本輪只是把它記錄成正式的
部署設計決策，不是新行為。

### 14.5 V1 不引入 daemon；MCP server 的定位

**V1 明確不引入 daemon/server 常駐程序**：OpenCode plugin 呼叫 `rune` CLI 的方式就是每次
subprocess + `--json` 輸出（見第 6 節），沒有長駐 process、沒有 IPC、沒有需要管理生命週期的背景
服務。這保持了「adapter 唯一合法介面是 CLI 的 `--json` 輸出」（第 6 節既有規則）的簡單性，也避免
daemon 帶來的額外複雜度（多 client 併發存取同一份 state、daemon crash 恢復、版本升級時如何優雅
重啟等）在 V1 完全不需要處理。

第 8 節的 MCP server（Milestone 8）**保留作為未來 Codex／Claude Code／其他 generic MCP client 的
整合手段**，但**不是 OpenCode V1 整合的必要依賴**——OpenCode 走 CLI subprocess，不透過 MCP。MCP
server 本身要不要以 daemon 形式常駐，是 Milestone 8 開工時才需要回答的問題，不在本輪討論範圍。

## 15. Adapter/Core Protocol Compatibility

（`protocol_version` 的完整設計已併入第 6.2 節，緊鄰它所規範的 adapter/core `--json` 介面邊界，
避免同一個契約分散在文件兩個不相鄰的地方。此處保留章節編號僅作為目錄索引用途。）

## 16. Git Worktree 與多 Agent 協作模型（第十七輪新增，純設計，本輪不改任何程式碼）

### 16.1 為什麼需要這節：worktree 是「多個 agent 平行工作在同一個 repo」的常見部署形態

```text
main
├─ worktree A → agent A / issue A
├─ worktree B → agent B / issue B
└─ worktree C → agent C / issue C
```

每個 worktree 會各自 checkout 一份完整的 `.rune/` canonical 檔案（因為它們是 git-tracked 檔案，
跟著 branch 走）。這一節記錄「多個 worktree 平行工作、最終需要 merge」這個場景下，Rune 的哪些部分
該怎麼表現——**這不是新的儲存機制或新的 revision schema，是既有機制（canonical vs derived 分離、
單一寫入者假設、既有的 incremental scope 併入規則）在多 worktree 場景下的行為說明與 workflow
建議**。

### 16.2 Derived 與 canonical 狀態的 per-worktree 行為

- **`.rune/cache/`（`memory.db`）、`.rune/logs/` 等 derived/local state**：per-worktree、
  gitignored（既有規則，見第 2、11 節）、**不參與 git merge**、需要時直接 `rune rebuild-cache`
  重建即可——這本來就是「SQLite 是完全衍生的 cache」這條既有核心原則（第 3、7 節）在多 worktree
  情境下的自然結果，不需要為 worktree 場景另外設計同步機制。
- **Canonical memory**（`scopes.json`、`decisions.jsonl`、`constraints.jsonl`、`notes.jsonl` 等）：
  跟著 branch/worktree，**會參與正常的 git merge**。不同 logical record 之間的 append-only 變更，
  原則上可以直接保留為 union（git 本身的 line-based merge 通常就能處理，因為每個 record 是獨立的
  JSONL 行）。
- **同一個 logical record（同一 `record_id`/`id`）的 revision 在不同 worktree 各自往下推進、merge
  後撞號**：這正是第 12 節、DATA_MODEL §8 已經定義的「單一寫入者假設」衝突場景，**V1 不自動
  resolve**——現有的 duplicate logical revision 偵測與 single-writer 保護機制直接適用，不需要為
  worktree 新增任何邏輯：偵測到衝突就大聲失敗，由人類或下方 16.4 節的 integration session 決定
  如何 reconcile。**V1 本輪依然不把 `revision` 改成 ULID + parent revision DAG**——那是既有記錄
  在案的 future/V2 候選方向（DATA_MODEL §8），worktree 這個新場景不構成「現在就該做」的理由。

### 16.3 Parallel worktree 的 authoritative-memory workflow：允許與不允許的操作

**平行工作的 agent worktree 可以**：
- 讀取正式的 Rune memory（`rune search`／`rune check`／`rune scope-for`／`rune bootstrap`）。
- 修改程式碼。
- 建立 Note（無需核准，第 4.6 節既有語意，本輪不變）。
- 建立 Decision/Constraint proposal（`propose`，仍待核准，第 4.6、7.9 節既有語意，本輪不變）。

**平行工作的 agent worktree 原則上不應該**：
- 同時批准（`approve`）同一個 Decision/Constraint 的 authoritative revision——批准是產生 canonical
  真相的動作，多個 worktree 各自批准同一筆會直接製造 16.2 節描述的 revision 衝突。
- 把 branch-local 的 semantic refresh（`rune update` 觸發的 scope summary 重新生成）當成「merge
  之後的最終權威 semantic state」——branch-local 的程式碼還沒有反映 merge 後的真實狀態，這份
  summary 只對這個 worktree 自己當下的程式碼有效。

### 16.4 Integration worktree／integration session 的角色

負責把平行工作收斂成一份權威狀態：

- Merge 程式碼。
- Resolve canonical 衝突（16.2 節描述的 revision 撞號，需要人類或人類授權的 integration session
  介入，不是自動化步驟）。
- Review／approve 治理 proposal（Decision/Constraint 的批准動作集中在這裡執行，避免 16.3 節提到的
  平行批准衝突）。
- 執行最終的 `rune update`（在 merge 後的程式碼上重新跑一次決定性索引）。
- 執行最終的 semantic refresh（在 merge 後的程式碼上重新生成過期的 scope summary，取代任何
  branch-local 的 semantic 結果）。
- 執行最終的 scope reconciliation（見 §4.4 新增的 Scope Membership Reconciliation 規則，
  16.5-16.6 節）。

### 16.5 Merge reconciliation 的 provenance 原則（架構層正式原則）

> **Merge reconciliation follows provenance.**

即：判斷「merge 之後這份狀態該怎麼處理」的依據，是這份狀態的來源是什麼，而不是「AI 現在看著 merge
後的程式碼、覺得應該長怎樣」。具體分類：

**可以在 merge 後從 merged repository 重新產生**（derived/reproducible state，既有機制直接適用，
不需要為 merge 場景另外設計）：
- File index、symbol index、import/reference graph（`core.index`，第 4.1、4.2 節既有機制）。
- 衍生 SQLite cache（`rune rebuild-cache`，完全衍生，第 3、7 節既有原則）。
- Semantic summary（`core.semantic`，第 4.5 節既有機制——但僅限「重新生成」本身允許，*寫入*仍要走
  既有的 staleness/refresh 邏輯，不是 merge 觸發就无条件整批重跑）。
- Auto-inferred scope membership——**但受 §4.4 新增的 Scope Membership Reconciliation 規則限制**
  （16.6 節），不是「可以隨便重新推導」。

**不可以由 AI 根據 merge 後的程式碼自動重新發明**（human-authoritative state，只能 merge 或明確
reconcile，絕不能被「根據現在的 code 重新生成一份新的權威版本」取代）：
- 已核准的 Decision。
- 已核准的 Constraint。
- Human-authoritative 的 Scope 定義（`locked` scope、`ScopeSource.human` 的 scope 本身）。
- `locked` membership。
- Human-confirmed 的 membership（即使該 scope 本身不是 `locked`，個別由人類確認過的 membership
  一樣受保護，見 16.6 節）。

**AI 對治理層的 merge 衝突可以提 reconciliation proposal（例如「這個 Decision 在兩個 worktree
各自被修改，建議合併成……」），但不能自己成為 authoritative**——批准權仍在人類手上，這與第 7.9 節
「Global MUST 一樣要走 propose/approve」是同一條原則的延伸適用。

### 16.6 Scope membership 在 merge 後的 reconciliation：適用 §4.4 的規則，不是重新設計

Merge 後的 scope membership reconciliation，完整規則寫在第 4.4 節新增的「Scope Membership
Reconciliation」小節（untouched region frozen、auto-apply 限定 high-confidence 且僅限 changed
set、`locked`/human-confirmed membership 絕對保護、large churn guardrail、AUTO/KEEP/REVIEW/BROKEN
四種結果分類），這裡不重複，只強調一點：**merge 後的 reconciliation 用的是同一套規則，不是給
worktree 場景另外發明一套「合併後重新分類全部 scope」的邏輯**——這正是 §4.4 開頭「Rune 不得在正常
update／merge reconciliation 時重新理解整個 repository」這句話特別把 merge reconciliation 也點名
在內的原因。

**若目前程式已經會在一般 `rune update` 對「新增檔案」自動寫 `scopes.json`（第 4.4 節既有的
incremental 自動併入規則，Milestone 4 已實作）**：這個行為繼續保留，不因為本輪新增 worktree/merge
討論就改變或停用；只是現在明確補上一句——**multi-worktree 情境下，真正權威的 reconciliation 以
merged tree 為準**，也就是說：branch-local 的 `rune update` 若在合併前就已經對某個新檔案觸發了
incremental 自動併入（因為當時只看得到 branch-local 的 import graph），這筆自動寫入的
`scopes.json` 變更會隨著 git merge 一起帶進 integration worktree；**integration reconciliation
仍然需要在 merged state 上重新驗證**——如果 merge 後這個檔案的 import graph 因為其他 worktree 帶進
的變更而不再滿足「恰好命中單一 unlocked scope」的條件（例如 merge 後多了一個候選 scope），這筆
membership 就不再自動視為有效，需要落回人類審查，不是「因為之前 branch-local 已經自動寫過了，
merge 後就自動維持」。

**這段落後就已經明講了「merge-affected auto-inferred membership revalidation」是需要的**——第
一輪 Milestone 9 實作原本以「沒有 per-membership provenance schema，做這件事等於重新 clustering
全 repo」為由整段沒做（見下方 §17 第 2 點的舊版記錄），事後複查發現這個理由不成立：不需要 schema
層的 provenance，直接讀 `scopes.json` 這個 git 追蹤檔案在 `since` ref（merge base／merge 前任一
commit）當時的內容，跟現在的 canonical 內容做差集，就能精確界定「這次 merge 本身引入了哪些
membership」——人類另外用 `rune scope edit`/`create` 加的東西是獨立一次 commit，天然不會被算進
「`since` 到 `HEAD` 之間」這個差集裡，不需要另外分辨「這筆是不是自動寫的」。**已實作為
`rune scope reconcile --since <ref>`**（`core.scopes.reconcile._merge_affected_entries`）：

- 範圍限定：只處理「現在是某個 unlocked、`source != human` scope 成員，但在 `since` 當時的
  `scopes.json` 裡還不是」的 file membership（`locked`/`source == human` 用的是同一個
  `_is_protected` 保護代理指標，跟 BROKEN 分類共用）。
- 對每筆這樣的 membership，用目前（merged 後）的 import graph 重跑
  `high_confidence_import_candidates`；還是唯一命中同一個 scope 就不產生任何輸出；命中零個、
  多個、或命中別的單一 scope，一律產生 `REVIEW`（附上目前的候選 scope id），**絕不自動改寫或
  搬移**——這個函式本身沒有寫入路徑，`reconcile()` 既有的 AUTO-only 寫入邏輯完全不受影響。
- 這是選擇性參數（`--since` 不給就完全不跑這段邏輯，行為與之前一致），因為 reconcile 本身無法在事後
  自動判斷「這是不是一次 merge」（`git merge` 完成後 `.git/MERGE_HEAD` 就消失了）——猜錯 base ref
  會漏掉真正的 merge-affected case，或誤把不相關的歷史當成 merge 影響，所以要求呼叫者明確提供
  `since`。

## 17. Milestone 9 Scope Governance backlog（索引用途）

Scope reconciliation 的設計已定，**Milestone 9 已完成實作**；完整細節見 §4.4（含新增的
「Reconciliation CLI 與 merge/integration worktree 使用流程」小節）與 IMPLEMENTATION_PLAN.md 的
Milestone 9 決策記錄：

1. **`rune scope reconcile`（含 `--full`）與 AUTO/KEEP/REVIEW/BROKEN 輸出格式**：已實作
   （`core.scopes.reconcile`、`scope_app` 底下的 `reconcile` 子命令），large-churn threshold 鎖定為
   絕對數量 20（`config.scopes.reconcile_large_churn_threshold`，理由見 IMPLEMENTATION_PLAN.md 第
   168 條）。
2. **Scope membership 的 per-membership provenance schema**（`ScopeMembership` 概念，第 4.4/16.6
   節提及，完整內容見 DATA_MODEL.md §9）**仍是 future/V2，沒有修改** `Scope`/`ScopeMembers`
   schema。**但「merge 後重新驗證既有 membership」這件事本身已經實作**（`rune scope reconcile
   --since <ref>`，見上方 §16.6 新增段落）——複查後發現不需要 schema 層的 provenance 就能做到：
   直接用 git 讀 `scopes.json` 在 `since` 當時的內容跟現在的差集，界定「這次 merge 引入了哪些
   membership」，範圍精確，不是重新 clustering 全 repo。IMPLEMENTATION_PLAN.md 第 170 條記錄的
   舊版「因為缺 provenance schema 所以不做」判斷已撤回，見同檔案新增的決策記錄。
