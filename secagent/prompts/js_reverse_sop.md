# SOP — Web JS Reverse Engineering

> Methodology the agent follows when the user asks to reverse-engineer an
> encrypted parameter, a sign-of-request, an obfuscated bundle, etc. Loaded
> on top of the system prompt for engagements where this work is the focus.

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
- Sanity check: after deobfuscation, the file should be searchable by
  identifier name. If still all `_0xa1b2c3` — try a different mode.

### 5. Find the signing / encryption function
Heuristics (try in order, stop on first hit):

a. **Endpoint string search.**  Grep the deobfuscated bundle for the API
   endpoint path. Whatever function constructs that request is the entry
   point of the chain.
b. **Crypto-keyword search.**  Grep for `sign`, `signature`, `encrypt`,
   `hmac`, `aes`, `md5`, `sha`, `cipher`, `nonce`. False positives are
   common; rank by proximity to step (a)'s match.
c. **Algorithm fingerprints.**
   - AES: lookup tables `0x63, 0x7c, 0x77, 0x7b...` (sbox), or `aes_*` ids.
   - SHA-1: constants `0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476, 0xc3d2e1f0`.
   - SHA-256: `0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, ...` (K table).
   - HMAC: a function that does `xor 0x36` and `xor 0x5c` over the key.
   - Base64: the alphabet `ABCDEFGHIJ...456789+/` literal.
   - CRC32: polynomial `0xEDB88320`.
d. **Trace from the network call.**  If the bundle uses `fetch(...)` /
   `XMLHttpRequest`, find the wrapper, walk back through the call sites until
   you reach the function that sets the suspect header / body field.

### 6. Trace inputs to the function
- Read the function source carefully. Identify each input: which are constants,
  which come from request body, which from cookies/storage/URL/timestamps.
- If the path is unclear, hook live: `js_reverse__trace_function` (after
  approval) to log call args during a real navigation.

### 7. Verify in a sandbox
- Reconstruct a minimal JS snippet that calls the suspect function with
  KNOWN inputs (from your captured request).
- Run via `js_execute`. **Sandbox modes:**
  - **`auto` (default)** — picks docker if available, falls back to
    node-permission. Refuses if neither — does NOT silently downgrade to raw.
  - **`docker`** — hardened container, `--network none`, FS read-only,
    nobody user, cgroup limits. Use this if you don't trust the snippet
    or there's any chance of exfil. **Recommended.**
  - **`node-permission`** — Node 20+ permission model. FS confined, but
    network is NOT blocked. Acceptable for pure crypto math; risky if the
    snippet might `fetch()` somewhere.
  - **`raw`** — no isolation. Only use when you genuinely need network
    access (e.g. testing against a real in-scope endpoint) AND the
    snippet has been read+approved.
- Compare the output against the captured request's signed/encrypted value.
- If MATCH → you have the algorithm. Skip to phase 8.
- If MISMATCH:
   - Inputs differ? (timestamp drift, missing field, encoded vs raw)
   - Function uses a closure / module-private constant? (re-read step 5)
   - There's a wrapper layer (Base64-of-Hex-of-AES, etc.)? Peel one layer.

### 8. Document & port
- Add a `add_finding` (or `note_*.md` if not yet a finding):
  - Endpoint, function path inside the bundle (file:line),
  - Inputs (with examples), output format,
  - Algorithm name + key derivation,
  - One known-good test vector.
- Provide a Python (or whatever the user wants) port of the algorithm.
  Include a self-test that asserts against the test vector.

---

## Decision rules

- **Keep snippets in `engagement_dir/`.**  Never load remote JS into
  `js_execute` — it must come from a file you already pulled in scope.
- **One hypothesis per `js_execute` call.**  If you think the algo is
  HMAC-SHA256(secret + ts), that's one call. Don't bundle three guesses.
- **Sandbox by default.** Don't pass `sandbox_mode='raw'` unless the
  snippet legitimately needs network access. If you do, say WHY in the
  approval prompt so the user can decide.
- **Stop and `ask_user` if** any of:
  - The bundle uses WASM and you can't statically read it.
  - You see anti-debugger / VM / CFG protection (jscrambler, etc.).
  - Reverse work would obviously violate the engagement scope.

## Token discipline

- Pulled JS files belong on disk — refer by `path` and `line` ranges, do not
  paste full bodies into chat.
- For `code_diff` between two bundle versions, prefer `mode: stats` first to
  decide if a full diff is worth pulling.
- `har_analyze` filter aggressively before reading rows back.

## Anti-patterns (do not do)

- Running `js_execute` without first reading the function you're calling.
- Probing endpoints out of scope just to "reproduce the request once."
- Brute-forcing encryption keys (forbidden_operations).
- Re-running the same failed reverse loop more than twice without
  summarizing and asking the user.
