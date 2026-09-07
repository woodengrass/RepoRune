/**
 * The real OpenCode plugin entry point (Milestone 7 full build, following
 * the spike in spike.ts). Wires the pure `RuneSessionContext` state
 * manager (rune-context.ts) and the CLI wrappers (rune-cli.ts) into the
 * real `Hooks` interface from `@opencode-ai/plugin` v1.18.29's classic
 * (non-v2/effect) API -- confirmed with the user as the target surface
 * after the real type definitions turned out to differ from what
 * ARCHITECTURE.md §6 originally assumed (see ARCHITECTURE.md's
 * fifteenth-round note).
 *
 * `directory`/`worktree` are captured once, at plugin load, from
 * `PluginInput` -- the CWD doesn't change within a single session, and
 * neither `tool.execute.before`/`.after` nor `event` carry it themselves
 * (confirmed against the real type definitions, unlike what was assumed
 * before this round).
 */
import type { Hooks, Plugin, PluginInput } from "@opencode-ai/plugin";
import { tool } from "@opencode-ai/plugin/tool";
import type { Event } from "@opencode-ai/sdk";
import { stat } from "node:fs/promises";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

import {
  bootstrapHard,
  bootstrapSoft,
  changedFilesFromGitStatus,
  constraintPropose,
  decisionPropose,
  noteAdd,
  scopeFor,
  type HardBootstrapResult,
  type ScopeForResult,
  type SoftBootstrapResult,
} from "./rune-cli.js";
import { mergeRuneBlock, renderSoftBootstrap, RuneSessionContext } from "./rune-context.js";
import { extractPathsFromToolArgs } from "./tool-paths.js";

const TITLE_GENERATION_SYSTEM_MARKER =
  "You are a title generator. You output ONLY a thread title. Nothing else.";
const RUNE_PLUGIN_BUILD = "directory-normalization-host-debug-20260907-a";

/**
 * The subset of rune-cli.ts this module calls, factored out as an
 * injectable interface purely so tests can supply a fake instead of
 * shelling out to a real `rune` CLI (see plugin.test.ts) -- production
 * code always gets `defaultRuneClient` below, which is a thin pass-
 * through to the real CLI-backed functions.
 */
export interface RuneClient {
  scopeFor(directory: string, path: string): Promise<ScopeForResult>;
  bootstrapHard(directory: string): Promise<HardBootstrapResult>;
  bootstrapSoft(directory: string): Promise<SoftBootstrapResult>;
  changedFilesFromGitStatus(directory: string): Promise<string[]>;
}

interface RuneHookOptions {
  acceptanceLog?: (message: string, extra: Record<string, unknown>) => Promise<void>;
  warningLog?: (message: string, extra: Record<string, unknown>) => Promise<void>;
  acceptanceSentinel?: string;
}

const defaultRuneClient: RuneClient = {
  scopeFor, bootstrapHard, bootstrapSoft, changedFilesFromGitStatus,
};

/**
 * Builds the `Hooks` object for a fixed `directory`/`worktree` -- split
 * out from the `Plugin` factory export below purely so tests can
 * construct one directly without going through OpenCode's own plugin
 * loading machinery.
 */
