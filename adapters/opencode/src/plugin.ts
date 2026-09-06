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

const defaultRuneClient: RuneClient = {
  scopeFor, bootstrapHard, bootstrapSoft, changedFilesFromGitStatus,
};

/**
 * Builds the `Hooks` object for a fixed `directory`/`worktree` -- split
 * out from the `Plugin` factory export below purely so tests can
 * construct one directly without going through OpenCode's own plugin
 * loading machinery.
 */
export function createRuneHooks(directory: string, client: RuneClient = defaultRuneClient): Hooks {
  const sessions = new Map<string, RuneSessionContext>();

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
    const result = await client.scopeFor(directory, path);
    const ctx = getSession(sessionID);
    for (const scope of result.scopes) {
      ctx.addActiveScope(scope);
    }
  }

  async function handleEvent(event: Event): Promise<void> {
    if (event.type === "session.created") {
      const sessionID = event.properties.info.id;
      await seedHardBootstrap(sessionID);
      const soft = await client.bootstrapSoft(directory);
      getSession(sessionID).enqueuePendingEvent(renderSoftBootstrap(soft));
    } else if (event.type === "session.compacted") {
      // "Possible amnesia event" (ARCHITECTURE §7.3): hard bootstrap is
      // always re-fetched and re-rendered from here on, never assumed to
      // have survived the compacted summary. Soft bootstrap is
      // deliberately NOT re-queued -- it's a session.created-only payload.
      await seedHardBootstrap(event.properties.sessionID);
    }
  }

  return {
    event: async ({ event }) => {
      await handleEvent(event);
    },

    "tool.execute.before": async (input, output) => {
      if (input.tool === "bash") return; // handled post-hoc in tool.execute.after
      for (const path of extractPathsFromToolArgs(input.tool, output.args)) {
        await activateScopesForPath(input.sessionID, path);
      }
    },

    "tool.execute.after": async (input) => {
      if (input.tool !== "bash") return;
      // V1 deliberately never parses shell command semantics to predict
      // what a `bash` call will touch (ARCHITECTURE §6) -- instead this
      // detects what actually changed, after the fact, via `git status`.
      for (const path of await client.changedFilesFromGitStatus(directory)) {
        await activateScopesForPath(input.sessionID, path);
      }
    },

    "experimental.chat.system.transform": async (input, output) => {
      if (!input.sessionID) return;
      const ctx = sessions.get(input.sessionID);
      if (!ctx) return; // no session.created seen yet for this id -- nothing to inject
      mergeRuneBlock(output.system, ctx.render());
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
          critical: tool.schema
            .boolean()
            .optional()
            .describe("Eligible for hard bootstrap -- reserve for genuinely load-bearing decisions."),
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
        },
        execute: async (args, context) => {
          const result = await noteAdd(context.directory, args);
          return JSON.stringify(result);
        },
      }),
    },
  };
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
  return createRuneHooks(input.directory);
};

export default RunePlugin;
