# RepoRune（rune）— 資料模型

狀態：**已確認（第三輪修訂）**。第二輪修正了 revision lifecycle 的一個根本性 bug（current 與 visible
必須分離）、補上 `source_bound`/`scope_bound`/`temporary` Constraint 實際可實作所需的 snapshot 欄位、
補上 Note 的 revision 機制、以及 ScopeSummary `source_files` 的推導 invariant。第三輪修正
`created_by` 的型別（改為 `RevisionAuthor` enum，解決與「系統自動附加 revision」的矛盾）、補上
Proposal 的 revision 機制（解決 append-only 與「改狀態」的矛盾）、補齊 Note 在 SQLite 的
`source_hashes`/`evidence` 投影、明寫系統自動附加 revision 時的「完整 snapshot」規則、以及確定
`temporary` Constraint 過期後轉為 `stale` 而非 `inactive`。第四輪新增 `PricingConfig`（純估算用途，
見 §7）；資料模型本身的 revision/staleness 邏輯未再變動。第五輪新增 Global Code Standards / Hard
Policy Injection 支援欄位——`MemoryRevision` 新增選填的 `critical`（decision-only，標記進 hard
bootstrap）、`source_document`/`source_section`（追溯到 `CODE_STANDARDS.md`）、
`machine_check_hint`（constraint-only），以及 `BootstrapConfig`；「global constraint」的判定
（`scopes == []`）完全沿用既有 schema，不需要新的 boolean flag，詳見 ARCHITECTURE.md §7。這是
Milestone 1–6/7 實作時遵循的契約。

## 1. 慣例

- 需要跨重新索引存活的 ID（`scope.id`、Decision/Constraint 的 `record_id`、Note 的 `id`、`symbol_id`）
  一律是**穩定字串**，絕非流水號、絕非暫時性 clustering 標籤（規格 §10、§30）。
- 每個 canonical JSON/JSONL 檔案的頂層物件（或 JSONL 每行的隱含約定）都帶 `schema_version: int`。
- 時間戳一律為 ISO-8601 UTC 字串（`created_at`、`generated_at`、`last_verified_at`、`expires_at`）。
- **「current」與「visible」是兩個不同概念（本輪修正的核心 bug）**：
  - **current revision** = 該 `record_id`／Note `id` 底下 **`revision` 最大者，與其 `status` 完全無關**。
    停用（`inactive`）或孤兒化（`orphaned`）的 revision 如果是最新的一筆，它就是 current——絕不會因為
    status 不在某個白名單集合裡就被目前版本較舊的 revision 取代。
  - **visible**：current revision 是否出現在預設 retrieval，以及以什麼形式出現（正常／帶警告／不出現），
    完全由該 current revision 的 `status` 另外決定，見第 3、6 節的可見性表。
  - 兩者混為一談正是先前版本的 bug：例如 `rev1=active, rev2=inactive` 時，current 必須是 rev2
    （因為它是最新核准的動作，即「停用」這個決定本身），只是 `inactive` 的可見性規則是「不顯示」——
    結果剛好等於「agent 看不到它」，但**不是**因為系統誤判 rev1 才是 current。這個區分在
    `rev1=active, rev2=review_required` 這種情境下才會顯出差異：current 是 rev2，可見性規則是
    「顯示 + 警告」，而非退回顯示 rev1。
- **單一寫入者假設（V1 不解決多人協作場景，見第 8 節）**：V1 假設每個邏輯 record 同時只有一個寫入者。
  若 canonical JSONL 中出現同一 `(record_id, revision)`（或同一 `id, revision`）重複的兩行，
  materialize 視為 canonical 衝突，**絕不自動選一筆默默採用**，而是中止 materialize 並由
  `rune doctor`/`rune update` 回報衝突，要求人類手動解決（通常是編輯 JSONL 移除其中一行重新編號）。

## 2. Canonical Pydantic model（`rune.core.storage.models`）

### 2.1 Project（`project.json`）
```python
class ProjectFile(BaseModel):
    schema_version: int = 1
    project_id: str                     # 穩定，`rune init` 時產生一次，`--force` 絕不重新產生（見第 9 節）
    name: str
    created_at: str                     # `--force` 絕不覆寫
    last_indexed_head: str | None       # 上次成功 `rune update` 時的 git HEAD sha
    last_indexed_tree_hash: str | None   # rune 自算：已索引檔案 (path, content_hash) 排序後串接雜湊
    last_indexed_at: str | None
```

### 2.2 程式碼索引基礎型別（衍生資料——非 canonical JSON，是 `core.index` 與
`core.storage.sqlite.materialize` 之間傳遞的形狀，只落地在 SQLite）

