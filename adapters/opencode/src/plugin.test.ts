import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import {
  createRuneHooks,
  findRuneProjectDirectory,
  isRuneProject,
  MAX_SCOPE_ACTIVATIONS,
  MAX_SESSION_ADMISSIONS,
  pathForScopeLookup,
  pluginDirectoryPath,
  type RuneClient,
} from "./plugin.js";
import type { HardBootstrapResult, ScopeForResult, SoftBootstrapResult } from "./rune-cli.js";

function fakeClient(overrides: Partial<RuneClient> = {}): RuneClient {
  const hard: HardBootstrapResult = {
    protocol_version: 1,
    mode: "hard",
    constraints: [{ record_id: "no-bare-except", severity: "MUST", content: "no bare except", source_document: null, source_section: null }],
    decisions: [],
    estimated_tokens: 10,
    budget_tokens: 3000,
    overflow: false,
  };
  const soft: SoftBootstrapResult = {
    protocol_version: 1,
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
      protocol_version: 1,
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
        protocol_version: 1,
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

test("system.transform fails open when the host supplies a non-array system field", async () => {
  const warnings: Record<string, unknown>[] = [];
  const hooks = createRuneHooks("/repo", fakeClient(), {
    warningLog: async (_message, extra) => { warnings.push(extra); },
  });
  await hooks.event!({ event: { type: "session.created", properties: { info: { id: "s1" } as never } } });

  await hooks["experimental.chat.system.transform"]!(
    { sessionID: "s1", model: {} as never },
    { system: "invalid host value" } as never,
  );

  assert.equal(warnings.length, 1);
  assert.equal(warnings[0].hook, "experimental.chat.system.transform");
});

test("session.deleted releases its Rune session state", async () => {
  const hooks = createRuneHooks("/repo", fakeClient());
  await hooks.event!({ event: { type: "session.created", properties: { info: { id: "s1" } as never } } });
  await hooks.event!({ event: { type: "session.deleted", properties: { info: { id: "s1" } as never } } });

  const output = { system: [] as string[] };
  await hooks["experimental.chat.system.transform"]!({ sessionID: "s1", model: {} as never }, output);
  assert.deepEqual(output.system, []);
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

test("a nested plugin directory resolves to its enclosing Rune project", async () => {
  const root = await mkdtemp(join(tmpdir(), "rune-opencode-plugin-nested-"));
  const nested = join(root, "workspace", "src");
  try {
    await mkdir(join(root, ".rune"));
    await mkdir(nested, { recursive: true });

    assert.equal(await isRuneProject(nested), false);
    assert.equal(await findRuneProjectDirectory(nested), root);
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

test("concurrent tool calls for the same path share one scope lookup", async () => {
  let scopeCalls = 0;
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const hooks = createRuneHooks("/repo", fakeClient({
    scopeFor: async (_directory, path) => {
      scopeCalls += 1;
      await gate;
      return fakeClient().scopeFor("/repo", path);
    },
  }));
  await hooks.event!({ event: { type: "session.created", properties: { info: { id: "s1" } as never } } });

  const first = hooks["tool.execute.before"]!(
    { tool: "read", sessionID: "s1", callID: "c1" },
    { args: { filePath: "app/services.py" } },
  );
  const second = hooks["tool.execute.before"]!(
    { tool: "read", sessionID: "s1", callID: "c2" },
    { args: { filePath: "app/services.py" } },
  );
  release();
  await Promise.all([first, second]);

  assert.equal(scopeCalls, 1);
});

test("non-bash scope activation is capped for multi-file tools", async () => {
  let scopeCalls = 0;
  const hooks = createRuneHooks("/repo", fakeClient({
    scopeFor: async (_directory, path) => {
      scopeCalls += 1;
      return fakeClient().scopeFor("/repo", path);
    },
  }));
  await hooks.event!({ event: { type: "session.created", properties: { info: { id: "s1" } as never } } });

  const patchText = Array.from(
    { length: MAX_SCOPE_ACTIVATIONS + 1 },
    (_, index) => `*** Update File: app/file-${index}.py`,
  ).join("\n");
  await hooks["tool.execute.before"]!(
    { tool: "apply_patch", sessionID: "s1", callID: "c1" },
    { args: { patchText } },
  );

  assert.equal(scopeCalls, MAX_SCOPE_ACTIVATIONS);
});

test("session bootstrap is registered before an awaited event diagnostic", async () => {
  let release!: () => void;
  let eventLogStarted!: () => void;
  const eventLogReady = new Promise<void>((resolve) => { eventLogStarted = resolve; });
  const eventLogGate = new Promise<void>((resolve) => { release = resolve; });
  const hooks = createRuneHooks("/repo", fakeClient(), {
    acceptanceLog: async (message) => {
      if (message === "event") {
        eventLogStarted();
        await eventLogGate;
      }
    },
  });

  const created = hooks.event!({ event: { type: "session.created", properties: { info: { id: "s1" } as never } } });
  await eventLogReady;
  const outputPromise = fireSystemTransform(hooks, "s1");
  let transformed = false;
  void outputPromise.then(() => { transformed = true; });
  await Promise.resolve();
  assert.equal(transformed, false);

  release();
  await Promise.all([created, outputPromise]);
});

test("deleting a session while bootstrap is pending does not recreate its state", async () => {
  let release!: () => void;
  const bootstrapGate = new Promise<void>((resolve) => { release = resolve; });
  const hooks = createRuneHooks("/repo", fakeClient({
    bootstrapHard: async () => {
      await bootstrapGate;
      return fakeClient().bootstrapHard("/repo");
    },
  }));

  const created = hooks.event!({ event: { type: "session.created", properties: { info: { id: "s1" } as never } } });
  await Promise.resolve();
  const deleted = hooks.event!({ event: { type: "session.deleted", properties: { info: { id: "s1" } as never } } });
  await deleted;
  release();
  await created;

  const output = { system: [] as string[] };
  await hooks["experimental.chat.system.transform"]!({ sessionID: "s1", model: {} as never }, output);
  assert.deepEqual(output.system, []);
});

test("deleting a session while scope activation is pending does not recreate its state", async () => {
  let release!: () => void;
  let scopeStarted!: () => void;
  const scopeStartedReady = new Promise<void>((resolve) => { scopeStarted = resolve; });
  const scopeGate = new Promise<void>((resolve) => { release = resolve; });
  const hooks = createRuneHooks("/repo", fakeClient({
    scopeFor: async (directory, path) => {
      scopeStarted();
      await scopeGate;
      return fakeClient().scopeFor(directory, path);
    },
  }));

  await hooks.event!({ event: { type: "session.created", properties: { info: { id: "s1" } as never } } });
  const activation = hooks["tool.execute.before"]!(
    { tool: "read", sessionID: "s1", callID: "c1" },
    { args: { filePath: "app/services.py" } },
  );
  await scopeStartedReady;
  await hooks.event!({ event: { type: "session.deleted", properties: { info: { id: "s1" } as never } } });
  release();
  await activation;

  const output = { system: [] as string[] };
  await hooks["experimental.chat.system.transform"]!({ sessionID: "s1", model: {} as never }, output);
  assert.deepEqual(output.system, []);
});

test("session admission cap fails open without evicting existing state", async () => {
  let hardCalls = 0;
  const warnings: Record<string, unknown>[] = [];
  const hooks = createRuneHooks("/repo", fakeClient({
    bootstrapHard: async () => {
      hardCalls += 1;
      return fakeClient().bootstrapHard("/repo");
    },
  }), {
    warningLog: async (_message, extra) => { warnings.push(extra); },
  });

  for (let index = 0; index < MAX_SESSION_ADMISSIONS + 1; index += 1) {
    await hooks.event!({ event: { type: "session.created", properties: { info: { id: `s${index}` } as never } } });
  }

  assert.equal(hardCalls, MAX_SESSION_ADMISSIONS);
  assert.equal(warnings.length, 1);
  assert.equal(warnings[0].max_sessions, MAX_SESSION_ADMISSIONS);
  assert.match(String(warnings[0].remediation), /Reload/);
});
