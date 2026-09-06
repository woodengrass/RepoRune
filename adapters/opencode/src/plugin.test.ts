import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { createRuneHooks, isRuneProject, pathForScopeLookup, pluginDirectoryPath, type RuneClient } from "./plugin.js";
import type { HardBootstrapResult, ScopeForResult, SoftBootstrapResult } from "./rune-cli.js";

function fakeClient(overrides: Partial<RuneClient> = {}): RuneClient {
  const hard: HardBootstrapResult = {
    mode: "hard",
    constraints: [{ record_id: "no-bare-except", severity: "MUST", content: "no bare except", source_document: null, source_section: null }],
    decisions: [],
    estimated_tokens: 10,
    budget_tokens: 3000,
    overflow: false,
  };
  const soft: SoftBootstrapResult = {
    mode: "soft",
    project_name: "demo",
    working_tree_fresh: true,
    files_indexed: 3,
    symbols_indexed: 10,
    scopes: [],
    decisions: [],
    estimated_tokens: 5,
    budget_tokens: 8000,
    overflow: false,
  };
  return {
    scopeFor: async (_directory, path) => ({
      path,
      scopes: [{
        scope_id: "app", name: "App", description: "", summary: null, summary_status: null,
        constraints: [{ record_id: "app-rule", severity: "SHOULD", content: "prefer repository pattern", status: "active", warning: null }],
        notes: [],
      }],
    } satisfies ScopeForResult),
    bootstrapHard: async () => hard,
    bootstrapSoft: async () => soft,
    changedFilesFromGitStatus: async () => [],
    ...overrides,
  };
}

async function fireSystemTransform(hooks: ReturnType<typeof createRuneHooks>, sessionID: string): Promise<string[]> {
  const output = { system: [] as string[] };
  await hooks["experimental.chat.system.transform"]!({ sessionID, model: {} as never }, output);
  return output.system;
}

test("global MUST constraint is present on every LLM call after session.created", async () => {
  const hooks = createRuneHooks("/repo", fakeClient());
  await hooks.event!({ event: { type: "session.created", properties: { info: { id: "s1" } as never } } });

  const firstCall = await fireSystemTransform(hooks, "s1");
  const secondCall = await fireSystemTransform(hooks, "s1");

  assert.match(firstCall[0], /no bare except/);
  assert.match(secondCall[0], /no bare except/);
});

test("active scoped constraints persist across subsequent LLM calls", async () => {
  const hooks = createRuneHooks("/repo", fakeClient());
  await hooks.event!({ event: { type: "session.created", properties: { info: { id: "s1" } as never } } });
  await hooks["tool.execute.before"]!({ tool: "edit", sessionID: "s1", callID: "c1" }, { args: { filePath: "app/services.py" } });

  const firstCall = await fireSystemTransform(hooks, "s1");
  const secondCall = await fireSystemTransform(hooks, "s1");

  assert.match(firstCall[0], /prefer repository pattern/);
  assert.match(secondCall[0], /prefer repository pattern/);
});

test("no duplicate Rune block accumulation across repeated system.transform calls", async () => {
  const hooks = createRuneHooks("/repo", fakeClient());
  await hooks.event!({ event: { type: "session.created", properties: { info: { id: "s1" } as never } } });

  await fireSystemTransform(hooks, "s1");
  await fireSystemTransform(hooks, "s1");
  const thirdCall = await fireSystemTransform(hooks, "s1");

  assert.equal(thirdCall.length, 1);
  const occurrences = thirdCall[0].split("no bare except").length - 1;
  assert.equal(occurrences, 1);
});

test("compaction rehydrates the system context with a freshly re-fetched hard bootstrap", async () => {
  let hardCallCount = 0;
  const client = fakeClient({
    bootstrapHard: async () => {
      hardCallCount += 1;
      return {
        mode: "hard",
        constraints: [{
          record_id: hardCallCount === 1 ? "rule-v1" : "rule-v2",
          severity: "MUST", content: hardCallCount === 1 ? "rule v1" : "rule v2",
          source_document: null, source_section: null,
        }],
        decisions: [], estimated_tokens: 5, budget_tokens: 3000, overflow: false,
      };
    },
  });
  const hooks = createRuneHooks("/repo", client);
  await hooks.event!({ event: { type: "session.created", properties: { info: { id: "s1" } as never } } });
  await fireSystemTransform(hooks, "s1");

  await hooks.event!({ event: { type: "session.compacted", properties: { sessionID: "s1" } } });
  const afterCompaction = await fireSystemTransform(hooks, "s1");

  assert.match(afterCompaction[0], /rule v2/);
  assert.doesNotMatch(afterCompaction[0], /rule v1/);
  assert.equal(hardCallCount, 2);
});

