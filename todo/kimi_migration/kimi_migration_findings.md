# Kimi provider migration analysis: `kimi-cli` (legacy) → `kimi-code` (new)

Research-only. No provider code changed. All secrets redacted.
Date: 2026-06-19. Host kimi-code = **v0.18.0** (`/home/cjangrist/.kimi-code/bin/kimi`). Repo pins `kimi-cli==1.46.0`; host still has `kimi-cli v1.47.0` installed via uv (binary symlink `~/.local/bin/kimi` is broken post-migration, but the package is intact and runnable via `uv tool run --from kimi-cli kimi`).

---

## TL;DR

**Not a drop-in.** The CLI invocation must be rewritten: kimi-code removes `--print`, `--no-thinking`, `--verbose`, `-w/--work-dir`, `--config`, and `--config-file` (every flag the provider passes except `-m`, `-p`, and `--output-format`), and it **rejects `--yolo`/`--auto` when combined with `--prompt`**. The good news: the **stream-json event schema is essentially identical** to what `kimi_parsing.py` already targets — same `role`/`tool_calls`/`function.name`/`tool_call_id`/`content` shapes — with only **two field/name mismatches** (`SearchWeb`→`WebSearch` tool name, and `Summary:`→`Snippet:` result field). Critically, the parser is **already broken against kimi-cli 1.46/1.47** for the same `SearchWeb` reason (proven below: 0 sources extracted), so the parser fix is needed regardless of the migration. The biggest behavioral change is auth: there is no `--config-file`, so the per-request secret-TOML approach must be replaced (write the api key into `~/.kimi-code/config.toml`'s `[providers.direct-api]` + `[services.moonshot_search]`/`[services.moonshot_fetch]`, or use `kimi login` device-code).

---

## 1. Binary / install inventory (evidence)

| Item | Legacy kimi-cli | New kimi-code |
|---|---|---|
| Install method | `uv tool install kimi-cli==1.46.0 --python 3.12` (Dockerfile:35-37) | `curl -fsSL https://code.kimi.com/kimi-code/install.sh \| bash` (CDN binary, **not** a pip/uv package) |
| Host binary | `~/.local/bin/kimi` → `~/.local/share/uv/tools/kimi-cli/bin/kimi` (**symlink target now missing** post-migration) | `~/.kimi-code/bin/kimi` (153 MB single packaged-JS binary) |
| Version flag | `--version` / `-V` | `--version` / `-V` (prints `0.18.0`) |
| Config dir | `~/.kimi/` | `~/.kimi-code/` (override via `KIMI_CODE_HOME` env var) |
| Migration marker | `~/.kimi/.migrated-to-kimi-code` present (migrated 2026-06-19, migrator v0.1.1) | — |

**install.sh details** (fetched from CDN; redirects `code.kimi.com`→`cdn.kimi.com`):
- Install dir: `KIMI_INSTALL_DIR="${KIMI_INSTALL_DIR:-$HOME/.kimi-code}"`, binary at `${KIMI_INSTALL_DIR}/bin/kimi`.
- Downloads a per-platform binary from `${KIMI_DOWNLOAD_BASE}/binaries/${version}/${filename}` plus a `manifest.json` (filename + SHA256).
- **Version pinning: `KIMI_VERSION` env var or `--version VERSION` flag.** Unset → fetches `${KIMI_DOWNLOAD_BASE}/latest`. (So Docker can pin like the `agy` installer does.)
- Modifies shell RC files to prepend `${KIMI_INSTALL_DIR}/bin` to PATH; skip with `KIMI_NO_MODIFY_PATH=1`.
- Requires `curl` or `wget`, and `shasum`/`sha256sum`.

The kimi-code `migrate` subcommand exists (`kimi migrate`) and already ran on the host. Migration report (`~/.kimi-code/migration-report.json`) dropped config keys `show_thinking_stream`, `notifications`, `mcp`, and flagged `kimi-code.json` OAuth login as "requiring relogin".

---

## 2. Flag mapping table

The provider builds argv in `kimi.py` `build_kimi_arguments()` (kimi.py:56-68):
```python
["--print", "--no-thinking", "--verbose", "--output-format", "stream-json", "-w", sandbox_dir, "-p", augmented_prompt]
# prepended: ["-m", model] if model; ["--config-file", config_file_path] if config_file_path
```

| kimi-cli flag (provider uses) | Purpose | kimi-code equivalent | Notes |
|---|---|---|---|
| `--print` | Force non-interactive print mode (auto-dismiss questions, auto-approve tools) | **REMOVED — implicit.** `-p/--prompt` IS the non-interactive mode. | kimi-code has no `--print`. Plain `-p` runs fully unattended and auto-approves tools (verified: `WebSearch` ran with no approval flag). **DELETE this flag.** |
| `--no-thinking` | Disable thinking to reduce noise/latency | **REMOVED — no flag.** Closest config: `default_thinking = false` in config.toml (host has `default_thinking = true`). No `--no-thinking` / `--thinking` CLI flag exists. | The migrator dropped `show_thinking_stream`. Thinking is now a config-only setting. **DELETE this flag.** Risk: thinking blocks may appear; but the parser already ignores non-`text` content parts and string-content with no tool_calls, and in the captured run the final assistant message was plain string content with no separate think event — so safe. |
| `--verbose` | Verbose CLI logging to stderr | **REMOVED.** No `--verbose`. (Closest: `kimi web --log-level`, irrelevant.) | **DELETE this flag.** |
| `--output-format stream-json` | JSONL event stream on stdout | **SAME:** `--output-format stream-json` (choices: `text`, `stream-json`). | KEEP unchanged. |
| `-w <sandbox_dir>` / `--work-dir` | Set CWD/workspace for the agent | **REMOVED — no `-w`/`--work-dir`/`--add-dir`.** | kimi-code uses the **process CWD**. **Replace `-w sandbox_dir` by launching the subprocess with `cwd=request_sandbox`** (sh's `_cwd=...`). Functionally equivalent. |
| `-p <prompt>` / `--prompt` | The user prompt (headless) | **SAME:** `-p` / `--prompt`. (Legacy alias `-c`/`--command` is gone; provider doesn't use it.) | KEEP unchanged. Still the secret-bearing arg for log redaction. |
| `-m <model>` / `--model` | Model alias override | **SAME:** `-m` / `--model`. | KEEP. Default model is empty in repo (`PROVIDER_DEFAULTS["kimi"]["model"] == ""`, config.py:23) → config default `direct/kimi-for-coding` used. |
| `--config-file <path>` | Load a per-request TOML holding the api key (mode 0600) | **REMOVED — no `--config-file` and no `--config`.** | **This is the big one.** No way to inject a per-request config file. See §3 for the replacement. The entire `write_kimi_config_file` / `discard_kimi_config_file` / `expected_kimi_config_path` machinery (kimi.py:71-133, 196-227) must be reworked or dropped. |
| *(not used)* `--quiet`, `--final-message-only`, `--afk`, `--input-format`, `--mcp-config*`, `--agent*`, `--max-*` | — | Mostly removed/renamed in kimi-code | Not relevant to current provider. |

**New / changed kimi-code-only flags worth knowing:** `-y/--yolo`, `--auto` (both **rejected with `-p`** — see below), `-S/--session`, `-C/--continue`, `--plan`, `--skills-dir`. Subcommands: `export`, `provider`, `acp`, `server`, `web`, `login`, `doctor`, `vis`, `migrate`, `upgrade`.

### Hard gotcha (verified empirically)
```
$ kimi -p "..." --output-format stream-json --yolo
error: Cannot combine --prompt with --yolo.        (exit 1, no JSON emitted)

$ kimi -p "..." --auto
error: Cannot combine --prompt with --auto.         (exit 0)
```
So **do NOT pass `--yolo` or `--auto` in `-p` mode.** `-p` alone already auto-approves (the WebSearch tool executed unattended and returned results). The example invocation in the task brief (`... --yolo`) fails; the working invocation is `-p "..." --output-format stream-json` with no approval flag.

---

## 3. Auth model differences

### How kimi-cli authed (current provider)
- Per request, `write_kimi_config_file()` renders `API_KEY_CONFIG_TEMPLATE` (kimi.py:33-53) — a full TOML with the api key embedded **3×** — to `<output_dir>/kimi_apiconfig_<request_id>.toml` at mode `0o600`, then passes it via `--config-file` (kimi.py:204, 67). After the call, `discard_kimi_config_file()` overwrite-then-unlinks it (kimi.py:106-133). The key never hits `/proc/<pid>/cmdline` because `--config-file` takes a path, not the key. The child env is also stripped of credential-shaped vars (`build_sanitized_environment`, kimi.py:212).
- The template targets the **coding** endpoints: `default_model="direct/kimi-for-coding"`, `[providers.direct-api]` `base_url="https://api.kimi.com/coding/v1"`, plus `[services.moonshot_search]` (`/coding/v1/search`) and `[services.moonshot_fetch]` (`/coding/v1/fetch`). **This matches the host config.toml almost exactly** (see below) — the embedded template is essentially a copy of `~/.kimi/config.toml`'s provider/services blocks.
- Empty api key → falls back to OAuth (no `--config-file`; kimi-cli reads `~/.kimi/config.toml` + `~/.kimi/credentials/kimi-code.json`).

### Legacy OAuth (`~/.kimi/credentials/kimi-code.json`)
Keys (values redacted): `access_token`, `refresh_token`, `expires_at`, `scope`, `token_type`, `expires_in`. This is the device-code token store. The migration report flagged it as needing **re-login** under kimi-code (kimi-code did NOT copy it; there is no `~/.kimi-code/credentials/` dir on the host).

### How kimi-code auths
- **No `--config-file`.** Config is read from `~/.kimi-code/config.toml` (or `$KIMI_CODE_HOME/config.toml`).
- Two paths:
  1. **API-key in config.toml** — `[providers.direct-api]` `api_key = "<redacted>"` plus `[services.moonshot_search]` / `[services.moonshot_fetch]` blocks (each with their own `api_key`). This is exactly what the host has now, and `kimi provider list` confirms it is the active auth: `direct-api  type=kimi  models=1  source=inline`. `kimi doctor config` passes.
  2. **Device-code login** — `kimi login` (or `kimi acp --login`), writes credentials. The migration left no kimi-code credentials dir, so on the host the api-key path is what's in use.
- **Web search is gated on the `[services.moonshot_search]` block.** From the binary: `webSearcher: searchService?.baseUrl === void 0 ? void 0 : new MoonshotWebSearchProvider(...)`. If `[services.moonshot_search].base_url` is unset, the WebSearch tool is **not registered**. Same for `moonshot_fetch` → FetchURL. **This is the web-search enablement mechanism** (see §5).

### Host `~/.kimi-code/config.toml` (secrets redacted)
```toml
default_model = "direct/kimi-for-coding"
default_thinking = true
default_plan_mode = false
merge_all_available_skills = false

[loop_control]
max_retries_per_step = 3
reserved_context_size = 50000

[background]
max_running_tasks = 4
keep_alive_on_exit = false

[services.moonshot_search]
base_url = "https://api.kimi.com/coding/v1/search"
api_key = "<redacted>"

[services.moonshot_fetch]
base_url = "https://api.kimi.com/coding/v1/fetch"
api_key = "<redacted>"

[providers.direct-api]
type = "kimi"
base_url = "https://api.kimi.com/coding/v1"
api_key = "<redacted>"

[models."direct/kimi-for-coding"]
provider = "direct-api"
model = "kimi-for-coding"
max_context_size = 262144
capabilities = [ "video_in", "thinking", "image_in" ]
display_name = "Kimi-k2.6"
```

**Comparison to the provider's embedded `API_KEY_CONFIG_TEMPLATE`** (kimi.py:33-53): identical `base_url`s, `type="kimi"`, `model="kimi-for-coding"`, `max_context_size=262144`, and the same three `api_key` injection points. Differences: the host config adds `default_thinking`, `[loop_control]`, `[background]`, richer `capabilities` (`video_in`/`image_in` vs the template's `["thinking"]`), and `display_name`. **The template is forward-compatible with kimi-code's config schema** — kimi-code accepts and `doctor`-validates it. The only structural difference vs the legacy `~/.kimi/config.toml`: the migrator dropped `show_thinking_stream`, `[notifications]`, and `[mcp.client]` (kimi-code rejects/ignores those keys; `tui.toml` now holds theme/editor/notifications instead).

### What the per-request secret-TOML approach maps to
Because there is no `--config-file`, the cleanest equivalents (pick one):

- **(A) Point kimi-code at a per-request config via `KIMI_CODE_HOME`.** Write a throwaway `<dir>/config.toml` containing the full template (api key in `[providers.direct-api]` + both `[services.*]`), set child env `KIMI_CODE_HOME=<dir>`, run, then scrub-delete `<dir>` (reusing the existing `discard_*` overwrite-then-unlink idiom). This **preserves the current per-request, key-out-of-argv, scrubbed-after security model** with minimal logic change — only the flag (`--config-file PATH`) becomes an env var (`KIMI_CODE_HOME=DIR`) and the file name becomes `config.toml`. **Recommended.**
- **(B) Bake the key into `~/.kimi-code/config.toml` once** (at container start via entrypoint, from `$KIMI_API_KEY`) and pass no per-request config. Simpler, but the key lives on disk for the container lifetime (acceptable in this single-tenant container; the host already does this) and loses the per-request scrub.
- **(C) `kimi login` device-code** — not headless-friendly for CI/container; requires interactive device approval. Not recommended for the server path.

Note: kimi-code reads the api key from config/env regardless; the env-sanitization in `build_sanitized_environment` should be revisited — with approach (A) the key is in the per-request `config.toml`, not env, so sanitization stays valid; with (B), if kimi-code can read `KIMI_API_KEY` from env you must NOT strip it. (Could not confirm whether kimi-code honors a `KIMI_API_KEY` env var; the host uses config.toml `api_key`. Treat env-key support as an open question — see §7.)

---

## 4. stream-json schema diff — THE CRITICAL SECTION

**Captured evidence:** real run on the host, saved to `tmp/kimi_code_streamjson_raw.txt` (4 JSON lines, 26 KB), via:
```
kimi -p "use web search: what is the price of bitcoin in USD right now? cite sources" --output-format stream-json
```
(Plain `-p`, no `--yolo`; the WebSearch tool executed unattended.)

### The four event types kimi-code emitted

1. **assistant + tool_calls** (the search call):
   ```json
   {"role":"assistant","tool_calls":[{"type":"function","id":"tool_2L3c...","function":{"name":"WebSearch","arguments":"{\"query\":\"Bitcoin price USD right now today\",\"limit\":5}"}}]}
   ```
2. **tool** (the search results):
   ```json
   {"role":"tool","tool_call_id":"tool_2L3c...","content":"Title: ...\nDate: 2026-06-19\nURL: http://...\nSnippet: ...\n\n---\n\nTitle: ...\n..."}
   ```
   `content` is a **string** (not a list), 23.9 KB, with **8 records** separated by `\n---\n`, each anchored by `Title:` / `Date:` / `URL:` / `Snippet:` (8× each).
3. **assistant + content** (final answer):
   ```json
   {"role":"assistant","content":"Bitcoin is trading around **$62,300–$62,900 USD** ... — https://..."}
   ```
   `content` is a **string** with no `tool_calls`.
4. **meta** (new, harmless):
   ```json
   {"role":"meta","type":"session.resume_hint","session_id":"session_...","command":"kimi -r session_...","content":"To resume this session: ..."}
   ```

### Field-by-field vs parser expectations

| Parser function (kimi_parsing.py) | Looks for | kimi-code emits | Status |
|---|---|---|---|
| `parse_stream_events` (l.25) | JSONL lines starting with `{`; logs `event["role"]` | One JSON object per line, all with `role` | ✅ Works. The trailing `meta` line is valid JSON and harmless. |
| `extract_search_queries` (l.53) | `role=="assistant"` → `tool_calls[].function.name` in `SEARCH_TOOL_NAMES={"SearchWeb"}`; reads `function.arguments` JSON → `query`/`q` | tool name is **`WebSearch`**; args = `{"query":..., "limit":5}` | ❌ **BREAKS.** `WebSearch` ∉ `{"SearchWeb"}` → 0 queries. Arguments shape (`.query`) is correct once the name matches. |
| `find_search_tool_call_ids` (l.84) | same `SEARCH_TOOL_NAMES`; collects `tool_call.id` | `id="tool_..."` present | ❌ **BREAKS** (same name mismatch) → no ids → no source matching. |
| `find_fetchurl_tool_call_urls` (l.97) | `FETCH_TOOL_NAMES={"FetchURL"}`; args `{"url":...}` | kimi-code fetch tool **is** `FetchURL`, args `["url"]` (from binary registry) | ✅ Name + arg correct. (No FetchURL call in this particular run, but the registry confirms the name/arg.) |
| `extract_search_sources` (l.176) | `role=="tool"` → match `tool_call_id` to search/fetch ids; `flatten_tool_content` then `parse_search_result_text` | `role=="tool"`, `tool_call_id` present, `content` is a string | ⚠️ Plumbing ✅; depends on the broken id set above → 0 matches today. |
| `parse_search_result_text` (l.146) | `^Title:`, `^URL:`, `^Date:`, `^Summary:` anchors; records bounded by `Title:` | `Title:`/`URL:`/`Date:` present, but result body field is **`Snippet:`** not `Summary:` | ❌ **BREAKS (content field).** Title/URL/Date parse fine; `SUMMARY_FIELD_PATTERN=^Summary:` matches nothing → every source's `content` is `""`. |
| `flatten_tool_content` (l.127) | list-of-parts OR string; strips `<system>...</system>` | string content; **no `<system>` block** in search results (0 found) | ✅ Works (string branch; the `<system>` strip is a no-op here, fine). |
| `extract_model_response` (l.247) | last `role=="assistant"` with string content (or list of `type=="text"` parts), **no `tool_calls`**, non-empty | final assistant event is string content, no tool_calls | ✅ **Works.** Verified: extracted 686 chars correctly. |
| `detect_provider_failure` (l.215) | `role=="error"` OR `type∈{"error","turn.failed"}`; else empty-response fallback | **kimi-code emits NO error JSON event.** Failures print plain text `error: ...` to stdout (e.g. bad model: `error: failed to run prompt: config.invalid: ...`) and **exit 0**. | ⚠️ **Partial.** No `role=="error"`/`type=="error"` event will ever appear → that branch is dead. The plain-text `error:` line is skipped by `parse_stream_events` (doesn't start with `{`), leaving 0 events → the empty-`model_response` fallback (l.230) DOES fire and raises. So failures are still caught, but **only via the empty-response heuristic**, and the surfaced message is the generic `"empty model response..."` rather than the real `error: ...` text. Worth adding an explicit scan of raw stdout for a leading `error:` line for better diagnostics. |
| `build_openai_format` (l.339) | builds `web_search_call` from queries + `message` with annotations | — | ⚠️ With today's code: queries empty → **no `web_search_call` item**; sources empty → message has **0 annotations**. |

### Empirical proof of breakage (real parser run on the captured file)
```
CURRENT code:
  SEARCH QUERIES extracted: []           # expected: ["Bitcoin price USD right now today"]
  SEARCH SOURCES extracted: 0            # expected: 8
  MODEL RESPONSE chars: 686              # OK
  FAILURE DETECTED: None                 # OK (model_response non-empty)
  OPENAI OUTPUT items: 1 -> ['message']  # missing 'web_search_call'; message has 0 annotations
```
```
After fix (SEARCH_TOOL_NAMES={"WebSearch"}; SUMMARY pattern also accepts 'Snippet:'):
  QUERIES: ['Bitcoin price USD right now today']
  SOURCES: 8   (url/title/date/content all populated)
  OPENAI items: ['web_search_call', 'message']   annotations: 5
```

### **KEY INSIGHT — the parser is already broken on kimi-cli today**
The legacy `kimi-cli v1.47.0` package strings show **`WebSearch` (300×)** and **`FetchURL`** — NOT `SearchWeb`. So `SEARCH_TOOL_NAMES={"SearchWeb"}` is **stale relative to the currently-pinned kimi-cli too** (`SearchWeb` was an even-earlier name). This means the `SearchWeb`→`WebSearch` rename is a **pre-existing bug fix**, not strictly a migration cost; it should be applied regardless. (Recommend re-testing the current container to confirm whether kimi-cli 1.46 in the image actually returns citations — it likely does NOT, given this.)

### Net: does `kimi_parsing.py` need changes? **Yes, two one-line changes:**
1. `SEARCH_TOOL_NAMES = {"SearchWeb"}` → `{"WebSearch"}` (or `{"WebSearch","SearchWeb"}` to be tolerant). **(l.14)**
2. `SUMMARY_FIELD_PATTERN` accept `Snippet:` as well as `Summary:`, e.g. `re.compile(r"^(?:Summary|Snippet):[ \t]*(.*)", re.MULTILINE | re.DOTALL)`. **(l.22)**

Everything else in the parser (event iteration, role keys, `tool_calls`/`function`/`arguments`/`id`, `tool_call_id`, string-vs-list content handling, annotation building) is **schema-compatible as-is**.

---

## 5. Web search enablement (how to make kimi-code actually search in `-p` mode)

- **No CLI flag turns search on.** Web search is enabled by the presence of `[services.moonshot_search]` (with `base_url` + `api_key`) in the active config.toml. From the kimi-code binary: `webSearcher: searchService?.baseUrl === void 0 ? void 0 : new MoonshotWebSearchProvider(...)` and `if (services.moonshotSearch !== void 0) out["moonshot_search"] = serviceToToml(...)`. URL fetching is enabled the same way via `[services.moonshot_fetch]` → `FetchURL` tool (`MoonshotFetchURLProvider`).
- The tool the model calls is **`WebSearch`** (params `["query"]`, optional `limit`); fetch tool is **`FetchURL`** (params `["url"]`). Confirmed from the binary registry (`name = "WebSearch"`, `name = "FetchURL"`, `WebSearch: ["query"]`, `FetchURL: ["url"]`).
- **The host config already has both `[services.*]` blocks**, so kimi-code searches out of the box (proven by the bitcoin run: 8 sources returned).
- **The model must be prompted to search.** The provider already augments the prompt with `CRITICAL RULE-> using web_search answer: "..."` (kimi.py:188). That worked here (the prompt literally said "use web search"). Consider updating that hint text to reference the actual tool name `WebSearch` if the model needs nudging, though it triggered fine as-is.
- Since `-p` auto-approves tools, no approval flag is needed (and `--yolo`/`--auto` are forbidden with `-p`).

---

## 6. Concrete migration plan

Ordered. Each item: change, risk, effort, and whether a host re-test is needed.

### A. `kimi_parsing.py` — 2 line changes (do this FIRST; fixes a pre-existing bug)
1. **Rename search tool.** `SEARCH_TOOL_NAMES = {"WebSearch"}` (or `{"WebSearch", "SearchWeb"}` for safety). *Effort: trivial. Risk: low. Re-test: yes — re-run the parser against `tmp/kimi_code_streamjson_raw.txt` (already proven to fix extraction).* (l.14)
2. **Accept `Snippet:` result field.** `SUMMARY_FIELD_PATTERN = re.compile(r"^(?:Summary|Snippet):[ \t]*(.*)", re.MULTILINE | re.DOTALL)`. *Effort: trivial. Risk: low. Re-test: yes (covered by the same parser run — 8 sources, content populated).* (l.22)
3. **(Optional, recommended) Better failure diagnostics.** Since kimi-code prints `error: ...` as plain stdout text + exit 0, add to `detect_provider_failure` (or to `kimi.py` before parsing) a scan of the raw stdout for a leading `error:` / `config.invalid:` line and surface it, instead of only the generic empty-response message. *Effort: small. Risk: low. Re-test: yes — force an error (e.g. `-m bogus`) and confirm the message propagates.*

### B. `kimi.py` — invocation rewrite
4. **Rewrite `build_kimi_arguments`** to drop `--print`, `--no-thinking`, `--verbose`, and `-w`. New argv: `["--output-format", "stream-json", "-p", augmented_prompt]`, prepend `["-m", model]` if model. **Do NOT add `--yolo`/`--auto`** (rejected with `-p`). *Effort: small. Risk: medium (behavioral). Re-test: yes — end-to-end search.* (kimi.py:56-68)
5. **Set subprocess CWD instead of `-w`.** Pass `_cwd=request_sandbox` to the `sh.kimi(...)` call (kimi.py:214-219) and remove the `-w sandbox_dir` arg. *Effort: small. Risk: low. Re-test: yes.*
6. **Replace the `--config-file` auth path** (choose approach A from §3): write the full template to `<dir>/config.toml`, set child env `KIMI_CODE_HOME=<dir>`, scrub-delete after. Concretely:
   - Repurpose `expected_kimi_config_path` → return a per-request **directory** (e.g. `<output_dir>/kimi_home_<request_id>/`) and the `config.toml` path inside it.
   - `write_kimi_config_file` writes the template to `<dir>/config.toml` (mode 0600 file, 0700 dir).
   - Add `KIMI_CODE_HOME=<dir>` to `sanitized_environment` before the call (note: this must be added AFTER `build_sanitized_environment`, since that strips credential-shaped vars — `KIMI_CODE_HOME` is a path, not a secret, but verify it isn't stripped).
   - `discard_kimi_config_file` scrubs `<dir>/config.toml` then removes the dir.
   - Drop the `--config-file` argv entirely.
   - *Effort: medium. Risk: medium-high (security + correctness of key plumbing). Re-test: yes — run WITH a `KIMI_API_KEY` set and confirm search works and the temp config.toml is scrubbed/removed afterward; also confirm `provider list`-style auth resolves to the injected key.*
   - **Alternative (simpler, approach B):** bake the key into `~/.kimi-code/config.toml` once via the entrypoint and delete the whole per-request config machinery (kimi.py:33-53, 71-133, 196-227). Lower code complexity, but loses per-request scrubbing. *Effort: medium (delete + entrypoint). Risk: low-medium.*
7. **Update the template / model id if needed.** `API_KEY_CONFIG_TEMPLATE` (kimi.py:33-53) already matches kimi-code's schema; consider syncing `capabilities`/`display_name` with the host (`video_in`/`image_in`, `Kimi-k2.6`) for parity, though not required for search. `KIMI_DEFAULT_MODEL` stays `""` → config default `direct/kimi-for-coding`. *Effort: trivial. Risk: low.*
8. **`redact_argv_for_logging` / `REDACT_VALUE_FLAGS`** (kimi.py:136, 162-174): `--config-file`/`--config` no longer appear in argv, so those entries become dead (harmless to keep). `-p`/`--prompt` redaction still needed. *Effort: trivial. Risk: none.*

### C. `config.py`
9. Add a `KIMI_CODE_HOME`-style config global if approach A is used (e.g. `KIMI_CONFIG_TEMPLATE_DIR` or reuse `KIMI_DEFAULT_OUTPUT_DIR`). The existing `KIMI_*` globals (model, output dir, sandbox dir) are unchanged. *Effort: trivial. Risk: none.*

### D. Dockerfile + entrypoint + compose
10. **Replace the kimi install** (Dockerfile:26-37): remove the `uv tool install "kimi-cli==${KIMI_CLI_VERSION}"` block and instead install kimi-code via its CDN script, pinned, e.g.:
    ```dockerfile
    ARG KIMI_CODE_VERSION=0.18.0
    RUN curl -fsSL https://code.kimi.com/kimi-code/install.sh | \
        env KIMI_INSTALL_DIR=/usr/local/kimi-code KIMI_VERSION="${KIMI_CODE_VERSION}" KIMI_NO_MODIFY_PATH=1 bash \
        && ln -s /usr/local/kimi-code/bin/kimi /usr/local/bin/kimi \
        && kimi --version
    ```
    (Mirror the `agy` installer pattern already at Dockerfile:42-43.) **uv may no longer be needed for kimi** — check whether anything else uses uv (currently only kimi-cli does; uv install at Dockerfile:27-37 could be removed if so). *Effort: medium. Risk: medium (need a container rebuild + smoke test; disk-prune first per project notes). Re-test: yes — rebuild and POST a real search to the kimi provider.*
11. **Entrypoint** (`docker/entrypoint.sh`): the `kimi --version` health check (l.70) still works. The creds copy currently targets `~/.kimi` and `~/.kimi/credentials` (l.11,18) — update to `~/.kimi-code` (and write/refresh `~/.kimi-code/config.toml` if approach B). The api-key-TOML scrub glob (l.36-41, `kimi_apiconfig_*.toml`) must be updated to the new per-request dir/config name (approach A) or removed (approach B). *Effort: small-medium. Risk: medium. Re-test: yes.*
12. **docker-compose.yml**: `KIMI_API_KEY` env passthrough (l.16) stays. The RO mount `~/.kimi:/mnt/creds/kimi:ro` (l.25) should become `~/.kimi-code:/mnt/creds/kimi-code:ro` (only needed if you mount host OAuth/config; if using `KIMI_API_KEY` + generated config, the mount can be dropped). *Effort: trivial. Risk: low.*

### E. Tests
13. **No dedicated kimi tests exist under `tests/`** (only the ad-hoc `tmp/test_all_providers.py`). After the parser changes, add a unit test that feeds the captured `tmp/kimi_code_streamjson_raw.txt` (or a trimmed fixture) through `parse_stream_events`→`build_openai_format` and asserts 1 query, ≥1 source, ≥1 annotation, and a `web_search_call` item. *Effort: small. Risk: none.*

**Suggested order:** A (parser fix, testable immediately against the captured file) → B/C (provider rewrite) → host smoke test (run `python -m llm_search.providers.kimi "..."` against host kimi-code) → D/E (Docker rebuild + container smoke test).

---

## 7. Open questions / unknowns

1. **Does kimi-code honor a `KIMI_API_KEY` (or `MOONSHOT_API_KEY`) env var** as an auth source, or only `config.toml` `api_key`? The host uses config.toml. If env keys ARE supported, approach B simplifies further and `build_sanitized_environment` must NOT strip it. *Not confirmed empirically.* (Mitigation: test `KIMI_CODE_HOME=<tmp> kimi -p ...` with key only in env vs only in config.)
2. **FetchURL output shape under kimi-code.** The bitcoin run made no FetchURL call, so the `role=="tool"` content shape for FetchURL (does it still wrap in `<system>...</system>`? string vs list-of-parts?) is **inferred from the binary registry only**, not captured. The legacy `flatten_tool_content` strips `<system>` and handles list-of-parts; likely still fine, but unverified. *Mitigation: run a prompt that forces a URL fetch (e.g. "fetch https://example.com and summarize") with `--output-format stream-json` and inspect the tool event.*
3. **Thinking-block events in stream-json.** With `default_thinking=true` and no `--no-thinking`, do separate "thinking"/"thought" events appear as their own JSON lines (with a distinct `role`/`type`)? In this run none did (final answer was a single string-content assistant event). If they appear with `role=="assistant"` and a non-`text` part type, the parser already filters them; if they use a new `role`/`type`, confirm they don't get mistaken for the final response. *Mitigation: run a reasoning-heavy prompt and inspect.* Optionally set `default_thinking=false` in the injected config to eliminate the question.
4. **Multi-turn / multiple WebSearch calls.** The captured run had a single search call. The parser handles multiple (it aggregates across all assistant events), and dedupes queries, so this is expected to work, but not exercised here.
5. **Does kimi-code 0.18.0's stream-json ever emit a structured error event** (e.g. for rate-limit/auth mid-stream, as opposed to the up-front `error:` plain-text line)? The changelog mentions "error enrichment" and an "outcome enum" added to the event schema in a recent version — there MAY be a `type`-bearing error/turn-failed event under some conditions that the existing `detect_provider_failure` `{"error","turn.failed"}` check would catch. Not reproduced. *Mitigation: induce a rate-limit or use an expired key and capture.*
6. **`uv` still needed in the image?** Only kimi-cli used it. Confirm no other Dockerfile consumer before removing the uv install layer.

---

## Artifacts saved to `tmp/`
- `tmp/kimi_code_streamjson_raw.txt` — raw kimi-code stream-json for the bitcoin query (the evidence for §4).
- `tmp/kimi_code_streamjson_stderr.txt` — stderr from that run (empty).
- `tmp/kimi_migration_findings.md` — this document.