export function createRuneHooks(
  directory: string,
  client: RuneClient = defaultRuneClient,
  options: RuneHookOptions = {},
): Hooks {
  const sessions = new Map<string, RuneSessionContext>();
  const activatedPaths = new Map<string, Set<string>>();
  const warnedHooks = new Set<string>();
  let systemTransformCount = 0;

  const logAcceptance = async (message: string, extra: Record<string, unknown>): Promise<void> => {
    try {
      await options.acceptanceLog?.(message, extra);
    } catch {
      // Acceptance logging is diagnostic only.
    }
  };

  const failOpen = async (hook: string, error: unknown, extra: Record<string, unknown> = {}): Promise<void> => {
    const errorType = error instanceof Error ? error.name : typeof error;
    try {
      await logAcceptance("hook.error", {
        hook,
        error_type: errorType,
        ...extra,
      });
    } catch {
      // Observability must never make an injection hook block the host.
    }
    if (!warnedHooks.has(hook)) {
      warnedHooks.add(hook);
      try {
        await options.warningLog?.("Rune hook failed open; context injection was skipped", {
          hook, error_type: errorType, ...extra,
        });
      } catch {
        // Logging failures must not escape a fail-open hook.
      }
    }
  };

  const getSession = (sessionID: string): RuneSessionContext => {
    let ctx = sessions.get(sessionID);
    if (!ctx) {
      ctx = new RuneSessionContext();
      sessions.set(sessionID, ctx);
    }
    return ctx;
  };

  async function seedHardBootstrap(sessionID: string): Promise<void> {
    const hard = await client.bootstrapHard(directory);
    getSession(sessionID).setHardBootstrap(hard);
  }

  async function activateScopesForPath(sessionID: string, path: string): Promise<void> {
    const scopePath = pathForScopeLookup(directory, path);
    if (!scopePath) {
      await logAcceptance("scope-activation.skipped", {
        session_id: sessionID,
        path,
        reason: "path_outside_repository",
      });
      return;
    }
    const seenPaths = activatedPaths.get(sessionID) ?? new Set<string>();
    if (seenPaths.has(scopePath)) return;
    const result = await client.scopeFor(directory, scopePath);
    seenPaths.add(scopePath);
    activatedPaths.set(sessionID, seenPaths);
    const ctx = getSession(sessionID);
    for (const scope of result.scopes) {
      ctx.addActiveScope(scope);
    }
  }

  async function handleEvent(event: Event): Promise<void> {
    if (event.type === "session.created") {
      const sessionID = event.properties.info.id;
      try {
        await seedHardBootstrap(sessionID);
      } catch (error) {
        await failOpen("bootstrap.hard", error, { session_id: sessionID });
      }
      try {
        const soft = await client.bootstrapSoft(directory);
        getSession(sessionID).enqueuePendingEvent(renderSoftBootstrap(soft));
      } catch (error) {
        await failOpen("bootstrap.soft", error, { session_id: sessionID });
      }
    } else if (event.type === "session.compacted") {
      // "Possible amnesia event" (ARCHITECTURE §7.3): hard bootstrap is
      // always re-fetched and re-rendered from here on, never assumed to
      // have survived the compacted summary. Soft bootstrap is
      // deliberately NOT re-queued -- it's a session.created-only payload.
      try {
        await seedHardBootstrap(event.properties.sessionID);
      } catch (error) {
        await failOpen("bootstrap.hard", error, { session_id: event.properties.sessionID });
      }
    } else if (event.type === "session.deleted") {
      sessions.delete(event.properties.info.id);
      activatedPaths.delete(event.properties.info.id);
    }
  }

  return {
    event: async ({ event }) => {
      await logAcceptance("event", {
        event_type: event.type,
        session_id: event.type === "session.created"
          ? event.properties.info.id
          : "sessionID" in event.properties
            ? (event.properties as { sessionID: string }).sessionID
            : undefined,
        event_property_keys: Object.keys(event.properties),
        event_info_keys: "info" in event.properties && event.properties.info && typeof event.properties.info === "object"
          ? Object.keys(event.properties.info)
          : [],
        event_role: "info" in event.properties && event.properties.info && typeof event.properties.info === "object"
          ? (event.properties.info as { role?: unknown }).role
          : undefined,
        event_agent: "info" in event.properties && event.properties.info && typeof event.properties.info === "object"
          ? (event.properties.info as { agent?: unknown }).agent
          : undefined,
      });
      try {
        await handleEvent(event);
      } catch (error) {
        await failOpen("event", error, { event_type: event.type });
      }
    },

    "tool.execute.before": async (input, output) => {
      if (input.tool === "bash") return; // handled post-hoc in tool.execute.after
      try {
        const paths = extractPathsFromToolArgs(input.tool, output.args);
        await logAcceptance("tool.execute.before", {
          tool: input.tool, session_id: input.sessionID, tool_call_id: input.callID,
          arg_keys: output.args && typeof output.args === "object" ? Object.keys(output.args) : [],
          extracted_paths: paths, paths_are_absolute: paths.map((path) => /^(?:[A-Za-z]:[\\/]|\/)/.test(path)),
        });
        for (const path of paths) await activateScopesForPath(input.sessionID, path);
      } catch (error) {
        await failOpen("tool.execute.before", error, { tool: input.tool, session_id: input.sessionID });
      }
    },

    "tool.execute.after": async (input) => {
      if (input.tool !== "bash") return;
      // V1 deliberately never parses shell command semantics to predict
      // what a `bash` call will touch (ARCHITECTURE §6) -- instead this
      // detects what actually changed, after the fact, via `git status`.
      try {
        const paths = await client.changedFilesFromGitStatus(directory);
        await logAcceptance("tool.execute.after", {
          tool: input.tool, session_id: input.sessionID, tool_call_id: input.callID,
          extracted_paths: paths, paths_are_absolute: paths.map((path) => /^(?:[A-Za-z]:[\\/]|\/)/.test(path)),
        });
        for (const path of paths) await activateScopesForPath(input.sessionID, path);
      } catch (error) {
        await failOpen("tool.execute.after", error, { tool: input.tool, session_id: input.sessionID });
      }
    },

    "experimental.chat.system.transform": async (input, output) => {
      if (!input.sessionID) return;
      const ctx = sessions.get(input.sessionID);
      if (!ctx) return; // no session.created seen yet for this id -- nothing to inject
      try {
        if (!Array.isArray(output.system)) {
          throw new TypeError("host supplied a non-array output.system");
        }
        if (output.system.some((entry) => entry.includes(TITLE_GENERATION_SYSTEM_MARKER))) {
          await logAcceptance("experimental.chat.system.transform", {
            session_id: input.sessionID,
            title_generation: true,
            rune_block_present: output.system.some((entry) => entry.includes("<!-- rune-context:start -->")),
          });
          return;
        }
        const beforeLength = output.system.length;
        const context = options.acceptanceSentinel
          ? `${ctx.render()}\n\n[RUNE_HOST_ACCEPTANCE_TEST]\n${options.acceptanceSentinel}` : ctx.render();
        mergeRuneBlock(output.system, context);
        systemTransformCount += 1;
        await logAcceptance("experimental.chat.system.transform", {
        invocation: systemTransformCount,
        session_id: input.sessionID,
        title_generation: false,
        system_length_before: beforeLength,
        system_length_after: output.system.length,
        rune_block_present: output.system.some((entry) => entry.includes("<!-- rune-context:start -->")),
        model_keys: input.model && typeof input.model === "object" ? Object.keys(input.model) : [],
        model_id: input.model && typeof input.model === "object"
          ? (input.model as { id?: unknown }).id
          : undefined,
        });
      } catch (error) {
        await failOpen("experimental.chat.system.transform", error, { session_id: input.sessionID });
      }
    },

    tool: {
      decision_propose: tool({
        description:
          "Propose a new rune Decision. Sits pending until a human runs `rune proposal approve` -- " +
          "this tool never writes a Decision directly.",
        args: {
          record_id: tool.schema.string().describe("Stable id, e.g. 'use-postgres-for-primary-store'."),
          content: tool.schema.string(),
          rationale: tool.schema.string().optional(),
          scopes: tool.schema.array(tool.schema.string()).optional(),
          files: tool.schema.array(tool.schema.string()).optional(),
          symbols: tool.schema.array(tool.schema.string()).optional(),
          critical: tool.schema
            .boolean()
            .optional()
            .describe("Eligible for hard bootstrap -- reserve for genuinely load-bearing decisions."),
          source_document: tool.schema.string().optional(),
          source_section: tool.schema.string().optional(),
        },
        execute: async (args, context) => {
          const result = await decisionPropose(context.directory, args);
          return JSON.stringify(result);
        },
      }),

      constraint_propose: tool({
        description:
          "Propose a new rune Constraint (MUST/SHOULD/INFO rule). Sits pending until a human runs " +
          "`rune proposal approve` -- this tool never writes a Constraint directly.",
        args: {
          record_id: tool.schema.string(),
          content: tool.schema.string(),
          severity: tool.schema.enum(["MUST", "SHOULD", "INFO"]),
          persistence_mode: tool.schema.enum(["persistent", "scope_bound", "source_bound", "temporary"]),
          rationale: tool.schema.string().optional(),
          scopes: tool.schema.array(tool.schema.string()).optional(),
          files: tool.schema.array(tool.schema.string()).optional(),
          symbols: tool.schema.array(tool.schema.string()).optional(),
          expires_at: tool.schema.string().optional(),
          source_document: tool.schema.string().optional(),
          source_section: tool.schema.string().optional(),
          machine_check_hint: tool.schema.string().optional(),
        },
        execute: async (args, context) => {
          const result = await constraintPropose(context.directory, args);
          return JSON.stringify(result);
        },
      }),

      note_add: tool({
        description:
          "Add a rune Note (pitfall/observation/workaround/etc). Written immediately -- no approval gate.",
        args: {
          category: tool.schema.enum([
            "pitfall", "observation", "workaround", "implementation_detail",
            "known_issue", "temporary_context", "investigation_result",
          ]),
          content: tool.schema.string(),
          why_persist: tool.schema.string(),
          scopes: tool.schema.array(tool.schema.string()).optional(),
          files: tool.schema.array(tool.schema.string()).optional(),
          symbols: tool.schema.array(tool.schema.string()).optional(),
          importance: tool.schema.number().optional(),
          confidence: tool.schema.number().optional(),
          evidence: tool.schema.array(tool.schema.string()).optional(),
          expires_at: tool.schema.string().optional(),
        },
        execute: async (args, context) => {
          const result = await noteAdd(context.directory, args);
          return JSON.stringify(result);
        },
      }),
    },
  };
}

