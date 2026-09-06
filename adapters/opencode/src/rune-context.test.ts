import assert from "node:assert/strict";
import { test } from "node:test";

import { mergeRuneBlock, RuneSessionContext } from "./rune-context.js";
import type { HardBootstrapResult, ScopeForScope } from "./rune-cli.js";

function hard(overrides: Partial<HardBootstrapResult> = {}): HardBootstrapResult {
  return {
    mode: "hard",
    constraints: [],
    decisions: [],
    estimated_tokens: 0,
    budget_tokens: 3000,
    overflow: false,
    ...overrides,
  };
}

function scope(id: string): ScopeForScope {
  return {
    scope_id: id,
    name: id,
    description: "",
    summary: null,
    summary_status: null,
    constraints: [{ record_id: `${id}-c1`, severity: "MUST", content: `${id} rule`, status: "active", warning: null }],
    notes: [],
  };
}

test("global MUST constraints render on every call, not just the first", () => {
  const ctx = new RuneSessionContext();
  ctx.setHardBootstrap(hard({ constraints: [{ record_id: "c1", severity: "MUST", content: "no bare except", source_document: null, source_section: null }] }));

  const first = ctx.render();
  const second = ctx.render();
  assert.match(first, /no bare except/);
  assert.match(second, /no bare except/);
});

test("active scoped constraints persist across subsequent renders", () => {
  const ctx = new RuneSessionContext();
  ctx.addActiveScope(scope("app"));

  const first = ctx.render();
  const second = ctx.render();
  assert.match(first, /app-c1/);
  assert.match(second, /app-c1/);
});

test("a second addActiveScope call for the same scope id is a no-op", () => {
  const ctx = new RuneSessionContext();
  assert.equal(ctx.addActiveScope(scope("app")), true);
  assert.equal(ctx.addActiveScope(scope("app")), false);
});

test("pending events are drained after one render, not repeated", () => {
  const ctx = new RuneSessionContext();
  ctx.enqueuePendingEvent("project overview text");

  const first = ctx.render();
  const second = ctx.render();
  assert.match(first, /project overview text/);
  assert.doesNotMatch(second, /project overview text/);
});

test("mergeRuneBlock does not grow the system array on repeated calls (no duplicate accumulation)", () => {
  const system: string[] = [];
  mergeRuneBlock(system, "block A");
  mergeRuneBlock(system, "block B");
  mergeRuneBlock(system, "block C");

  assert.equal(system.length, 1);
  assert.match(system[0], /block C/);
  assert.doesNotMatch(system[0], /block A/);
  assert.doesNotMatch(system[0], /block B/);
});

test("mergeRuneBlock never produces more than one system-role entry when one already existed", () => {
  const system: string[] = ["existing system prompt from opencode itself"];
  mergeRuneBlock(system, "rune content");
  mergeRuneBlock(system, "rune content v2");

  assert.equal(system.length, 1);
  assert.match(system[0], /existing system prompt from opencode itself/);
  assert.match(system[0], /rune content v2/);
  assert.doesNotMatch(system[0], /rune content(?! v2)/);
});

test("mergeRuneBlock with an empty block and no prior marker is a no-op", () => {
  const system = ["untouched"];
  mergeRuneBlock(system, "");
  assert.deepEqual(system, ["untouched"]);
});

test("compaction rehydrates hard bootstrap: setHardBootstrap replaces, not appends", () => {
  const ctx = new RuneSessionContext();
  ctx.setHardBootstrap(hard({ constraints: [{ record_id: "old", severity: "MUST", content: "old rule", source_document: null, source_section: null }] }));
  ctx.render();

  // Simulate session.compacted: hard bootstrap is re-fetched and replaces
  // the old set (a differently-shaped set post-compaction, e.g. a rule
  // was deactivated in the meantime).
  ctx.setHardBootstrap(hard({ constraints: [{ record_id: "new", severity: "MUST", content: "new rule", source_document: null, source_section: null }] }));
  const rendered = ctx.render();

  assert.match(rendered, /new rule/);
  assert.doesNotMatch(rendered, /old rule/);
});

test("separate RuneSessionContext instances do not leak state into each other", () => {
  const sessionA = new RuneSessionContext();
  const sessionB = new RuneSessionContext();

  sessionA.addActiveScope(scope("app"));
  sessionA.enqueuePendingEvent("session A event");

  const renderedA = sessionA.render();
  const renderedB = sessionB.render();

  assert.match(renderedA, /app-c1/);
  assert.match(renderedA, /session A event/);
  assert.equal(renderedB, "");
});

test("hard bootstrap overflow is surfaced as a warning, never as a dropped constraint", () => {
  const ctx = new RuneSessionContext();
  ctx.setHardBootstrap(hard({
    constraints: [{ record_id: "c1", severity: "MUST", content: "rule", source_document: null, source_section: null }],
    estimated_tokens: 5000,
    budget_tokens: 3000,
    overflow: true,
  }));
  const rendered = ctx.render();
  assert.match(rendered, /rule/);
  assert.match(rendered, /overflow/i);
});
