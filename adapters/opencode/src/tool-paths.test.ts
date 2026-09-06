import assert from "node:assert/strict";
import { test } from "node:test";

import { extractPathsFromToolArgs } from "./tool-paths.js";

test("edit/write/read tools yield their filePath argument", () => {
  assert.deepEqual(extractPathsFromToolArgs("edit", { filePath: "app/services.py" }), ["app/services.py"]);
  assert.deepEqual(extractPathsFromToolArgs("write", { path: "app/models.py" }), ["app/models.py"]);
  assert.deepEqual(extractPathsFromToolArgs("read", { file_path: "app/utils.py" }), ["app/utils.py"]);
});

test("apply_patch extracts every Add/Update/Delete File marker", () => {
  const patchText = [
    "*** Begin Patch",
    "*** Update File: app/services.py",
    "@@",
    "-old",
    "+new",
    "*** Add File: app/new_module.py",
    "+content",
    "*** Delete File: app/old_module.py",
    "*** End Patch",
  ].join("\n");
  assert.deepEqual(
    extractPathsFromToolArgs("apply_patch", { patchText }).sort(),
    ["app/new_module.py", "app/old_module.py", "app/services.py"],
  );
});

test("bash yields no paths -- handled post-hoc via git status, not here", () => {
  assert.deepEqual(extractPathsFromToolArgs("bash", { command: "rm -rf /tmp/x" }), []);
});

test("unknown args shape yields no paths instead of throwing", () => {
  assert.deepEqual(extractPathsFromToolArgs("edit", null), []);
  assert.deepEqual(extractPathsFromToolArgs("edit", "not an object"), []);
  assert.deepEqual(extractPathsFromToolArgs("edit", {}), []);
});
