# RepoRune (rune) — 交接文件

最後更新：2026-09-06，Milestone 4 已實作、完成驗證、commit `edd87d8`。之後兩輪自我複查／使用者轉述
外部 finding 各修正若干問題：`rune scope suggest` 崩潰 bug（IMPLEMENTATION_PLAN.md 第 50 條）；接著
使用者轉述 6 條 Milestone 3／4 finding，逐條重現後全部確認為真並修正（第九輪修訂，見 IMPLEMENTATION_
PLAN.md 第 51-56 條）：unchanged caller 的 reference edge 不會重新解析、Python/TS 的
qualified/generic 繼承 reference 被整個丟棄、extends/implements target 沒有 kind 限制導致誤配、
scope 自動併入在 `rebuild_cache` 之前就寫入 canonical `scopes.json`（破壞 all-or-nothing 交易）、
真實 repo 品質實驗第二個樣本改用真正中型的 `honeypot-discord-bot`、補齊 locked scope 的端對端測試。
121 個測試全綠，`ruff check` 全綠。Milestone 3、4 目前已重新確認完成，沒有已知未修的問題。

**Milestone 5（Semantic worker）已實作並通過測試**（`core.semantic.{provider,worker,validation,
redaction}`，159 個測試全綠）。開工前先修正 `ScopeSummary` 生成失敗語意的自相矛盾（新增 `revision`
欄位，比照 Decision/Constraint/Note，`last_error` 收斂為清洗過的分類字串，原始錯誤另存不進 git 的
`.rune/logs/semantic.log`——見 IMPLEMENTATION_PLAN.md 第 57-58 條、DATA_MODEL.md §2.4、
ARCHITECTURE.md §4.5）。`rune update` 的接線比照 Milestone 4 的 `scopes_override` 模式，`semantic.
jsonl` 的 append 延後到 `rebuild_cache` 成功之後才執行，維持 all-or-nothing。**用使用者提供的
OpenRouter API key 對 `qwen/qwen3.8-flash` 做過真實端到端驗證**（不只 mock）：發現這個模型的
`reasoning` 欄位會吃掉 `max_tokens`、真的踩過一次上游 429 rate-limit 並確認 fallback ladder 正確
處理、真的讓模型 hallucinate 出不存在的 symbol 並確認 strip 邏輯正確踢掉。細節見 IMPLEMENTATION_
PLAN.md 第 59-61 條，含 3 項刻意延後的已知未完成項目（`possibly_stale` 沒有觸發邏輯、無退避的
無限重試、CLI 輸出六項 metrics 沒有端對端測試——都不是遺漏，是誠實記錄的取捨）。

**同一輪追加（第 62 條）**：使用者問「思考強度、模型這些設定放在哪」才發現 `max_tokens` 沒有進
`config.toml`（寫死在 `worker.py`），而且完全沒有管道能關掉/調低 reasoning 模型的思考量。已補
`SemanticConfig.max_tokens` 與 `SemanticConfig.reasoning`（`ReasoningConfig`：`enabled`/`effort`/
`max_tokens`）。同樣先用真實 API 驗證 OpenRouter 的 `reasoning` request 欄位真的有效
（`{"enabled":false}` 讓 `qwen/qwen3.8-flash` 的 reasoning_tokens 從 36 降到 0）才接線，最後用
`reasoning.enabled=False` 端到端跑過一次真實 API（`output_tokens=1`，沒有思考開銷）。

**`max_tokens` 預設值改為 16000**（使用者要求，原本 4000 只是暫定值），同步更新 `worker.py` 兩處
函式參數預設值與對應測試。

**使用者接著要求對 Milestone 5 做品質複查（第十二輪修訂），逐條重現後發現並修正 3 個問題**（見
IMPLEMENTATION_PLAN.md 第 64-66 條）：`reference_strip_rate` 指標在模型回傳非 list 的
`entry_points`/`important_symbols` 時會被 `len()` 對字串算成字元數而嚴重灌水；provider 層級的真正
失敗（HTTP 429／網路逾時）被誤當成「有回應但驗證失敗」，把原始錯誤訊息整段塞進下一次呼叫的
repair prompt——**這不是假設情境，第 60 條記錄的真實 API 驗證裡實際發生過**，只是那次模型剛好夠
聰明沒被搞混；同一根因也讓 `metrics.provider_error` 誤把「回應到了但不是合法 JSON」算成 provider
本身不可靠。修復第二條時第一版改法過寬，意外讓既有測試失敗，重新精確區分「provider 層級失敗」與
「回應到了但格式錯」才修好——連自己剛寫的修法都要重新驗證。162 個測試全綠，`ruff check` 全綠。