```python
class IndexedFile(BaseModel):
    path: str                      # repo 相對路徑，posix 分隔符
    language: str                  # "python" | "typescript" | "javascript" | ...
    content_hash: str              # 檔案位元組的 sha256
    size: int
    mtime: float
    git_blob_hash: str | None
    indexed_at: str
    status: Literal["ok", "parse_error"] = "ok"

class SymbolKind(str, Enum):
    function = "function"
    class_ = "class"
    method = "method"
    interface = "interface"
    type = "type"
    variable = "variable"
    constant = "constant"
    component = "component"

class Symbol(BaseModel):
    symbol_id: str                 # 穩定：sha1(f"{path}:{qualified_name}:{kind}")[:16]
    file: str
    name: str
    qualified_name: str
    kind: SymbolKind
    signature: str | None
    start_line: int
    end_line: int

class EdgeType(str, Enum):
    imports = "imports"
    references = "references"
    calls = "calls"
    extends = "extends"
    implements = "implements"

class Edge(BaseModel):
    source_symbol: str | None       # symbol_id；檔案層級 import edge 可為 None
    source_file: str
    target_symbol: str | None
    target_file: str | None         # 無法解析時為 None
    edge_type: EdgeType
    confidence: float               # 0.0-1.0；已解析 import 為 1.0，best-effort reference 較低
```

`symbol_id` 刻意設計為 `(path, qualified_name, kind)` 的純函式。Rename 視為刪除舊 symbol + 建立新
symbol：產生新的 `symbol_id`，任何引用舊 ID 的東西會被標記為 orphaned，不做模糊比對或自動重新映射。

### 2.3 Scope（`scopes.json`）
```python
class ScopeSource(str, Enum):
    auto = "auto"
    model = "model"
    human = "human"

class ScopeMembers(BaseModel):
    files: list[str] = []
    symbols: list[str] = []          # symbol_id 清單

class Scope(BaseModel):
    id: str                          # 穩定 kebab-case，例如 "authentication"
    name: str
    description: str = ""
    locked: bool = False
    source: ScopeSource
    members: ScopeMembers

class ScopesFile(BaseModel):
    schema_version: int = 1
    scopes: list[Scope]
```
`scopes.json` 是昂貴的 canonical knowledge（scope 定義、名稱、membership 一旦建立就有持續價值），
`rune init --force` 絕不清空或重建此檔案為 skeleton，見第 9 節。

### 2.4 Semantic summary（`semantic.jsonl`，一行一個 JSON 物件，append-only）
```python
class SemanticStatus(str, Enum):
    fresh = "fresh"
    possibly_stale = "possibly_stale"
    stale = "stale"

class ScopeSummary(BaseModel):
    scope_id: str
    purpose: str
    responsibilities: list[str] = []
    entry_points: list[str] = []      # file 或 symbol_id
    important_symbols: list[str] = [] # symbol_id
    dependencies: list[str] = []      # scope_id 或外部套件名稱
    data_flow: list[str] = []
    invariants: list[str] = []
    known_risks: list[str] = []
    open_questions: list[str] = []
    generated_at: str
    model: str
    source_hash: str                  # 所有 member 檔案 content_hash 的合併雜湊
    source_files: dict[str, str] = {} # path -> content_hash，見下方 invariant
    status: SemanticStatus = SemanticStatus.fresh
    last_error: str | None = None
    schema_version: int = 1
```

**Invariant（本輪新增，修正 edge case）**：`source_files` 必須等於
`scope.members.files ∪ {owning_file(s) for s in scope.members.symbols}`。也就是說，一個 scope 若只透過
`members.symbols`（而非 `members.files`）納入成員（例如 `scope.files=[]`、
`scope.symbols=["AuthService.login", "TokenService.rotate"]`），`source_files` 仍必須解析每個
symbol 的 owning file（透過 SQLite `symbols.file`）並納入，例如：
```json
"source_files": {
  "auth/service.ts": "sha256:...",
  "auth/token.ts": "sha256:..."
}
```
`source_files = {}` 只有在 scope 完全沒有 member 時才合法；`core.semantic.worker` 在組 prompt 與計算
`source_hash` 前，必須先呼叫這條解析邏輯，不能只看 `scope.members.files`。

Append-only JSONL：每次 `rune update` 重新產生某 scope 的 summary 就附加新的一行；SQLite 只
materialize 每個 `scope_id` 最新一行（`max(generated_at)` 或以寫入順序為準，非本節 revision
機制管轄——ScopeSummary 沒有 Decision/Constraint/Note 那種 `revision` 欄位，因為它單純是「目前的
描述」而非需要 audit trail 的治理紀錄）。

### 2.5 Decision / Constraint revision（`decisions.jsonl`、`constraints.jsonl`）