/** Converts a host tool path to Rune's repo-relative, POSIX CLI contract.
 * Paths outside the plugin directory are rejected rather than mapped to an
 * unrelated scope. */
export function pathForScopeLookup(directory: string, path: string): string | null {
  const absolutePath = isAbsolute(path) ? path : resolve(directory, path);
  const relativePath = relative(directory, absolutePath);
  if (
    relativePath.length === 0 ||
    relativePath === ".." ||
    relativePath.startsWith(`..${sep}`) ||
    isAbsolute(relativePath)
  ) {
    return null;
  }
  return relativePath.split(sep).join("/");
}

export async function isRuneProject(directory: string): Promise<boolean> {
  try {
    return (await stat(join(directory, ".rune"))).isDirectory();
  } catch {
    return false;
  }
}

/** Finds the enclosing Rune repository so a workspace opened in a subdirectory
 * matches the CLI's upward project discovery. */
export async function findRuneProjectDirectory(directory: string): Promise<string | null> {
  let current = resolve(directory);
  while (true) {
    if (await isRuneProject(current)) return current;
    const parent = dirname(current);
    if (parent === current) return null;
    current = parent;
  }
}

/** Normalizes the live host's workspace-directory object at the adapter boundary. */
export function pluginDirectoryPath(directory: unknown): string | null {
  if (typeof directory === "string") return directory;
  if (!directory || typeof directory !== "object") return null;
  for (const key of ["directory", "path", "root", "cwd"]) {
    const value = (directory as Record<string, unknown>)[key];
    if (typeof value === "string") return value;
  }
  return null;
}