**使用者接著轉述另一份針對 Milestone 5 的 code review，10 條 finding，逐條重現後全部確認為真
（第十三輪修訂，見 IMPLEMENTATION_PLAN.md 第 67-76 條，細節同步記錄在 ARCHITECTURE.md §4.5、
DATA_MODEL.md §2.4/§5）**。9 條直接修，1 條依使用者要求記錄但不修：

- **修了的（重要性由高到低）**：`rune rebuild-cache` 會誤觸發 LLM 呼叫（違反自己宣稱的 zero LLM
  calls）；刪除已有 summary 的 scope 會讓 materialize 因 FK violation **持續** crash（新增
  `SemanticStatus.orphaned`，完全比照 Decision/Constraint 既有的 orphaned 語意）；多 scope 同時
  刷新時 canonical 的 append 不是原子操作（新增 `append_jsonl_many`）；provider request 缺少
  `response_format: {"type":"json_object"}`（先用真實 API 驗證過才接線）；redaction 沒有尊重
  `config.security.redact_secrets` 開關、且遺漏 `dependencies` 欄位；schema 驗證把不合法型別
  （例如 dict 型的 `purpose`）默默轉換而非拒絕；fallback model 產生的內容被誤標成 primary model；
  prompt 只有 symbol metadata、沒有實際程式碼（新增 `repo_root` 參數，利用既有的
  `start_line`/`end_line` 截取程式碼片段，200 行截斷保護）；六項 run-level metrics 沒有持久化
  （新增 SQLite `semantic_run_metrics` 表，不受 `rebuild_cache` 清空重建影響）。
- **記錄但不修的**：provider 不可用時（沒 API key／`semantic.enabled=false`／預算用完），已變 stale
  的 scope 仍顯示 fresh，`possibly_stale` 沒有觸發邏輯——使用者明確要求先記錄、留待後續討論設計
  （例如要不要在無 provider 時也附加一筆不呼叫 LLM 的 `possibly_stale` revision），不要自己選方案
  動手。

新增 12 個回歸測試（4 個既有測試從 `full=True` 改為 `full=False`，因為修好 bug 後它們原本用來
「意外觸發」semantic refresh 的路徑被關掉了；8 個全新測試），每個都驗證過修法前確實會失敗。
173 個測試全綠，`ruff check` 全綠。

**使用者又轉述第二份針對 Milestone 5 的 code review，5 條 finding，逐條重現後全部確認為真並修正
（第十四輪修訂，見 IMPLEMENTATION_PLAN.md 第 77-81 條）**：
- **中·SQLite cache 沒有 schema migration，舊版 memory.db 會讓 materialize 持續 crash**：
  `schema.sql` 全用 `CREATE TABLE IF NOT EXISTS`，新增欄位（例如 M5 的 `current_revision`）不會
  套用到已存在的舊 memory.db——實測重現一支手工造的舊 shape memory.db 確實讓 `rune update` 丟出
  `OperationalError: no such column`，且持續發生。新增 `CACHE_SCHEMA_VERSION` 機制，`rebuild_cache`
  偵測到 `schema_meta` 版本不符就自動丟棄整個 memory.db 重建（跟 `rune rebuild-cache` 本來就承諾的
  「隨時可安全丟棄重建」是同一套邏輯，只是自動觸發）。
- **中·Scope 被刪除後重建同名 scope，永遠卡在 orphaned 出不來**：`needs_refresh` 原本只把
  `unavailable` 視為無條件需要刷新，沒把 `orphaned` 算進去——重建的 scope 若 member 檔案完全沒變，
  hash 比對會相等，永遠不會再嘗試刷新。已實測重現、已修正（`orphaned` 現在跟 `unavailable` 一樣
  無條件觸發刷新）。