```python
class RevisionAuthor(str, Enum):
    """
    revision 的產生來源。`agent`/`human` 對應「新增權威內容」的正常流程（需經核准）；
    `system_staleness` 對應 core.memory.staleness 偵測到 snapshot/存在性偏離而自動附加的
    lifecycle revision；`system_lifecycle` 對應 TTL 到期等非 staleness 觸發的自動轉換
    （例如 temporary constraint 過期）。本輪新增，取代原本的 `Literal["agent", "human"]`——
    後者無法表示 Implementation Plan 明確要求的「系統自動附加 revision」情境，會導致
    Pydantic validation 失敗。同時套用於 Note 的 `source` 欄位（見 §2.6），因為 Note 的
    lifecycle 轉換一樣可能是系統觸發。
    """
    agent = "agent"
    human = "human"
    system_staleness = "system:staleness"
    system_lifecycle = "system:lifecycle"

class RecordType(str, Enum):
    decision = "decision"
    constraint = "constraint"

class RecordStatus(str, Enum):
    active = "active"
    review_required = "review_required"   # 引用的 file/symbol 被刪除，但 scope 仍在——需要人類覆核
    stale = "stale"                        # 僅 constraint 的 source_bound/temporary 模式使用
    orphaned = "orphaned"                  # 引用的整個 scope 消失
    inactive = "inactive"                  # 人類明確停用

class Severity(str, Enum):           # 僅 constraint 使用
    must = "MUST"
    should = "SHOULD"
    info = "INFO"

class PersistenceMode(str, Enum):    # 僅 constraint 使用
    persistent = "persistent"
    scope_bound = "scope_bound"
    source_bound = "source_bound"
    temporary = "temporary"

class MemoryRevision(BaseModel):
    record_id: str                   # 穩定，例如 "auth-session-storage"
    revision: int                    # 1, 2, 3, ...，per record_id 單調遞增
    type: RecordType
    status: RecordStatus
    content: str
    rationale: str = ""
    scopes: list[str] = []           # scope id，多值
    files: list[str] = []
    symbols: list[str] = []          # symbol_id
    severity: Severity | None = None            # type == constraint 時必填
    persistence_mode: PersistenceMode | None = None  # type == constraint 時必填

    # --- 本輪新增：讓 source_bound / scope_bound / temporary 真正可實作 ---
    source_hashes: dict[str, str] = {}
    # 僅 persistence_mode == source_bound 時使用。核准當下對 `files`（以及 `symbols` 解析出的
    # owning file）的 content_hash 快照，例如 {"src/auth/token.ts": "sha256:ABC..."}。
    # staleness 判斷 = 目前 content_hash 是否仍等於此快照值，而非只看「files 欄位有沒有列」。
    scope_hashes: dict[str, str] = {}
    # 僅 persistence_mode == scope_bound 時使用。核准當下對 `scopes` 每個 scope 的 membership
    # 快照雜湊，例如 {"authentication": "sha256(sorted(files+symbols))"}。
    # staleness 判斷 = 目前重新計算的 membership hash 是否仍等於此快照值。
    expires_at: str | None = None
    # 僅 persistence_mode == temporary 時使用（必填，見下方驗證規則）。

    # --- 本輪新增：Global Code Standards / Hard Policy Injection 支援欄位 ---
    # 見 ARCHITECTURE.md §7。三者皆為選填，V1 不強制寫入，但資料模型預留以避免未來需要 migration。
    critical: bool = False
    # 僅對 type == decision 有意義的慣例欄位：標記這個 Decision 是否要進入 hard bootstrap
    # （ARCHITECTURE §7.3、§7.5）。不是所有 Decision 都該進 hard bootstrap，只有明確標記的少數。
    # Constraint 不使用這個欄位表達「全域」——一個 Constraint 是否為 global 完全由
    # `scopes == []` 決定，不需要另一個 boolean（見 ARCHITECTURE §7.2）。
    source_document: str | None = None
    source_section: str | None = None
    # 供人類追溯這條 MUST/SHOULD 規則對應到 CODE_STANDARDS.md（或其他文件）的哪一段，避免兩份文件
    # 語意漂移卻無從對照。純粹是人類可讀的追溯資訊，不影響任何 staleness/current/visible 邏輯。
    machine_check_hint: str | None = None
    # 僅對 type == constraint 有意義：記錄這條規則若可被工具驗證，對應的工具名稱（例如 "ruff"、
    # "mypy"、"pytest"）。V1 不執行任何驗證邏輯，rune 只負責提醒規則存在，驗證永遠交給工具本身
    # （ARCHITECTURE §7.8）。

    created_by: RevisionAuthor        # 本輪修正：改為 enum，涵蓋 system_staleness/system_lifecycle
    approved_by: str | None          # 人類身分／代號；proposal 階段、以及 system 觸發的 revision 一律為 None
    created_at: str
    schema_version: int = 1
```

**Global Constraint 的判定規則（本輪明定，不需要新欄位）**：一個 Constraint 的 current revision 若
`scopes == []`（即 `constraint_scopes` 表中沒有任何對應列），就是 *global*；否則是 *scoped*。
Global MUST Constraint = global 且 `severity == "MUST"`。這類 constraint 的 `persistence_mode`
理論上應為 `persistent`（不依賴任何 file/scope hash），`rune doctor` 對「global 但非
persistent」的組合發出警告，但 V1 不在核准流程強制阻擋（ARCHITECTURE §7.2）。

**寫入時驗證規則（本輪新增，`core.memory.constraints` 在核准 proposal、寫出新 revision 前執行）：**
- `type == constraint and persistence_mode == source_bound` -> `source_hashes` 必須非空，且其 key
  集合等於 `files ∪ {owning_file(s) for s in symbols}`；缺一即拒絕核准，要求先解出 hash。
- `type == constraint and persistence_mode == scope_bound` -> `scope_hashes` 必須非空，且其 key
  集合等於 `scopes`。
- `type == constraint and persistence_mode == temporary` -> `expires_at` 必須有值（ISO-8601）；
  其他 `persistence_mode` 或 `type == decision` 時 `expires_at` 必須為 `null`。
- 這些 snapshot 由 `core.memory.constraints` 在核准當下自動計算並填入，**不要求人類手動輸入 hash**——
  人類只需核准 proposal 內容本身，snapshot 是系統自動附加的實作細節。

