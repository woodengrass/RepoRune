/**
 * Milestone 7's required pre-work (ARCHITECTURE.md §6, IMPLEMENTATION_
 * PLAN.md's Milestone 7 intro): verify the single most important path --
 * `tool.execute.before` -> `rune scope-for --path ... --json` -> inject
 * context -- actually works end to end, before building out the full
 * adapter (session hooks, hard/soft bootstrap, custom tools).
 *
 * This is NOT a real OpenCode plugin: OpenCode's `tool.execute.before`
 * hook isn't reachable from a standalone script, so this simulates the
 * one thing that matters for the spike -- given a `directory` (what
 * OpenCode's tool context provides) and a file path a tool is about to
 * touch, can this TypeScript code locate the right `.rune/` and get back
 * real scope/constraint/note data through nothing but the `rune` CLI's
 * `--json` output? `activeScopeIds` mirrors the real adapter's dedup
 * rule (ARCHITECTURE.md §6: inject a scope's context once per session,
 * not on every tool call).
 */
import { scopeFor } from "./rune-cli.js";

const activeScopeIds = new Set<string>();

async function simulateToolExecuteBefore(directory: string, filePath: string): Promise<void> {
  const result = await scopeFor(directory, filePath);
  if (result.scopes.length === 0) {
    console.log(`[tool.execute.before] ${filePath}: not a member of any scope, nothing to inject`);
    return;
  }
  for (const scope of result.scopes) {
    if (activeScopeIds.has(scope.scope_id)) {
      console.log(`[tool.execute.before] ${filePath}: scope "${scope.scope_id}" already injected this session, skipping`);
      continue;
    }
    activeScopeIds.add(scope.scope_id);
    console.log(`[tool.execute.before] ${filePath}: injecting context for scope "${scope.scope_id}"`);
    if (scope.summary) {
      console.log(`  summary [${scope.summary_status}]: ${scope.summary}`);
    }
    for (const c of scope.constraints) {
      console.log(`  [${c.severity}] ${c.record_id}: ${c.content}`);
    }
    for (const n of scope.notes) {
      console.log(`  note [${n.category}]: ${n.content}`);
    }
  }
}

async function main(): Promise<void> {
  const [directory, ...filePaths] = process.argv.slice(2);
  if (!directory || filePaths.length === 0) {
    console.error("usage: node dist/spike.js <repo-directory> <file-path> [file-path ...]");
    process.exitCode = 1;
    return;
  }
  for (const filePath of filePaths) {
    await simulateToolExecuteBefore(directory, filePath);
  }
  // second pass over the same paths proves the dedup actually skips
  console.log("--- second pass (should skip already-injected scopes) ---");
  for (const filePath of filePaths) {
    await simulateToolExecuteBefore(directory, filePath);
  }
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