/**
 * Degraded-mode fallback, NOT wired into the primary flow above (kept as
 * documented but unused -- confirmed with the user this stays a fallback,
 * not the V1 mechanism): if `experimental.chat.system.transform` turns
 * out to be unavailable or unreliable on a given OpenCode version, context
 * could instead be pushed as a silent synthetic message via
 * `client.session.prompt({ path: { id: sessionID }, body: { noReply:
 * true, parts: [...] } } )`. Left unverified against a live host (the
 * `noReply` semantics -- whether it truly produces no visible turn -- are
 * exactly the kind of thing that needs checking against a real OpenCode
 * instance before this is ever turned on).
 */
export async function injectViaPromptFallback(
  client: PluginInput["client"],
  sessionID: string,
  text: string,
): Promise<void> {
  await client.session.prompt({
    path: { id: sessionID },
    body: { noReply: true, parts: [{ type: "text", text }] },
  });
}

export const RunePlugin: Plugin = async (input: PluginInput): Promise<Hooks> => {
  const acceptance = process.env.RUNE_OPENCODE_ACCEPTANCE === "1";
  const logAcceptance = async (message: string, extra: Record<string, unknown>): Promise<void> => {
    if (!acceptance) return;
    await input.client.app.log({
      body: {
        service: "rune-opencode-acceptance",
        level: "info",
        message,
        extra,
      },
    });
  };

  const rawDirectory: unknown = input.directory;
  const inputDirectory = pluginDirectoryPath(rawDirectory);
  const directory = inputDirectory === null ? null : await findRuneProjectDirectory(inputDirectory);
  if (acceptance) {
    console.error("[RepoRune loaded]", {
      build: RUNE_PLUGIN_BUILD,
      module: import.meta.url,
      directoryType: typeof rawDirectory,
      directoryKeys: rawDirectory && typeof rawDirectory === "object"
        ? Object.keys(rawDirectory)
        : [],
      normalizedDirectory: directory,
    });
  }
  const enabled = directory !== null && await isRuneProject(directory);
  try {
    await logAcceptance("plugin.loaded", {
      enabled,
      directory: directory ?? "unrecognized",
      worktree: input.worktree,
      opencode_plugin_api: "classic-hooks",
      node: process.version,
      platform: process.platform,
      fallback_used: false,
    });
  } catch {
    // Plugin diagnostics must not prevent the plugin from loading.
  }
  if (!enabled || directory === null) return {};

  return createRuneHooks(directory, defaultRuneClient, {
    acceptanceLog: logAcceptance,
    warningLog: async (message, extra) => {
      await input.client.app.log({
        body: { service: "rune-opencode", level: "warn", message, extra },
      });
    },
    acceptanceSentinel: acceptance ? "RUNE_SENTINEL_7A91F" : undefined,
  });
};

export default RunePlugin;