- **低（純文件修正，行為本來就正確）**：`rune update` 的 CLI 說明文字仍寫「Zero LLM calls」（M5 後
  它正是會呼叫 LLM 的指令）；`needs_refresh` 的 docstring 用詞容易誤導成「失敗一定會重試」，實際上
  由 source_hash 比對驅動；`compute_source_files` 對已刪除 member 靜默略過可能讓非空 scope 產出空
  `source_files`，評估後判斷這是正確行為（如實反映現況、仍正確參與 staleness 判斷），只澄清
  DATA_MODEL.md 的措辭。

175 個測試全綠，`ruff check` 全綠。

**第十五輪修訂（確認設計）**：針對第 70 條記錄的「provider 不可用時 possibly_stale 沒有觸發邏輯」
缺口，跟使用者討論後定案三項設計，當輪只寫文件、刻意不動手——使用者要求先把決議完整寫進文件，
下一個 session 開工前先讀，再開始寫程式碼。

**第十六輪修訂（完成第十五輪確認設計的實作，見 IMPLEMENTATION_PLAN.md 第 85-90 條）**：

1. **`possibly_stale` 觸發邏輯已實作**（`mark_possibly_stale`，`rune.core.semantic.worker`）：
   只在「曾有內容、hash 對不上、這次沒 provider」時觸發，附加新 revision，複製前一筆 current
   revision 內容，只改 `status=possibly_stale`／`source_hash`／`source_files`／`generated_at`。
   「從沒成功過」的 scope 不需要額外處理——`needs_refresh` 對 `current=None`／`unavailable` 本來
   就無條件回傳 `True`。
2. **Provider 健康檢查已實作為三層**（`check_semantic_health`，`rune.core.semantic.provider`），
   每次 `rune update` 開頭跑一次（`not full` 時）：`semantic.enabled != true` → 靜默跳過；Step 1
   （純靜態，不連網）`model` 空字串、API key 環境變數沒設、或 provider 名稱不認得 → **設定錯誤**，
   印出訊息，CLI exit code 1；Step 2（一次輕量連線測試，`ModelProvider.probe()`）HTTP 429 → 提示
   使用者，exit code 0；其他失敗 retry 一次仍失敗 → **大聲失敗**，exit code 1；成功 → 照現有方式
   刷新每個 scope。`ProviderError` 新增 `status_code`/`is_rate_limited`；CLI 的 `update` 指令把
   健康狀態訊息從一般 stats 那行拆出來單獨印出。

新增 17 個測試，每個都用 `git stash` 只還原 `src/` 改動、保留新測試，確認修法前確實會失敗才算數。
192 個測試全綠，`ruff check` 全綠。

**第十七輪修訂（使用者要求撤回一處實作時的簡化）**：第十六輪實作時，因為 `SemanticConfig` 預設值
正是 `enabled=True, model=""`（每個全新專案的預設狀態），一度把「model 空字串」重新歸類為等同
`disabled`（靜默跳過），避免所有未設定過 semantic 的專案每次 `rune update` 都大聲失敗。**使用者
明確不同意**：這個專案完工、被別人部署使用時，理當已經正確填入 API 設定，`model` 空字串不該因為
剛好是預設值就特殊放行，應該跟 API key 沒設一樣算設定錯誤——已撤回這個簡化，恢復成第十五輪原始
措辭（`model` 空字串一律算 `config_error`）。連帶修正：既有測試
`test_update_then_status_reports_fresh_again`（測 working-tree freshness，跟 semantic 無關）
改成明確在 config.toml 寫 `semantic.enabled = false`，不再依賴「什麼都不設也能正常 update」這個
現在已不成立的假設；`tests/unit/test_semantic.py` 對應的健康檢查測試也改回預期 `config_error`。
192 個測試維持全綠，`ruff check` 全綠。細節見 IMPLEMENTATION_PLAN.md 第十七輪修訂。

retrieval 端「`possibly_stale`/`stale` 不顯示舊摘要、改指向 `source_files`」的規則（第十五輪決議
第 2 點）尚未實作，屬於 Milestone 6 範圍，留到那時再做——見下方「立刻可以做的下一步」。

