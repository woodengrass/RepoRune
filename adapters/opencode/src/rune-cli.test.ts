import assert from "node:assert/strict";
import { test } from "node:test";

import {
  parseRuneJson,
  resolveRuneCliPath,
  RuneCliError,
  validateProposeResult,
  validateScopeForResult,
} from "./rune-cli.js";

test("adapter rejects a missing or incompatible core protocol version", () => {
  assert.throws(
    () => parseRuneJson('{"protocol_version":2}'),
    (error: unknown) => error instanceof RuneCliError && /protocol mismatch/.test(error.message),
  );
  assert.throws(
    () => parseRuneJson('{"scopes":[]}'),
    (error: unknown) => error instanceof RuneCliError && /core returned missing/.test(error.message),
  );
});

test("adapter rejects malformed endpoint payloads after protocol validation", () => {
  assert.throws(
    () => validateScopeForResult({ protocol_version: 1, path: "a.py", scopes: [{}] } as never),
    (error: unknown) => error instanceof RuneCliError && /invalid scope_id/.test(error.message),
  );
  assert.throws(
    () => validateProposeResult({ protocol_version: 1, proposal_id: "p", record_id: "r" } as never, "decision propose"),
    (error: unknown) => error instanceof RuneCliError && /invalid status/.test(error.message),
  );
});

test("an empty CLI override falls back to the default executable", () => {
  assert.equal(resolveRuneCliPath(""), "rune");
  assert.equal(resolveRuneCliPath(undefined), "rune");
  assert.equal(resolveRuneCliPath("rune-dev"), "rune-dev");
});