**系統自動附加 revision 時的完整性規則（本輪新增，非 blocker 但必須明寫，避免實作時漏欄位）**：

> System-generated lifecycle revisions are full snapshots of the previous current revision with
> only status/lifecycle metadata changed unless the transition explicitly requires otherwise.

也就是說，`core.memory.staleness` 在附加一筆 `status=review_required`/`stale`/`orphaned` 的新
revision 時，**必須複製前一筆 current revision 的完整內容**（`content`、`rationale`、`scopes`、
`files`、`symbols`、`severity`、`persistence_mode`、`source_hashes`、`scope_hashes`、
`expires_at` 全部原樣帶過去），只改動 `status`（以及 `created_by=RevisionAuthor.system_staleness`
或 `system_lifecycle`、新的 `created_at`、`approved_by=None`）。絕不能只寫一筆只有
`{record_id, revision, status}` 的殘缺 revision——那會讓 current revision 遺失原本的
constraint/decision metadata（例如 `source_hashes` snapshot 消失，導致下次 staleness 判斷失去
比對基準）。此規則同樣適用於 Note（見 §2.6）的系統觸發轉換（TTL 到期、source hash 偏離）。

### 2.5a Pending proposal（`.rune/proposals.jsonl`，canonical 持久化，本輪修正為 revision 化）

**問題背景（本輪修正）**：`proposals.jsonl` 定義為 append-only，但流程要求「同一 proposal 從
`pending` 改成 `approved`」——append-only 檔案不能原地改寫既有行，若不定義版本規則，就只剩兩種
不明確的做法：原地改寫（違反 append-only）或再 append 一行卻不知道 materialize 該怎麼選。修法與
Note 一致：**Proposal 也採 `revision` 機制**，`proposal_id` 是邏輯身分，current = `max(revision)`。

```python
class ProposalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    edited = "edited"

class Proposal(BaseModel):
    proposal_id: str          # 穩定，例如 ulid；同一提案跨多個 revision 共用
    revision: int             # 1, 2, ...，per proposal_id 單調遞增；current = max(revision)
    type: RecordType
    record_id: str
    payload: MemoryRevision   # approved_by 必為 None；source_hashes/scope_hashes 尚未計算（核准時才補）
    status: ProposalStatus = ProposalStatus.pending
    created_by: Literal["agent", "human"]   # propose 動作只會是 agent 或 human，不會是 system
    created_at: str
    resolved_at: str | None = None   # 該 revision 若是解決動作（approve/reject/edit）才有值
    resolved_by: str | None = None
    schema_version: int = 1
```

流程範例：`proposal_id=P` 的 `revision=1`（`status=pending`）由 agent 寫入；人類核准時，附加
`revision=2`（`status=approved`，`resolved_at`/`resolved_by` 有值），**同時**（同一次操作）在
`decisions.jsonl`/`constraints.jsonl` 附加對應的 `MemoryRevision`（`approved_by` 有值）。人類編輯後
核准（`edited`）時同理：`revision=2` 記錄編輯後的 `payload` 與 `status=edited`，並照樣驅動
`decisions.jsonl`/`constraints.jsonl` 的新增。`rune search`/一般 retrieval 只需要看
「`status=pending` 的 current revision」（即尚待處理的提案清單），history 模式可看到完整
提案處理過程。

`proposals.jsonl` 不進入正常 memory retrieval，但必須撐過 cache rebuild、重開機、`git checkout`。是否
連同該檔案一起 commit 進 git，由 config `[proposals].commit_to_git`（預設 `false`）決定。

### 2.6 Note（`notes.jsonl`，本輪新增 revision 機制）

**問題背景（本輪修正）**：Note 需要能被更新／重新驗證／封存（例如「package X 在 Windows 有 bug」一個月
後被驗證為已修復），但 canonical 是 append-only JSONL，若沒有明確的版本欄位，同一個 `id` 出現兩行時
無法判斷誰是 current。修法比照 Decision/Constraint，但**不需要 proposal/human approval 關卡**——
agent 仍可直接寫入新 revision。

```python
class NoteCategory(str, Enum):
    pitfall = "pitfall"
    observation = "observation"
    workaround = "workaround"
    implementation_detail = "implementation_detail"
    known_issue = "known_issue"
    temporary_context = "temporary_context"
    investigation_result = "investigation_result"

class NoteStatus(str, Enum):
    active = "active"
    stale = "stale"       # 仍出現在預設 retrieval，標記 [STALE]，不隱藏
    expired = "expired"    # 預設 retrieval 排除
    orphaned = "orphaned"  # 預設 retrieval 排除
    archived = "archived"  # 僅 history/audit 模式可見

class Note(BaseModel):
    id: str                           # 穩定 record identity，例如 ulid；同一筆知識跨多個 revision 共用
    revision: int                     # 1, 2, 3, ...，per id 單調遞增
    category: NoteCategory
    content: str
    why_persist: str
    scopes: list[str] = []
    files: list[str] = []
    symbols: list[str] = []
    importance: float = 0.5          # 0.0-1.0
    confidence: float = 0.5
    source: RevisionAuthor            # 本輪修正：改為與 MemoryRevision.created_by 相同的 enum，
                                       # 因為 stale/expired 等 lifecycle 轉換也可能是系統自動附加
    evidence: list[str] = []
    created_at: str
    last_verified_at: str
    expires_at: str | None
    source_hashes: dict[str, str] = {}   # path -> content_hash，供 source_bound staleness 判斷
    status: NoteStatus = NoteStatus.active
    schema_version: int = 1
```