## 專案是什麼

RepoRune（CLI/套件名：`rune`）= *Repository Understanding & Navigation Engine*。
一個獨立於任何 coding agent 的 repository intelligence 系統，目標是讓 agent 在新 session／
context compaction 後不需要重新探索整個 repo，就能拿到：決定性的程式碼結構（檔案/symbol/import/
reference）、持久化的語意理解、持久化的治理紀錄（Decision/Constraint）、會過期的工作筆記
（Note），以及一套「不需要 agent 自己想到要查」的主動投遞機制（Global MUST Constraint hard
bootstrap）。

**GitHub**：https://github.com/woodengrass/RepoRune（`main` branch，目前只有一條線性歷史，8 個
commit，全部已 push，沒有未提交的變更）。

## 必看的三份設計文件（優先順序：先讀這三份，再看程式碼）

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — 系統設計、模組職責、資料流、package 邊界。目前**第十二輪
  修訂**。
- [`DATA_MODEL.md`](DATA_MODEL.md) — 所有 canonical Pydantic model、SQLite schema、revision
  lifecycle 規則。目前**第十一輪修訂**（這個輪數指的是 DATA_MODEL 自己的版號，跟 ARCHITECTURE/
  IMPLEMENTATION_PLAN 的輪數不是同一套計數，不要混淆——三份文件各自獨立記錄自己的修訂輪次）。
- [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) — 8 個 Milestone 的交付項目、驗收標準、
  **文末的「設計決策記錄」按輪次列出每一次修正**，這是最重要的部分：每個 milestone 完成後都有
  「自我複查」或「外部 code review」段落，記錄實際發現並修好的 bug、為什麼修、怎麼驗證的。**這份
  文件是這個專案最重要的「為什麼會長這樣」的歷史記錄，比起單純看 git log 更能理解設計取捨。**

這三份文件**本身就是這個專案的治理機制**：任何會改變 canonical schema、scope model、
Decision/Constraint 語意、staleness 語意、agent-injection 語意的變更，都要求先更新文件、記錄決策
理由，才能動程式碼。**接手的人／agent 請延續這個習慣**，不要跳過文件直接改 code。

## 目前進度：Milestone 1-4 完成並通過測試

Milestone 4 已實作 `rune.core.scopes.{model,heuristics,clustering}` 與 `rune scope`
CLI（`list`/`create`/`edit`/`delete`/`lock`/`unlock`/`suggest`）。`networkx` 是新增的 runtime dependency。
候選只在 `rune scope suggest` 互動期間存在；接受後才寫入 `scopes.json`，拒絕或結束 CLI 不留下副作用。
`rune update` 對新增檔案實作唯一的無人確認寫入：只接受指向單一 unlocked scope member 的
`imports`、`confidence=1.0` edge。詳見 IMPLEMENTATION_PLAN.md 第八輪實作記錄第 48-49 條。

### Milestone 4 已採用的三個設計決策（第七輪修訂，見 ARCHITECTURE.md §4.4、
IMPLEMENTATION_PLAN.md 設計決策記錄第 45-47 條）

1. **候選 scope（heuristic + clustering）不持久化，純一次性 CLI 互動**：`rune scope suggest` 當場
   計算候選、當場印出、當場人類 `[y]es/[n]o` 確認，才會寫成 `source=model` 的 scope；不新增
   `scope_candidates.jsonl` 之類的 canonical 檔案，關掉終端候選就消失，下次重跑重新計算即可。這與
   Decision/Constraint 的 `proposals.jsonl`（代表「尚待處理的治理狀態」，必須撐過重開機/cache 刪除）
   刻意不同——scope 候選在確認前只是可低成本重算的建議。
2. **Clustering 候選建議可以同時使用 import 與 best-effort reference edge（calls/extends/
   implements）做圖聚類**：這不違反 ARCHITECTURE §4.3「Scope/Constraint 系統不得把 reference
   graph 當唯一依據」——那條原則管的是治理系統（無人把關的自動寫入），clustering 建議一定要經人類
   確認才會寫進 `scopes.json`，human review 本身就是對 reference 解析不完美的防線。
