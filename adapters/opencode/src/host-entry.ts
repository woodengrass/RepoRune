// OpenCode treats every function exported by the configured entry module as a
// plugin factory. Keep the public host entry to one default export; plugin.ts
// also exports unit-test helpers that must not be registered as plugins.
import RunePlugin from "./plugin.js";

export default RunePlugin;