「更新一則 Note」= 附加一行 `id` 相同、`revision = 上一筆 + 1` 的新 JSON 物件（例如把 `status` 改為
`archived` 並附上新 `content` 說明已被驗證修復）。**current note = 該 `id` 底下 `revision` 最大者**，
與第 1 節「current 與 visible 分離」的規則完全一致；預設 retrieval 只看 current revision，再依其
`status` 決定可見性（見第 6 節表格）；history 模式可看到全部 revision。系統自動觸發的轉換（TTL 到期
->`expired`、source hash 偏離 ->`stale`）同樣適用 §2.5 定義的「完整 snapshot」規則：新 revision 複製
前一筆的 `content`/`why_persist`/`scopes`/`files`/`symbols`/`source_hashes` 等欄位，只改動
`status`、`source=RevisionAuthor.system_lifecycle`（或 `system_staleness`）、`last_verified_at`。

## 3. Revision 模型（Decision / Constraint / Note 共用同一套「current vs visible」規則）

- `record_id`（Decision/Constraint）或 `id`（Note）是邏輯身分；`revision` 為其底下單調遞增，從 1 開始。
- **current revision = `max(revision)`，無條件成立，與 `status` 完全無關**（本輪修正的核心 bug：先前
  版本誤把「`status ∈ {active, review_required}` 中最大者」當作 current，導致 `rev1=active,
  rev2=inactive` 時系統會錯誤地把 rev1 當作 current，變成「你明明停用了 Decision，agent 卻繼續看到
  舊內容」）。
- **visible（是否出現在預設 retrieval，以何種形式）完全由 current revision 的 `status` 另外決定**，
  規則如下（Decision/Constraint 共用；Note 見第 6 節，語意相同但狀態集合不同）：

  | current revision 的 status | 預設 retrieval 行為 |
  |---|---|
  | `active` | 正常顯示 |
  | `review_required` | 顯示，附「需要人類覆核」警告 |
  | `stale`（僅 constraint） | 顯示，附「可能已過期」警告 |
  | `inactive` | 不顯示 |
  | `orphaned` | 不顯示 |

- 取代某個 revision：附加新的一行，`revision = old.revision + 1`；`status` 依情境設定（人類停用 ->
  `inactive`；新內容核准取代 -> `active`；系統偵測到 snapshot hash 不符 -> `review_required`/`stale`，
  這種情況下**系統本身也可以是 revision 的產生者**，不需要每次都經過人類核准——只有「新增權威性內容」
  需要人類核准，「狀態轉換」由 `core.memory.staleness` 自動附加新 revision，`created_by` 設為
  `RevisionAuthor.system_staleness`/`system_lifecycle`，見 §2.5）。系統自動附加的 revision 必須是
  前一筆 current revision 的完整 snapshot，只改動 status/lifecycle 相關欄位（§2.5 的完整性規則）。
- 這是 SQLite 層的不變量，由 `materialize.py` 保證：`decision_records.current_revision`／
  `constraint_records.current_revision`／`note_records.current_revision` 一律等於該
  record_id/id 下實際存在的最大 `revision`，計算時**不得**依 `status` 過濾候選集合。
- Retrieval 預設只讀「current revision 且其 status 屬於可見集合」的紀錄；audit/history 讀取（明確
  CLI flag）讀取某 record_id/id 的全部 revision，不受可見性規則限制。
- **單一寫入者假設**：若解析 canonical JSONL 時發現同一 `(record_id, revision)` 或 `(id, revision)`
  重複，`materialize.py` 視為 canonical 衝突，中止該次 materialize 並回報，不自動選擇任一筆——這是
  V1 對多人協作（例如兩個 branch 各自從 rev1 產生 rev2 後 merge）刻意不解決的已知限制（見第 8 節）。

## 4. Scope membership 模型

雙層多對多（規格 §8–§9）：
- `scope_files(scope_id, file_path)`
- `scope_symbols(scope_id, symbol_id)`

一個檔案或 symbol 可屬於零個、一個或多個 scope。Membership 由 canonical `scopes.json` 的
`Scope.members` 在 materialize 時推導；canonical 是 membership 的 source of truth，SQLite 不是。

## 5. SQLite schema（`.rune/cache/memory.db`，完全衍生）