3. **新檔案 incremental 自動併入既有 scope，只認 import edge、且僅限單一候選**：`rune update` 對
   新檔案，若透過 `edge_type=imports`（`confidence=1.0`）**恰好命中一個**現有、非 `locked` 的
   scope，才自動加進該 scope 的 `members.files`，不需人類確認。以下情況一律落回人類確認的 CLI
   流程：零個或多個候選 scope（模糊）、只有 best-effort reference 命中（沒有 import edge）、目標
   scope 是 `locked`、任何會移除既有 membership 或建立新 scope 的動作。這個判準刻意比 clustering
   建議嚴格，因為 incremental 自動併入是唯一無人把關的寫入路徑，只能用 high-confidence 訊號。
   `locked` scope 永遠不受任何形式（clustering 建議或 incremental 自動併入）影響。

### Milestone 4 實作結果

`model.py` 提供 Scope CRUD 與嚴格的 import-only incremental assignment；`heuristics.py` 按共同頂層
目錄產生候選；`clustering.py` 從 SQLite `edges` 讀取 import/reference edge 後以 NetworkX
connected-components 產生候選，沒有重新解析來源檔。CLI 已提供
`rune scope list/create/edit/delete/lock/unlock/suggest`。第 49 條的真實 repo 實驗發現頂層目錄 heuristic
偏寬，但其輸出保持人類審核的 ephemeral suggestion，故不是正確性或治理風險；未鎖定 threshold。

## 舊進度記錄（Milestone 1-3，供對照）

專案採 8 個 Milestone（見 IMPLEMENTATION_PLAN.md 開頭）：

| Milestone | 狀態 | 內容 |
|---|---|---|
| 1. Core foundation | ✅ 完成 | canonical storage、config、project init、SQLite materialize、CLI 骨架 |
| 2. Code index | ✅ 完成 | Tree-sitter 掃描/解析、symbol 擷取、import graph |
| 3. References / graph | ✅ 完成 | best-effort calls/extends/implements 解析 |
| 4. Scopes | ✅ 完成 | Scope CRUD、一次性 heuristic/graph suggestions、import-only incremental auto-assignment |
| 5. Semantic worker | ✅ 完成 | provider/redaction/validation/worker、真實 API 驗證過 |
| 6. Policies & Memory | ❌ 未開始 | Decision/Constraint/Note 生命週期、search |
| 7. OpenCode Adapter | ❌ 未開始 | hard/soft bootstrap 注入 |
| 8. MCP + Polish | ❌ 未開始 | MCP server、doctor、打包 |

**192 個測試全綠，`ruff check` 全綠。** 每個 commit 都是在這個狀態下才 push 的，沒有已知的失敗
測試或已知會崩潰的路徑殘留。

## 程式碼結構（`src/rune/`）

```text
src/rune/
├─ cli/main.py              # Typer app：init / status / update / rebuild-cache / scope
├─ core/
│  ├─ config.py             # .rune/config.toml 讀取/驗證（extra="forbid"，見下方「重要教訓」）
│  ├─ hashing.py            # content_hash / git_blob_hash / working_tree_fingerprint
│  ├─ project.py            # find_repo_root（真的呼叫 git，不是只看 .git 存不存在）、
│  │                        # init_project（refuse/--force 語意）、RuneLayout（路徑集中管理）
│  ├─ update.py             # run_update(layout, full) —— 整個 Milestone 2/3 的協調中心
│  ├─ scopes/
│  │  ├─ model.py            # Scope CRUD、import-only incremental membership assignment
│  │  ├─ heuristics.py       # 路徑候選（一次性、非 canonical）
│  │  └─ clustering.py       # SQLite edges -> NetworkX graph 候選（一次性、非 canonical）
│  ├─ semantic/
│  │  ├─ provider.py         # ModelProvider protocol、OpenRouter/OpenAI/通用 httpx 實作
│  │  ├─ redaction.py        # free-text 欄位 secret 遮蔽（結構化參照欄位不碰）
│  │  ├─ validation.py       # schema 拒絕 vs. 條目 strip 的三層驗證
│  │  └─ worker.py           # staleness 判斷、fallback ladder、逐 scope refresh 迴圈
│  ├─ index/
│  │  ├─ scanner.py         # include/exclude glob 走訪（自寫 **-aware matcher）、diff_against_previous
│  │  ├─ treesitter.py      # ParserAdapter protocol + Python/JS/TS/TSX 實作
│  │  ├─ imports.py         # import specifier 解析成檔案路徑
│  │  └─ references.py      # calls/extends/implements 的跨檔案解析
│  └─ storage/
│     ├─ models.py          # 所有 canonical Pydantic model（超級重要，改之前先讀 DATA_MODEL.md）
│     ├─ canonical.py       # atomic_write_text / read_json_model / append_jsonl 等
│     ├─ schema_versions.py
│     └─ sqlite/
│        ├─ schema.sql      # 完整 SQLite schema
│        └─ materialize.py  # rebuild_cache()：整個系統唯一寫 SQLite 的地方
```