test("system.transform never produces more than one system-role entry, even alongside an existing one", async () => {
  const hooks = createRuneHooks("/repo", fakeClient());
  await hooks.event!({ event: { type: "session.created", properties: { info: { id: "s1" } as never } } });

  const output = { system: ["opencode's own base system prompt"] };
  await hooks["experimental.chat.system.transform"]!({ sessionID: "s1", model: {} as never }, output);
  await hooks["experimental.chat.system.transform"]!({ sessionID: "s1", model: {} as never }, output);

  assert.equal(output.system.length, 1);
  assert.match(output.system[0], /opencode's own base system prompt/);
  assert.match(output.system[0], /no bare except/);
});

test("separate sessions do not leak Rune state into each other", async () => {
  const hooks = createRuneHooks("/repo", fakeClient());
  await hooks.event!({ event: { type: "session.created", properties: { info: { id: "s1" } as never } } });
  await hooks["tool.execute.before"]!({ tool: "edit", sessionID: "s1", callID: "c1" }, { args: { filePath: "app/services.py" } });

  // session "s2" never saw session.created or any tool call -- system.transform
  // for it should be a no-op (nothing to inject yet), not leak s1's scope data.
  const s2Output = { system: [] as string[] };
  await hooks["experimental.chat.system.transform"]!({ sessionID: "s2", model: {} as never }, s2Output);

  assert.deepEqual(s2Output.system, []);
});

test("acceptance sentinel is injected only when explicitly enabled", async () => {
  const hooks = createRuneHooks("/repo", fakeClient(), { acceptanceSentinel: "RUNE_SENTINEL_7A91F" });
  await hooks.event!({ event: { type: "session.created", properties: { info: { id: "s1" } as never } } });

  const output = await fireSystemTransform(hooks, "s1");
  assert.match(output[0], /RUNE_HOST_ACCEPTANCE_TEST/);
  assert.match(output[0], /RUNE_SENTINEL_7A91F/);
});

test("soft bootstrap is rendered once and then drained", async () => {
  const hooks = createRuneHooks("/repo", fakeClient());
  await hooks.event!({ event: { type: "session.created", properties: { info: { id: "s1" } as never } } });

  const first = await fireSystemTransform(hooks, "s1");
  const second = await fireSystemTransform(hooks, "s1");
  assert.match(first[0], /Project: demo/);
  assert.doesNotMatch(second[0], /Project: demo/);
});

test("title generation leaves Rune state untouched until the first normal request", async () => {
  const hooks = createRuneHooks("/repo", fakeClient());
  await hooks.event!({ event: { type: "session.created", properties: { info: { id: "s1" } as never } } });
  await hooks["tool.execute.before"]!(
    { tool: "read", sessionID: "s1", callID: "c1" },
    { args: { filePath: "app/services.py" } },
  );

  const titleOutput = {
    system: ["You are a title generator. You output ONLY a thread title. Nothing else."],
  };
  await hooks["experimental.chat.system.transform"]!({ sessionID: "s1", model: {} as never }, titleOutput);
  assert.equal(titleOutput.system.length, 1);
  assert.doesNotMatch(titleOutput.system[0], /rune-context/);

  const firstNormal = await fireSystemTransform(hooks, "s1");
  assert.match(firstNormal[0], /Project: demo/);
  assert.match(firstNormal[0], /no bare except/);
  assert.match(firstNormal[0], /prefer repository pattern/);

  const secondNormal = await fireSystemTransform(hooks, "s1");
  assert.doesNotMatch(secondNormal[0], /Project: demo/);
  assert.match(secondNormal[0], /no bare except/);
  assert.match(secondNormal[0], /prefer repository pattern/);
});

test("a partial title marker remains a normal request", async () => {
  const hooks = createRuneHooks("/repo", fakeClient());
  await hooks.event!({ event: { type: "session.created", properties: { info: { id: "s1" } as never } } });

  const output = { system: ["You are a title generator."] };
  await hooks["experimental.chat.system.transform"]!({ sessionID: "s1", model: {} as never }, output);
  assert.match(output.system[0], /rune-context/);
  assert.match(output.system[0], /Project: demo/);
});

test("Rune project detection only enables directories with a .rune directory", async () => {
  const root = await mkdtemp(join(tmpdir(), "rune-opencode-plugin-"));
  try {
    assert.equal(await isRuneProject(root), false);
    await mkdir(join(root, ".rune"));
    assert.equal(await isRuneProject(root), true);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("host paths are converted to Rune's repo-relative CLI contract", () => {
  assert.equal(
    pathForScopeLookup("C:\\repo", "C:\\repo\\src\\service.py"),
    "src/service.py",
  );
  assert.equal(pathForScopeLookup("C:\\repo", "src\\service.py"), "src/service.py");
  assert.equal(pathForScopeLookup("C:\\repo", "C:\\outside\\secret.py"), null);
});

test("plugin directory boundary accepts host directory objects and rejects unknown shapes", () => {
  assert.equal(pluginDirectoryPath("C:/repo"), "C:/repo");
  assert.equal(pluginDirectoryPath({ directory: "C:/repo" }), "C:/repo");
  assert.equal(pluginDirectoryPath({ path: "C:/repo" }), "C:/repo");
  assert.equal(pluginDirectoryPath({ unknown: "C:/repo" }), null);
});

test("CLI failures in lifecycle hooks fail open and still deliver independent soft bootstrap", async () => {
  const hooks = createRuneHooks("/repo", fakeClient({ bootstrapHard: async () => { throw new Error("rune unavailable"); } }));
  await hooks.event!({ event: { type: "session.created", properties: { info: { id: "s1" } as never } } });
  const output = await fireSystemTransform(hooks, "s1");
  assert.match(output[0], /Project: demo/);
});

test("bash post-hook activates changed scopes without blocking the bash call", async () => {
  let scopeCalls = 0;
  const hooks = createRuneHooks("/repo", fakeClient({
    changedFilesFromGitStatus: async () => ["app/services.py"],
    scopeFor: async (_directory, path) => {
      scopeCalls += 1;
      return fakeClient().scopeFor("/repo", path);
    },
  }));
  await hooks.event!({ event: { type: "session.created", properties: { info: { id: "s1" } as never } } });
  await hooks["tool.execute.after"]!({ tool: "bash", sessionID: "s1", callID: "c1", args: {} }, {} as never);
  await hooks["tool.execute.after"]!({ tool: "bash", sessionID: "s1", callID: "c2", args: {} }, {} as never);
  const output = await fireSystemTransform(hooks, "s1");
  assert.match(output[0], /prefer repository pattern/);
  assert.equal(scopeCalls, 1);
});