```sql
-- schema/version 記錄
CREATE TABLE schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
); -- 例如：('schema_version','1')、('materialized_from_head','<sha>')
   --      ('materialized_from_tree_hash','<hash>')、('materialized_at','<iso>')

-- 決定性程式碼索引
CREATE TABLE files (
    path          TEXT PRIMARY KEY,
    language      TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    size          INTEGER NOT NULL,
    mtime         REAL NOT NULL,
    git_blob_hash TEXT,
    indexed_at    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'ok'
);

CREATE TABLE symbols (
    symbol_id      TEXT PRIMARY KEY,
    file            TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    qualified_name  TEXT NOT NULL,
    kind            TEXT NOT NULL,
    signature       TEXT,
    start_line      INTEGER NOT NULL,
    end_line        INTEGER NOT NULL
);
CREATE INDEX idx_symbols_file ON symbols(file);
CREATE INDEX idx_symbols_name ON symbols(name);

CREATE TABLE edges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_symbol   TEXT REFERENCES symbols(symbol_id) ON DELETE CASCADE,
    source_file     TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    target_symbol   TEXT REFERENCES symbols(symbol_id) ON DELETE SET NULL,
    target_file     TEXT REFERENCES files(path) ON DELETE SET NULL,
    edge_type       TEXT NOT NULL,
    confidence      REAL NOT NULL DEFAULT 1.0
);
CREATE INDEX idx_edges_source ON edges(source_file, source_symbol);
CREATE INDEX idx_edges_target ON edges(target_file, target_symbol);

-- scope
CREATE TABLE scopes (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    locked      INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL
);

CREATE TABLE scope_files (
    scope_id TEXT NOT NULL REFERENCES scopes(id) ON DELETE CASCADE,
    file     TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    PRIMARY KEY (scope_id, file)
);

CREATE TABLE scope_symbols (
    scope_id  TEXT NOT NULL REFERENCES scopes(id) ON DELETE CASCADE,
    symbol_id TEXT NOT NULL REFERENCES symbols(symbol_id) ON DELETE CASCADE,
    PRIMARY KEY (scope_id, symbol_id)
);
```

**Milestone 1 曾暫時省略上面 `file`/`symbol_id` 的 FK**（`files`/`symbols` 表在 M1 還是空的，
`scopes.json` 卻可能已經帶有 file/symbol membership），**已於 Milestone 2 索引器落地後補回**，
如上面 schema 所示。補回後發現一個容易誤判的地方：`INSERT OR IGNORE` **不會**抑制外鍵違反（
只抑制 UNIQUE 衝突，已用最小重現腳本驗證過），所以 `materialize._materialize_scopes` 在插入前會
先查詢目前索引裡實際存在的 file path／symbol_id 集合，把不在其中的 scope 成員直接跳過（不中止整個
materialize）——一個 typo 或指向已刪除檔案的 scope membership 只會讓那筆 membership 消失，不會讓
`rune update`/`rune rebuild-cache` 整個失敗。