`tests/unit/`、`tests/integration/`（含 `fixtures/python-simple`、`fixtures/ts-simple` 兩個
git-init 過的小型測試用 repo）。

## 開發環境

- **Python 3.12+**（`pyproject.toml` 的 `requires-python`；開發機原本只有 3.11，已用
  `winget install Python.Python.3.12` 補裝，理由是這個專案預期用好幾年、3.12 的安全支援窗口比
  3.11 晚一年）。
- **venv 在 `.venv/`**（`.gitignore` 排除），用 `pip install -e .` 裝的，不是 `uv`（開發機沒裝
  uv，但 `pyproject.toml` 保持 uv 相容）。
- 常用指令（Windows Git Bash 語法）：
  ```bash
  cd "C:\Users\maste\PycharmProjects\pmem"
  ".venv/Scripts/python.exe" -m pytest -q          # 跑全部測試
  ".venv/Scripts/python.exe" -m ruff check src tests   # lint
  ".venv/Scripts/python.exe" -m rune.cli.main init --path <repo>   # 手動跑 CLI
  ```
- 依賴：`pydantic>=2.6`、`typer>=0.12`、`tomli-w>=1.0`、`tree-sitter>=0.23` +
  `tree-sitter-{python,javascript,typescript}>=0.23`。

## 這個專案裡幾個「一定要知道」的設計決策

這些不是隨手寫的，都是討論過、記錄在文件裡、有時候是踩過坑才定案的，改之前務必先讀對應文件段落：

1. **`current` 與 `visible` 是兩個分開的概念**（DATA_MODEL.md §1、§3）：Decision/Constraint/
   Note 的 `current revision` 永遠是 `MAX(revision)`，跟 `status` 完全無關；是否要顯示、用什麼
   形式顯示，是另外一層邏輯。這是第二輪修訂修的一個根本性 bug，千萬別把兩者合併回去。
2. **`.rune/` 底下的 canonical 檔案（decisions.jsonl 等）跟 SQLite cache 是分離的**：SQLite
   （`.rune/cache/memory.db`）**永遠是衍生資料**，可以隨時刪掉用 `rune rebuild-cache` 重建。
   Code index（files/symbols/edges）比較特殊：它**沒有** canonical JSON/JSONL 背書，因為原始碼
   本身就是 source of truth，每次都直接從檔案重新推導。
3. **`rebuild_cache()` 是原地 transaction，不是「另建檔案再置換」**：第一版曾經用
   temp-file + `os.replace()` 做 crash-safety，結果在 Windows 上只要有 reader 開著檔案，
   `os.replace` 就會丟 `PermissionError`——比原本要解決的問題更嚴重。現在是在同一個檔案內用一個
   SQLite transaction（清空六個根表 → 靠 `ON DELETE CASCADE` 帶走所有衛星表 → 重新寫入 →
   commit），crash-safety 交給 SQLite 自己的 rollback journal。**不要再改回檔案置換的寫法。**
4. **`INSERT OR IGNORE` 不會抑制 SQLite 的外鍵違反**（只會抑制 UNIQUE 衝突）——這是親自寫小
   腳本驗證過的事實，不是網路上看來的印象。任何要插入可能違反 FK 的資料，都要在插入前自己過濾，
   不能指望 `OR IGNORE` 幫你擋。
