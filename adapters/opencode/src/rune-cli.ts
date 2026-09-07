/**
 * The only legal interface between this adapter and rune's core logic:
 * shell out to the `rune` CLI and parse its `--json` output
 * (ARCHITECTURE.md §6 -- "adapter 與 core 之間唯一合法的介面是 CLI 的
 * `--json` 輸出，TS 端絕不直接 import Python 邏輯"). This file must never
 * grow AST parsing, SQLite access, or any decision-making about scopes/
 * constraints/notes -- that's core's job; this only calls it and returns
 * whatever JSON came back.
 */
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

// Overridable via env var so a dev/CI environment can point at a venv's
// rune.exe without requiring a global `pip install`/PATH entry -- the
// real deployment story (Milestone 8 packaging) is expected to put
// `rune` on PATH, at which point this default is what actually runs.
const RUNE_CLI_PATH = process.env.RUNE_CLI_PATH ?? "rune";
export const RUNE_PROTOCOL_VERSION = 1;

export class RuneCliError extends Error {
  constructor(
    message: string,
    public readonly stdout: string,
    public readonly stderr: string,
  ) {
    super(message);
    this.name = "RuneCliError";
  }
}

/**
 * Runs `rune <args...> --json --path <directory>` and parses the JSON
 * stdout. `directory` is OpenCode's own `directory`/`worktree` tool
 * context value -- rune's own `--path` option walks up from there to
 * find the repo root and `.rune/` (this adapter never resolves that
 * itself).
 */
interface ProtocolResponse {
  protocol_version: number;
}

export function parseRuneJson<T extends ProtocolResponse>(stdout: string): T {
  const payload: unknown = JSON.parse(stdout);
  if (
    !payload || typeof payload !== "object" || Array.isArray(payload)
    || !Number.isInteger((payload as ProtocolResponse).protocol_version)
    || (payload as ProtocolResponse).protocol_version !== RUNE_PROTOCOL_VERSION
  ) {
    const version = payload && typeof payload === "object" && !Array.isArray(payload)
      ? (payload as Partial<ProtocolResponse>).protocol_version
      : undefined;
    const actual = version === undefined ? "missing" : String(version);
    throw new RuneCliError(
      `rune core/adapter protocol mismatch: adapter requires ${RUNE_PROTOCOL_VERSION}, core returned ${actual}. Upgrade one side.`,
      stdout,
      "",
    );
  }
  return payload as T;
}

export async function callRuneJson<T extends ProtocolResponse>(directory: string, args: string[]): Promise<T> {
  const fullArgs = [...args, "--json", "--path", directory];
  try {
    const { stdout } = await execFileAsync(RUNE_CLI_PATH, fullArgs, {
      encoding: "utf-8",
      maxBuffer: 10 * 1024 * 1024,
    });
    return parseRuneJson<T>(stdout);
  } catch (err: unknown) {
    const execErr = err as { stdout?: string; stderr?: string; message: string };
    throw new RuneCliError(
      `rune ${fullArgs.join(" ")} failed: ${execErr.message}`,
      execErr.stdout ?? "",
      execErr.stderr ?? "",
    );
  }
}

export interface ScopeForConstraint {
  record_id: string;
  severity: "MUST" | "SHOULD" | "INFO";
  content: string;
  status: string;
  warning: string | null;
}

export interface ScopeForNote {
  id: string;
  category: string;
  content: string;
  status: string;
  warning: string | null;
}

export interface ScopeForScope {
  scope_id: string;
  name: string;
  description: string;
  summary: string | null;
  summary_status: string | null;
  constraints: ScopeForConstraint[];
  notes: ScopeForNote[];
}

export interface ScopeForResult extends ProtocolResponse {
  path: string;
  scopes: ScopeForScope[];
}

/** `rune scope-for <filePath> --json --path <directory>`. */
export async function scopeFor(directory: string, filePath: string): Promise<ScopeForResult> {
  return callRuneJson<ScopeForResult>(directory, ["scope-for", filePath]);
}

export interface HardBootstrapConstraint {
  record_id: string;
  severity: string;
  content: string;
  source_document: string | null;
  source_section: string | null;
}

export interface HardBootstrapDecision {
  record_id: string;
  content: string;
}

export interface HardBootstrapResult extends ProtocolResponse {
  mode: "hard";
  constraints: HardBootstrapConstraint[];
  decisions: HardBootstrapDecision[];
  estimated_tokens: number;
  budget_tokens: number;
  overflow: boolean;
}

/** `rune bootstrap --mode hard --json --path <directory>` (ARCHITECTURE.md
 * §7.3/§7.6). Called on session.created and every session.compacted. */
export async function bootstrapHard(directory: string): Promise<HardBootstrapResult> {
  return callRuneJson<HardBootstrapResult>(directory, ["bootstrap", "--mode", "hard"]);
}

export interface SoftBootstrapScope {
  scope_id: string;
  name: string;
  description: string;
  summary: string | null;
}

export interface SoftBootstrapDecision {
  record_id: string;
  content: string;
}

export interface SoftBootstrapResult extends ProtocolResponse {
  mode: "soft";
  project_name: string | null;
  working_tree_fresh: boolean | null;
  files_indexed: number;
  symbols_indexed: number;
  scopes: SoftBootstrapScope[];
  decisions: SoftBootstrapDecision[];
  estimated_tokens: number;
  budget_tokens: number;
  overflow: boolean;
}

/** `rune bootstrap --mode soft --json --path <directory>` -- called once,
 * on session.created only (ARCHITECTURE.md §7.3: not resent on compaction). */