```sql
-- semantic summary（只存目前一份；歷史留在 semantic.jsonl，非本檔案 revision 機制管轄）
CREATE TABLE semantic_objects (
    scope_id      TEXT PRIMARY KEY REFERENCES scopes(id) ON DELETE CASCADE,
    purpose       TEXT NOT NULL,
    payload_json  TEXT NOT NULL,   -- 完整 ScopeSummary 序列化（含 source_files）
    generated_at  TEXT NOT NULL,
    model         TEXT NOT NULL,
    source_hash   TEXT NOT NULL,
    status        TEXT NOT NULL,
    last_error    TEXT
);

-- decision（current_revision = max(revision)，與 status 無關，見第 3 節）
CREATE TABLE decision_records (
    record_id TEXT PRIMARY KEY,
    current_revision INTEGER NOT NULL
);
CREATE TABLE decision_revisions (
    record_id TEXT NOT NULL REFERENCES decision_records(record_id) ON DELETE CASCADE,
    revision  INTEGER NOT NULL,
    status    TEXT NOT NULL,        -- active | review_required | orphaned | inactive
    content   TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    critical  INTEGER NOT NULL DEFAULT 0,  -- 本輪新增：是否進入 hard bootstrap，見 ARCHITECTURE §7.3
    source_document TEXT,                   -- 本輪新增，選填
    source_section  TEXT,                   -- 本輪新增，選填
    created_by TEXT NOT NULL,   -- RevisionAuthor 值：agent | human | system:staleness | system:lifecycle
    approved_by TEXT,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (record_id, revision)
);
CREATE TABLE decision_scopes (record_id TEXT, revision INTEGER, scope_id TEXT,
    FOREIGN KEY (record_id, revision) REFERENCES decision_revisions(record_id, revision) ON DELETE CASCADE);
CREATE TABLE decision_files  (record_id TEXT, revision INTEGER, file TEXT,
    FOREIGN KEY (record_id, revision) REFERENCES decision_revisions(record_id, revision) ON DELETE CASCADE);
CREATE TABLE decision_symbols(record_id TEXT, revision INTEGER, symbol_id TEXT,
    FOREIGN KEY (record_id, revision) REFERENCES decision_revisions(record_id, revision) ON DELETE CASCADE);

-- constraint（結構同 decision，多 severity/persistence_mode/snapshot 欄位；status 多一個 stale）
CREATE TABLE constraint_records (
    record_id TEXT PRIMARY KEY,
    current_revision INTEGER NOT NULL
);
CREATE TABLE constraint_revisions (
    record_id TEXT NOT NULL REFERENCES constraint_records(record_id) ON DELETE CASCADE,
    revision  INTEGER NOT NULL,
    status    TEXT NOT NULL,        -- active | review_required | stale | orphaned | inactive
    content   TEXT NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    severity  TEXT NOT NULL,
    persistence_mode TEXT NOT NULL,
    source_hashes_json TEXT NOT NULL DEFAULT '{}',  -- source_bound snapshot（第二輪新增）
    scope_hashes_json  TEXT NOT NULL DEFAULT '{}',  -- scope_bound snapshot（第二輪新增）
    expires_at TEXT,                                 -- temporary 模式必填（第二輪新增）
    source_document TEXT,        -- 本輪新增，選填，見 ARCHITECTURE §7.2
    source_section  TEXT,        -- 本輪新增，選填
    machine_check_hint TEXT,     -- 本輪新增，選填，見 ARCHITECTURE §7.8
    created_by TEXT NOT NULL,
    approved_by TEXT,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (record_id, revision)
);
CREATE TABLE constraint_scopes (record_id TEXT, revision INTEGER, scope_id TEXT,
    FOREIGN KEY (record_id, revision) REFERENCES constraint_revisions(record_id, revision) ON DELETE CASCADE);
CREATE TABLE constraint_files  (record_id TEXT, revision INTEGER, file TEXT,
    FOREIGN KEY (record_id, revision) REFERENCES constraint_revisions(record_id, revision) ON DELETE CASCADE);
CREATE TABLE constraint_symbols(record_id TEXT, revision INTEGER, symbol_id TEXT,
    FOREIGN KEY (record_id, revision) REFERENCES constraint_revisions(record_id, revision) ON DELETE CASCADE);

-- pending proposal（衍生快取，只存 current revision；source of truth 是 .rune/proposals.jsonl，
-- 其 revision 歷史不在 SQLite 保留完整拷貝，audit 需要時直接讀 JSONL）
CREATE TABLE pending_proposals (
    proposal_id TEXT PRIMARY KEY,
    current_revision INTEGER NOT NULL,   -- 本輪新增：對應 proposals.jsonl 的 max(revision)
    type        TEXT NOT NULL,   -- 'decision' | 'constraint'
    record_id   TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status      TEXT NOT NULL,   -- pending | approved | rejected | edited（current revision 的狀態）
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT
);

-- note（本輪改為 revision 化，結構比照 decision；current_revision = max(revision)）
CREATE TABLE note_records (
    id TEXT PRIMARY KEY,
    current_revision INTEGER NOT NULL
);
CREATE TABLE note_revisions (
    id          TEXT NOT NULL REFERENCES note_records(id) ON DELETE CASCADE,
    revision    INTEGER NOT NULL,
    category    TEXT NOT NULL,
    content     TEXT NOT NULL,
    why_persist TEXT NOT NULL,
    importance  REAL NOT NULL,
    confidence  REAL NOT NULL,
    source      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    last_verified_at TEXT NOT NULL,
    expires_at  TEXT,
    source_hashes_json TEXT NOT NULL DEFAULT '{}',  -- 本輪新增：canonical Note 有 source_hashes，
                                                      -- 先前 SQLite 投影漏了這欄，導致 staleness
                                                      -- snapshot 在 derived cache 中消失
    evidence_json TEXT NOT NULL DEFAULT '[]',        -- 本輪新增：完整投影 canonical evidence 欄位，
                                                      -- 供未來 retrieval/inspection 使用
    status      TEXT NOT NULL,     -- active | stale | expired | orphaned | archived
    PRIMARY KEY (id, revision)
);
CREATE TABLE note_scopes (id TEXT, revision INTEGER, scope_id TEXT,
    FOREIGN KEY (id, revision) REFERENCES note_revisions(id, revision) ON DELETE CASCADE);
CREATE TABLE note_files  (id TEXT, revision INTEGER, file TEXT,
    FOREIGN KEY (id, revision) REFERENCES note_revisions(id, revision) ON DELETE CASCADE);
CREATE TABLE note_symbols(id TEXT, revision INTEGER, symbol_id TEXT,
    FOREIGN KEY (id, revision) REFERENCES note_revisions(id, revision) ON DELETE CASCADE);

-- FTS5（materialize.py 全量重建時一併寫入）
CREATE VIRTUAL TABLE fts_semantic USING fts5(scope_id UNINDEXED, text);
CREATE VIRTUAL TABLE fts_decisions USING fts5(record_id UNINDEXED, text);
CREATE VIRTUAL TABLE fts_constraints USING fts5(record_id UNINDEXED, text);
CREATE VIRTUAL TABLE fts_notes USING fts5(note_id UNINDEXED, text);
CREATE VIRTUAL TABLE fts_symbols USING fts5(symbol_id UNINDEXED, text);
```

`materialize.py` 計算 `*_records.current_revision` 時，邏輯必須是單純的
`SELECT MAX(revision) FROM *_revisions WHERE record_id = ?`（或 `id = ?`），**不得**加上
`WHERE status IN (...)` 之類的條件——那正是本輪修正的 bug 來源。可見性過濾只發生在 retrieval 查詢層
（`core.retrieval.search`／`core.retrieval.context`），對 current revision 的 `status` 做第 3、6 節
表格中的判斷，而非發生在「選出哪個 revision 是 current」這一步。

`rebuild-cache` 時，若解析 JSONL 過程中偵測到重複 `(record_id/id, revision)`，中止並回報衝突（見第
1、3、8 節），不寫入任何一筆到 SQLite。

## 6. Staleness 傳播規則