5. **`imports` 是 high confidence，`calls`/`extends`/`implements`/`references` 是
   best-effort**（ARCHITECTURE §4.3）：import 的 confidence 永遠是 1.0（代表「這個 import 陳述句
   真的存在」，不代表「解析到的目標檔案一定對」）；reference 類的 confidence 依匹配方式分級
   （本檔案命中 0.8、透過 import 關係命中 0.6、完全比對不到 0.3，**仍然記錄，不刪除**）。
   Scope/Constraint 系統**不可以**把 reference graph 當唯一依據——已有回歸測試證明 reference
   全部清空時 `rune update` 仍能正常跑完。
6. **任何跨檔案的「哪個候選勝出」邏輯都要 `sorted()`，不能直接迭代 Python `set`**：Milestone 3
   踩到的坑——字串的 set 迭代順序在不同**行程**之間會因為 hash 隨機化（`PYTHONHASHSEED`）不同，
   同一份原始碼在不同次 `rune update` 呼叫可能解析出不同結果。單一行程內重複呼叫測不出這個問題
   （CPython 同行程內 hash 快取穩定），必須真的跨行程（`subprocess` + 不同 `PYTHONHASHSEED`）才
   測得出來。`references.py` 已經修好且有這種跨行程測試，未來任何類似的「從集合裡挑一個」邏輯都
   要留意這件事。
7. **`content_hash` 沒變 ≠ 上次解析是乾淨的**：`IndexedFileStatus.parse_error` 必須一路沿用到
   該檔案真的被重新解析為止，不能因為這次是 "unchanged"（不會重新丟進 tree-sitter）就預設狀態是
   `ok`。這是最新一次（d9cc48a）修的 bug。
8. **config.toml 用 `extra="forbid"`**：未知欄位（打錯字的 config 區塊）會直接拋錯，不會被
   Pydantic 靜默吞掉。這是刻意的，因為靜默接受設定錯字比丟例外更危險。
9. **`find_repo_root` 真的呼叫 `git rev-parse --show-toplevel`**，不是只檢查 `.git` 路徑存不
   存在——一個偽造的 `.git` 空目錄不該被當成合法 repo。
10. **`ScopeSummary` 現在有 `revision` 欄位，跟 Decision/Constraint/Note 共用同一套 current 機制**
    （DATA_MODEL.md §2.4，Milestone 5 開工前修正）：生成失敗時**一定要附加新的一行**（已有內容就
    複製舊內容只改 status/last_error，從未成功過就附加 `status=unavailable` 且內容留空），絕不能
    回到「失敗不寫 JSONL」的舊設計——那個設計自相矛盾，SQLite 投影本來就是從 JSONL 重新算出來的。
11. **`semantic.jsonl` 的 `last_error` 只能是清洗過的分類字串**（例如 `"provider_error:
    TimeoutError"`），絕對不能放 provider 的原始回應或例外訊息——那個檔案可能進 git。完整原始錯誤
    寫進 `.rune/logs/semantic.log`（不進 git，純本機除錯用）。
12. **API key 只能從環境變數讀，rune 自己的程式碼絕不讀專案根目錄的任何檔案來取得 key**——即使
    使用者為了本機測試方便建立了 `token.env` 之類的檔案，那也只是外部 shell 慣例，不是 rune 的
    功能。`build_provider()`（`core.semantic.provider`）只認 `OPENROUTER_API_KEY`/`OPENAI_API_KEY`
    這兩個環境變數。

## 這個專案的工作方式（如果你是接手的 agent，請比照辦理）

這個 session 一路下來的模式，使用者似乎很看重，建議延續：

1. **寫完一個 milestone 就自己重跑一次，不要只信任第一次寫對**。每個 milestone 完成後都做了
   「自我複查」：不是重讀程式碼，而是**實際跑腳本重現邊界情況**（例如直接 dump tree-sitter 的
   AST 看裝飾器、`var` 宣告怎麼被解析；直接開兩個 sqlite connection 測併發；直接用不同
   `PYTHONHASHSEED` 跑 subprocess 測 set 順序）。這個方法論已經抓到好幾個「紙上看不出來」的真
   bug（見 IMPLEMENTATION_PLAN.md 每個 milestone 底下的「自我複查」段落）。
