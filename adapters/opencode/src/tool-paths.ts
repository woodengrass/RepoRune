/**
 * Best-effort extraction of the file path(s) a built-in OpenCode tool call
 * is about to touch, from `tool.execute.before`'s `output.args` (typed
 * `any` in the real API -- there is no published schema for built-in
 * tools' argument shapes to check this against, and this has NOT been
 * verified against a live OpenCode host per the user's explicit
 * instruction to skip that verification for this round). Field names
 * (`filePath`/`path`/`file_path`) are a guess at common conventions, not
 * a confirmed contract -- if a real host uses a different key, `rune
 * scope-for` simply never gets called for that tool call (fails open,
 * not silently wrong: no context injected is safer than injecting
 * context for the wrong path).
 */

const PATH_ARG_KEYS = ["filePath", "file_path", "path"];

function firstStringArg(args: unknown, keys: string[]): string | undefined {
  if (typeof args !== "object" || args === null) return undefined;
  const record = args as Record<string, unknown>;
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.length > 0) return value;
  }
  return undefined;
}

/** Matches the file-path markers used by Codex-style `apply_patch` patch
 * text (`*** Add File: <path>`, `*** Update File: <path>`, `*** Delete
 * File: <path>`), plus a plain unified-diff `diff --git a/<path> b/<path>`
 * header as a fallback for other patch formats. */
const APPLY_PATCH_FILE_MARKERS = [
  /^\*\*\* (?:Add|Update|Delete) File: (.+)$/gm,
  /^diff --git a\/(\S+) b\/\S+$/gm,
];

function pathsFromPatchText(patchText: string): string[] {
  const paths = new Set<string>();
  for (const pattern of APPLY_PATCH_FILE_MARKERS) {
    for (const match of patchText.matchAll(pattern)) {
      paths.add(match[1].trim());
    }
  }
  return [...paths];
}

/**
 * Given a tool name and its (pre-execution, still-mutable) arguments,
 * returns the file path(s) this call is about to touch -- empty array if
 * this tool isn't one rune tracks (e.g. `bash`, handled post-hoc instead
 * via `git status`, not here) or no path could be found.
 */
export function extractPathsFromToolArgs(tool: string, args: unknown): string[] {
  switch (tool) {
    case "read":
    case "edit":
    case "write": {
      const path = firstStringArg(args, PATH_ARG_KEYS);
      return path ? [path] : [];
    }
    case "apply_patch": {
      const patchText = firstStringArg(args, ["patchText", "patch", "diff"]);
      return patchText ? pathsFromPatchText(patchText) : [];
    }
    default:
      return [];
  }
}