| 物件 | 直接來源變動 | 判斷依據 |
|------|----------------|-----------|
| ScopeSummary | `fresh -> stale`（比對 `source_hash`／`source_files`，見 §2.4 invariant） | 逐次重新計算，非 snapshot |
| Decision | 引用檔案**內容修改**：不變（current 不變）；引用 file/symbol **被刪除**：附加新 revision，`status=review_required`；引用**整個 scope 消失**：附加新 revision，`status=orphaned` | 存在性檢查，非 hash 比對 |
| Constraint，`persistent` | 永不自動變動 | — |
| Constraint，`scope_bound` | 目前 membership hash 與 `scope_hashes` snapshot 不符時，附加新 revision，`status=review_required` | 比對 `scope_hashes` snapshot |
| Constraint，`source_bound` | 目前 content_hash 與 `source_hashes` snapshot 不符時，附加新 revision，`status=stale`；引用項目被刪除時 `status=review_required`；scope 消失時 `status=orphaned` | 比對 `source_hashes` snapshot |
| Constraint，`temporary` | 目前時間超過 `expires_at` 時，附加新 revision，`status=stale`（本輪確定，非 `inactive`——見下方說明） | 比對 `expires_at` |
| Note，`source_bound` 類別（`implementation_detail`，設了 `source_hashes`） | content_hash 與 snapshot 不符：附加新 revision，`status=stale`（仍顯示，標記 `[STALE]`） | 比對 `source_hashes` |
| Note，`temporary_context` / `investigation_result` | 超過 `expires_at`：附加新 revision，`status=expired` | 比對 `expires_at` |
| Note，其餘類別（`pitfall`/`observation`/`workaround`/`known_issue`） | 只有設了 `files`/`symbols`/`source_hashes` 才 source-bound，否則視為 persistent | — |

**關鍵區別**：ScopeSummary 的 staleness 是**每次重新計算的內容雜湊比對**（描述性文字可能因程式碼變動而
不再準確，必須重新產生，沒有「snapshot」的概念，因為它本來就該永遠反映最新狀態）；Decision/Constraint/
Note 的 staleness 是**核准當下的 snapshot 與現況比對**（因為它們代表「某個時間點做的判斷」，只有在
支撐該判斷的具體依據消失或改變時才需要人類重新確認，內容本身不會因為程式碼變動而「被動更正」）。
`core.memory.staleness` 與 `core.semantic.worker` 因此是兩套完全獨立的判斷邏輯，不共用同一份 hash
比對函式，也不共用 snapshot 儲存格式。

**`temporary` Constraint 過期後的狀態，本輪確定為 `stale` 而非 `inactive`**：過期不代表這條規則
「從未存在過」或「肯定不再需要」——它可能仍值得 agent 知道「這條規則原本有效，但期限已過」，而
`stale` 已經定義為「顯示 + warning」，符合這個語意。若人類之後確認真的不再需要，再由人類手動將其
轉為 `inactive`（附加新 revision）——`inactive` 保留給「人類明確停用」，不由系統自動附加。

## 7. Config 檔案模型（`.rune/config.toml`）
```python
class IndexConfig(BaseModel):
    include: list[str]
    exclude: list[str]

class SemanticBudget(BaseModel):
    max_input_tokens_per_run: int

class SemanticConfig(BaseModel):
    enabled: bool = True
    provider: str
    model: str
    budget: SemanticBudget

class NotesConfig(BaseModel):
    temporary_context_ttl_days: int = 7
    investigation_result_ttl_days: int = 30

class SecurityConfig(BaseModel):
    redact_secrets: bool = True

class ProposalsConfig(BaseModel):
    commit_to_git: bool = False

class PricingConfig(BaseModel):
    # 本輪新增：純估算用途，人類手動維護，不追求自動同步真實 provider 定價
    input_per_million: float = 0.0
    output_per_million: float = 0.0

class BootstrapConfig(BaseModel):
    # 本輪新增，見 ARCHITECTURE.md §7.7
    hard_budget_tokens: int = 3000
    soft_budget_tokens: int = 8000
    must_count_warn_threshold: int = 30
    # global MUST 數量超過此值時 `rune doctor` 發出整併建議警告（非硬限制）

class RuneConfig(BaseModel):
    version: int = 1
    index: IndexConfig
    semantic: SemanticConfig
    notes: NotesConfig = NotesConfig()
    security: SecurityConfig = SecurityConfig()
    proposals: ProposalsConfig = ProposalsConfig()
    pricing: PricingConfig = PricingConfig()
    bootstrap: BootstrapConfig = BootstrapConfig()
```

## 8. 已知限制：多人協作下的 revision 衝突（V1 不解決，僅留 invariant）

`revision` 是單純遞增整數，若兩個 git branch 各自從同一 `revision` 出發修改同一筆 Decision/
Constraint/Note，各自產生下一個 `revision`，merge 後 canonical JSONL 會出現同一 `(record_id/id,
revision)` 重複兩行。**V1 明確假設單一寫入者，不處理這個情境的自動合併**。行為定義為：

> V1 assumes a single writer per logical record; duplicate `(record_id, revision)` (or `(id,
> revision)` for notes) is detected as a canonical conflict and `rune doctor`/`rune update` refuses
> to silently resolve it.

未來若需要多人協作，`revision` 應升級為 `revision_id`（例如 ULID）+ `parent_revision_id`，數字型
`revision` 僅作顯示用途；V1 不為此預先設計，避免拖延開發。