2. **外部 code review 的每一條 finding 都要先重現，才動手修**，不要看了描述就直接信。這個 session
   裡至少有一次自己重現失敗（先猜測、後驗證發現猜錯層級），修正後才確認真的抓到問題——連測試本身
   都要驗證「它是不是真的在測它宣稱要測的東西」。
3. **每個 bug 修完都要在 IMPLEMENTATION_PLAN.md 補一段記錄**：多嚴重、在哪裡、怎麼重現、怎麼修、
   為什麼這樣修（不是別的方案）、怎麼驗證。這份文件現在已經是很長的「決策與 bug 記錄」，這是刻意
   的，不要嫌它太長而省略新的記錄。
4. **修完就 commit + push**，commit message 要完整交代前因後果（這個 repo 的 commit message
   風格都寫得很長很詳細，不是隨便寫一行）。**不要加 attribution line。**
5. 遇到「這個決定該怎麼定」的時候（例如 `init --force` 該不該清掉 scopes.json、Decision 的
   staleness 要不要跟 hash 綁），**先問使用者，記錄決策理由，再動手**，不要自己選一個看起來最
   簡單的方案就做下去。

## 已知的限制／還沒做的事（不是 bug，是刻意的 V1 範圍）

- **多人協作衝突**：`revision` 是單純遞增整數，兩個 branch 各自從同一個 revision 產生下一版再
  merge 會撞號，V1 明確不解決，只保證偵測到衝突時會拒絕 materialize（不會默默選一個）。
  - **`from . import X`（Python 純點號相對 import）解析不到具體目標**：因為目前的擷取邏輯只抓
    relative import 的點號前綴，沒有抓 `import` 後面的名稱列表，這種情況現在回傳 `None`（誠實地
    unresolved），刻意不猜（之前猜錯過，猜成套件自己的 `__init__.py`，已經修掉那個假陽性）。
- **Scope clustering 演算法參數不鎖死**：Milestone 4 明確定位為「留給真實 repo 實驗調整」，不是
  現在就要做到完美聚類。
- **`project.json` 的 `last_indexed_*` 欄位跟 SQLite commit 不是原子的**：這是接受的已知限制，
  有文件記錄取捨理由跟自我修復機制（下次 update 一定會重新算，不會被過期 metadata 帶壞）。

## 立刻可以做的下一步

**第十五輪確認的三層 provider 健康檢查 + `possibly_stale` 觸發邏輯已在第十六輪實作完成**
（`check_semantic_health`、`ModelProvider.probe()`、`mark_possibly_stale`，見上方「第十六輪修訂」
段落與 IMPLEMENTATION_PLAN.md 第 85-90 條）。**唯一還沒做的部分**：retrieval 端「`possibly_stale`/
`stale` 不顯示舊摘要文字、改指向 `source_files`」的規則（第十五輪決議第 2 點）——這個屬於
Milestone 6 的 retrieval 範圍，`mark_possibly_stale` 已經確保 `source_files` 欄位在每次觸發時都
正確更新，Milestone 6 可以直接使用，不需要回頭修 Milestone 5 的程式碼。

**Milestone 6（Policies & Memory）**：`rune.core.memory.{decisions,constraints,notes,
staleness}`。開工前重讀 IMPLEMENTATION_PLAN.md 的 Milestone 6 整段（含驗收標準）與 DATA_MODEL.md
§1、§3、§6——「current 與 visible 分離」是這個 milestone 最容易重犯的錯誤點，文件裡已經明講必須有
專門測試鎖死 `rev1=active, rev2=inactive` 這個 case。Note 的 revision 化現在有 Milestone 5 的
`ScopeSummary` revision 實作可以參考同一套模式，retrieval 對 `possibly_stale`/`stale` 摘要的
「不顯示舊內容、改指向 source_files」規則也要在這個 milestone 落地。Proposal 流程
（`decision_propose`/`constraint_propose`）也是這個 milestone 的範圍，記得核准 constraint 時要依
`persistence_mode` 自動算好 `source_hashes`/`scope_hashes`/`expires_at` snapshot，不是人類手動填。
