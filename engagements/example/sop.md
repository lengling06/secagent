# Engagement-specific SOP — copied from secagent/prompts/js_reverse_sop.md
#
# repl.py auto-loads <engagement>/sop.md and appends it to the system prompt.
# Replace this file (or delete it) per engagement to switch playbooks.

# SOP — Web JS Reverse Engineering

> Methodology the agent follows when the user asks to reverse-engineer an
> encrypted parameter, a sign-of-request, an obfuscated bundle, etc.

## The 8 phases (and which tool fits each)

### 1. Pin the target
Before anything: which **specific request** are we trying to reproduce?
- Get from the user: URL of the endpoint, the field name(s) that look encrypted/signed.
- If unclear, `ask_user` with a 2-line summary and stop.

### 2. Capture
- If the user already exported a HAR → `har_analyze` to filter by host / URL pattern.
- If not, drive a real browser via `playwright__*` to navigate + capture.
- Goal: ONE concrete request with full headers and body, and the response.

### 3. Locate the JS that builds it
- From the HAR: read which JS files were loaded for the page that triggered the request.
- `shell` to `curl` each JS file into `engagement_dir/js/<host>/<filename>.js`.
  Always pass `targets` so the scope check fires.
- If the bundle is sourcemap-protected: also pull the .js.map and use it to
  recover original module names.

### 4. Deobfuscate / pretty-print
- For webpack/vite output → `js_reverse__pretty_print` (or `webcrack` via shell).
- For obfuscator.io output → `js_reverse__deobfuscate` first, then pretty-print.
- For packer (Dean Edwards) → eval-unpack via `js_reverse__deobfuscate`.
- Save deobfuscated output as `<filename>.deob.js` next to the original.

### 5. Find the signing / encryption function
Heuristics (try in order, stop on first hit):

a. **Endpoint string search.** Grep the deobfuscated bundle for the API
   endpoint path.
b. **Crypto-keyword search.** Grep for `sign`, `signature`, `encrypt`,
   `hmac`, `aes`, `md5`, `sha`, `cipher`, `nonce`.
c. **Algorithm fingerprints.**
   - AES sbox `0x63, 0x7c, 0x77, 0x7b...`
   - SHA-1 constants `0x67452301, 0xefcdab89, 0x98badcfe, ...`
   - SHA-256 K table `0x428a2f98, 0x71374491, 0xb5c0fbcf, ...`
   - HMAC: function with `xor 0x36` and `xor 0x5c`
   - Base64 alphabet literal
   - CRC32 polynomial `0xEDB88320`
d. **Trace from the network call.** Find `fetch(...)` / `XMLHttpRequest`
   wrapper, walk back to the suspect-field setter.

### 6. Trace inputs to the function
- Read the function source carefully. Identify each input source.
- If unclear, hook live: `js_reverse__trace_function` (after approval).

### 7. Verify in a sandbox
- Reconstruct a minimal JS snippet that calls the suspect function with
  KNOWN inputs from the captured request.
- Run via `js_execute`. **Sandbox modes:**
  - `auto` (default) — picks docker > node-permission > error.
  - `docker` — `--network none`, read-only FS, nobody user. **Recommended.**
  - `node-permission` — FS confined, network NOT blocked. Crypto-math only.
  - `raw` — no isolation. Use only when you actually need network/fs.
- Compare against the captured value.
- If MISMATCH: check inputs, closures, wrapper layers. Peel one layer.

### 8. Document & port
- `add_finding` with: endpoint, function path (file:line), inputs, output
  format, algorithm, one test vector.
- Provide a Python port with a self-test against the test vector.

---

## Decision rules

- **Keep snippets in `engagement_dir/`.** Never load remote JS into
  `js_execute` — only files you already pulled in scope.
- **One hypothesis per `js_execute` call.**
- **Sandbox by default.** Never pass `sandbox_mode='raw'` without a
  written reason in the approval prompt.
- **Stop and `ask_user` if** WASM-only / anti-debugger / scope concern.

## Token discipline

- Pulled JS files belong on disk — refer by `path` and `line` ranges,
  not by pasting bodies.
- `code_diff` mode `stats` first; only request `unified` if stats says diff exists.
- `har_analyze` filter aggressively.

## Anti-patterns

- Running `js_execute` without first reading the function being called.
- Probing endpoints out of scope to "just reproduce the request once."
- Brute-forcing encryption keys (forbidden).
- Retrying a failed reverse loop more than twice without summarizing + asking.