export async function bootstrapSoft(directory: string): Promise<SoftBootstrapResult> {
  return callRuneJson<SoftBootstrapResult>(directory, ["bootstrap", "--mode", "soft"]);
}

export interface ProposeResult extends ProtocolResponse {
  proposal_id: string;
  record_id: string;
  status: string;
}

export interface DecisionProposeArgs {
  record_id: string;
  content: string;
  rationale?: string;
  scopes?: string[];
  files?: string[];
  symbols?: string[];
  critical?: boolean;
  source_document?: string;
  source_section?: string;
}

/** `rune decision propose <record_id> --content ... --json --path <directory>`
 * -- the `decision_propose` custom tool's only job is to shell out to this. */
export async function decisionPropose(directory: string, args: DecisionProposeArgs): Promise<ProposeResult> {
  const cliArgs = ["decision", "propose", args.record_id, "--content", args.content];
  if (args.rationale) cliArgs.push("--rationale", args.rationale);
  for (const s of args.scopes ?? []) cliArgs.push("--scope", s);
  for (const f of args.files ?? []) cliArgs.push("--file", f);
  for (const sym of args.symbols ?? []) cliArgs.push("--symbol", sym);
  if (args.critical) cliArgs.push("--critical");
  if (args.source_document) cliArgs.push("--source-document", args.source_document);
  if (args.source_section) cliArgs.push("--source-section", args.source_section);
  return callRuneJson<ProposeResult>(directory, cliArgs);
}

export interface ConstraintProposeArgs {
  record_id: string;
  content: string;
  severity: "MUST" | "SHOULD" | "INFO";
  persistence_mode: "persistent" | "scope_bound" | "source_bound" | "temporary";
  rationale?: string;
  scopes?: string[];
  files?: string[];
  symbols?: string[];
  expires_at?: string;
  source_document?: string;
  source_section?: string;
  machine_check_hint?: string;
}

/** `rune constraint propose <record_id> --content ... --severity ... --json
 * --path <directory>` -- the `constraint_propose` custom tool's only job is
 * to shell out to this. */
export async function constraintPropose(directory: string, args: ConstraintProposeArgs): Promise<ProposeResult> {
  const cliArgs = [
    "constraint", "propose", args.record_id, "--content", args.content,
    "--severity", args.severity, "--persistence-mode", args.persistence_mode,
  ];
  if (args.rationale) cliArgs.push("--rationale", args.rationale);
  for (const s of args.scopes ?? []) cliArgs.push("--scope", s);
  for (const f of args.files ?? []) cliArgs.push("--file", f);
  for (const sym of args.symbols ?? []) cliArgs.push("--symbol", sym);
  if (args.expires_at) cliArgs.push("--expires-at", args.expires_at);
  if (args.source_document) cliArgs.push("--source-document", args.source_document);
  if (args.source_section) cliArgs.push("--source-section", args.source_section);
  if (args.machine_check_hint) cliArgs.push("--machine-check-hint", args.machine_check_hint);
  return callRuneJson<ProposeResult>(directory, cliArgs);
}

export interface NoteAddArgs {
  category: string;
  content: string;
  why_persist: string;
  scopes?: string[];
  files?: string[];
  symbols?: string[];
  importance?: number;
  confidence?: number;
  evidence?: string[];
  expires_at?: string;
}

export interface NoteAddResult extends ProtocolResponse {
  id: string;
  category: string;
}

/** `rune note add --category ... --content ... --why-persist ... --json
 * --path <directory>` -- the `note_add` custom tool's only job is to shell
 * out to this. No approval gate on the rune side, so this writes immediately. */
export async function noteAdd(directory: string, args: NoteAddArgs): Promise<NoteAddResult> {
  const cliArgs = [
    "note", "add", "--category", args.category, "--content", args.content,
    "--why-persist", args.why_persist,
  ];
  for (const s of args.scopes ?? []) cliArgs.push("--scope", s);
  for (const f of args.files ?? []) cliArgs.push("--file", f);
  for (const sym of args.symbols ?? []) cliArgs.push("--symbol", sym);
  if (args.importance !== undefined) cliArgs.push("--importance", String(args.importance));
  if (args.confidence !== undefined) cliArgs.push("--confidence", String(args.confidence));
  for (const e of args.evidence ?? []) cliArgs.push("--evidence", e);
  if (args.expires_at) cliArgs.push("--expires-at", args.expires_at);
  return callRuneJson<NoteAddResult>(directory, cliArgs);
}

/** Every file `git status --porcelain` reports as changed (modified,
 * added, deleted, renamed, or untracked) relative to the working tree --
 * used post-hoc after a `bash` tool call, since V1 deliberately never
 * tries to parse shell command semantics to predict what a `bash` call
 * will touch (ARCHITECTURE.md §6). Not `rune check`'s own diff (that
 * compares against the last `rune update`, not "what did this one bash
 * call just change") -- this needs "what changed very recently", which is
 * exactly what `git status` reports regardless of whether `rune update`
 * has run since.
 */
export async function changedFilesFromGitStatus(directory: string): Promise<string[]> {
  const { stdout } = await execFileAsync(
    "git", ["-c", "core.quotePath=false", "-C", directory, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
    { encoding: "utf-8", maxBuffer: 10 * 1024 * 1024 },
  );
  const paths: string[] = [];
  const entries = stdout.split("\0");
  for (let index = 0; index < entries.length; index += 1) {
    const line = entries[index];
    if (line.length < 4) continue;
    // In -z porcelain v1 a rename is "XY new\0old\0"; retain the destination.
    paths.push(line.slice(3));
    if (line[0] === "R" || line[0] === "C" || line[1] === "R" || line[1] === "C") index += 1;
  }
  return paths;
}
