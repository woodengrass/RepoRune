import assert from "node:assert/strict";
import { test } from "node:test";

import { parseRuneJson, RuneCliError } from "./rune-cli.js";

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
