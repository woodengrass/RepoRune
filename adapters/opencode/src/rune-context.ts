/**
 * Per-session Rune context state and its rendering into a single system
 * prompt block (confirmed with the user, see ARCHITECTURE.md §6 -- V1
 * injection mechanism).
 *
 * `tool.execute.before` cannot inject arbitrary text into a session (the
 * real `@opencode-ai/plugin` type only exposes mutable *tool arguments*
 * there, not a context channel); the only real system-level channel found
 * in the type definitions is the experimental `experimental.chat.system.
 * transform` hook, which runs before every LLM call and hands back a
 * mutable `system: string[]`. That means injection can't be a one-shot
 * "send this message once" event the way the original spike-era design
 * assumed -- instead this module holds durable per-session state (global
 * MUST constraints, active scopes' constraints/notes) that gets re-rendered
 * on *every* LLM call, plus a small queue of one-shot events (e.g. the
 * soft bootstrap project overview, which ARCHITECTURE §7.3 says should
 * only ever be injected once).
 *
 * This file has zero dependency on the OpenCode plugin API or the `rune`
 * CLI -- it is pure state + string rendering, kept that way specifically
 * so it can be unit-tested without mocking the whole Hooks/PluginInput
 * surface (see rune-context.test.ts).
 */
import type {
  HardBootstrapResult,
  ScopeForScope,
  SoftBootstrapResult,
} from "./rune-cli.js";

const RUNE_BLOCK_START = "<!-- rune-context:start -->";
const RUNE_BLOCK_END = "<!-- rune-context:end -->";

export class RuneSessionContext {
  private hard: HardBootstrapResult | null = null;
  private readonly activeScopes = new Map<string, ScopeForScope>();
  private readonly pendingEvents: string[] = [];

  /** Replaces the hard bootstrap set wholesale -- called on session.created
   * and again on every session.compacted (ARCHITECTURE §7.3: hard
   * bootstrap must be freshly re-sent every generation, never assumed to
   * have survived compaction). */
  setHardBootstrap(hard: HardBootstrapResult): void {
    this.hard = hard;
  }

  /** Adds a scope to the persistent, always-rendered set. Returns `false`
   * if the scope was already active (the caller can use this to decide
   * whether it's worth re-fetching scope-for data at all, though this
   * class itself doesn't care why a scope was skipped). Once a scope is
   * active it stays active (and rendered) for the rest of the session,
   * including across compaction -- unlike the original spike-era design's
   * one-shot `active_scope_ids` dedup set, persistent re-rendering means a
   * scope's constraints can never be silently dropped by compaction, since
   * they're re-sent on literally every LLM call, not just injected once. */
  addActiveScope(scope: ScopeForScope): boolean {
    if (this.activeScopes.has(scope.scope_id)) return false;
    this.activeScopes.set(scope.scope_id, scope);
    return true;
  }

  /** Queues one-shot text (e.g. the soft bootstrap project overview) --
   * included in the very next `render()` call, then dropped. */
  enqueuePendingEvent(text: string): void {
    this.pendingEvents.push(text);
  }

  /** Renders the full current Rune context block and drains pending
   * events. The persistent parts (hard bootstrap, active scopes) are
   * idempotent across repeated calls with no state change in between --
   * only the pending-events section differs between two consecutive calls
   * with nothing new queued (empty on the second). */
  render(): string {
    const sections: string[] = [];

    if (this.hard && (this.hard.constraints.length > 0 || this.hard.decisions.length > 0)) {
      const lines = ["## Global MUST constraints & critical decisions (rune, always in force)"];
      for (const c of this.hard.constraints) {
        lines.push(`- [MUST] ${c.record_id}: ${c.content}`);
      }
      for (const d of this.hard.decisions) {
        lines.push(`- [decision] ${d.record_id}: ${d.content}`);
      }
      if (this.hard.overflow) {
        lines.push(
          `- WARNING: hard bootstrap overflow (${this.hard.estimated_tokens} > ${this.hard.budget_tokens} ` +
          `token budget) -- the list above is still complete, nothing was dropped.`,
        );
      }
      sections.push(lines.join("\n"));
    }

    if (this.activeScopes.size > 0) {
      const lines = ["## Active scope context (rune)"];
      for (const scope of this.activeScopes.values()) {
        lines.push(`### ${scope.scope_id} (${scope.name})`);
        if (scope.summary) {
          lines.push(`Summary [${scope.summary_status}]: ${scope.summary}`);
        }
        for (const c of scope.constraints) {
          lines.push(`- [${c.severity}] ${c.record_id}: ${c.content}`);
          if (c.warning) lines.push(`  WARNING [${c.status}]: ${c.warning}`);
        }
        for (const n of scope.notes) {
          lines.push(`- note [${n.category}]: ${n.content}`);
        }
      }
      sections.push(lines.join("\n"));
    }

    if (this.pendingEvents.length > 0) {
      sections.push(["## Recent rune events", ...this.pendingEvents.map((e) => `- ${e}`)].join("\n"));
      this.pendingEvents.length = 0;
    }

    return sections.join("\n\n");
  }
}

/** Renders a soft bootstrap payload (ARCHITECTURE §7.3) as the one-shot
 * pending-event text queued on session.created -- never re-queued on
 * compaction (soft bootstrap is only ever injected once per session). */
export function renderSoftBootstrap(soft: SoftBootstrapResult): string {
  const lines = [
    `Project: ${soft.project_name ?? "(unknown)"}`,
    `Working tree: ${soft.working_tree_fresh ? "fresh" : "modified/unknown"} ` +
      `(${soft.files_indexed} files, ${soft.symbols_indexed} symbols indexed)`,
  ];
  for (const scope of soft.scopes) {
    lines.push(`- scope ${scope.scope_id} (${scope.name})${scope.summary ? `: ${scope.summary}` : ""}`);
  }
  for (const d of soft.decisions) {
    lines.push(`- decision ${d.record_id}: ${d.content}`);
  }
  return lines.join("\n");
}

/**
 * Merges `block` into `system` in place -- replaces an existing
 * rune-marked region if `render()` was already called once for this
 * system array (so re-rendering never grows the array or duplicates
 * content), otherwise appends to the last existing entry, or creates the
 * sole entry if `system` is empty. Deliberately never creates a second
 * array element once one already exists: confirmed with the user that some
 * OpenAI-compatible providers reject a request carrying more than one
 * system-role message, so every rune injection has to live inside a
 * single existing system entry, not as an additional one.
 */
export function mergeRuneBlock(system: string[], block: string): void {
  const hasExistingMarker = system.some((s) => s.includes(RUNE_BLOCK_START));
  if (block.length === 0 && !hasExistingMarker) {
    return;
  }
  const wrapped = `${RUNE_BLOCK_START}\n${block}\n${RUNE_BLOCK_END}`;
  for (let i = 0; i < system.length; i++) {
    const start = system[i].indexOf(RUNE_BLOCK_START);
    if (start === -1) continue;
    const endMarker = system[i].indexOf(RUNE_BLOCK_END, start);
    const end = endMarker === -1 ? system[i].length : endMarker + RUNE_BLOCK_END.length;
    system[i] = system[i].slice(0, start) + wrapped + system[i].slice(end);
    return;
  }
  if (system.length === 0) {
    system.push(wrapped);
  } else {
    system[system.length - 1] = `${system[system.length - 1]}\n\n${wrapped}`;
  }
}
