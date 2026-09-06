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
export async function callRuneJson<T>(directory: string, args: string[]): Promise<T> {
  const fullArgs = [...args, "--json", "--path", directory];
  try {
    const { stdout } = await execFileAsync(RUNE_CLI_PATH, fullArgs, {
      encoding: "utf-8",
      maxBuffer: 10 * 1024 * 1024,
    });
    return JSON.parse(stdout) as T;
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

export interface ScopeForResult {
  path: string;
  scopes: ScopeForScope[];
}

/** `rune scope-for <filePath> --json --path <directory>`. */
export async function scopeFor(directory: string, filePath: string): Promise<ScopeForResult> {
  return callRuneJson<ScopeForResult>(directory, ["scope-for", filePath]);
}
