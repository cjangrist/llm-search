# Kimi migration: kimi-cli → kimi-code (TODO — not started)

**Status:** research complete, implementation not started.
**Detail:** see [`kimi_migration_findings.md`](kimi_migration_findings.md) (agent deep-research, with a captured stream-json sample + field-by-field parser comparison).

**Why:** `kimi-cli` is legacy; the host already migrated (`~/.kimi/.migrated-to-kimi-code`). `kimi-code` (v0.18.0+) is the successor. Install: `curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash`.

**Headline:** NOT a drop-in. CLI invocation must be rewritten; the stream-json parser needs only ~2 lines. One of those parser fixes is also a **latent bug today** (see §2).

---

## 1. CLI invocation — `providers/kimi.py` (rewrite `build_kimi_arguments` / `call_kimi`)
- **Remove** flags kimi-code dropped: `--print`, `--no-thinking`, `--verbose`, `-w`/`--work-dir`, `--config`, `--config-file`.
- **Use:** `-p`/`--prompt` (this *is* headless mode; it auto-approves tools) + `--output-format stream-json`.
- **Do NOT** pass `--yolo` or `--auto` together with `--prompt` — kimi-code errors: `Cannot combine --prompt with --yolo.`
- Replace `-w <dir>` with the subprocess CWD (`_cwd=`).

## 2. Parser — `providers/kimi_parsing.py` (~2 lines)
- `SEARCH_TOOL_NAMES`: `"SearchWeb"` → `"WebSearch"`.
  ⚠️ **Latent bug:** kimi-cli **1.47.0+** already renamed this. The pinned 1.46.0 still emits `SearchWeb` (works today), so the next kimi version bump will silently zero out citations even without the kimi-code migration. Worth fixing independently.
- `SUMMARY_FIELD_PATTERN`: `Summary:` → `Snippet:` (else every source's content comes back empty).
- `detect_provider_failure`: kimi-code prints plain-text `error: ...` on stdout with **exit 0** (no JSON error event), so the `role=="error"` branch is dead — failures only hit the empty-response fallback. Add a stdout `error:` check for a clearer message.

## 3. Auth / config
- No `--config-file`. Options: (a) write a per-request `config.toml` in a temp dir and point kimi-code at it via the `KIMI_CODE_HOME` env var (preserves the existing write-then-scrub secret model), or (b) bake the key into `~/.kimi-code/config.toml` once.
- Web search is enabled purely by the `[services.moonshot_search]` block being present (no flag) — host config already has it.

## 4. Install + creds plumbing
- **Dockerfile:** swap `uv tool install kimi-cli==1.46.0` → `curl … install.sh` (pin via `KIMI_VERSION`; installs binary `kimi` into `KIMI_INSTALL_DIR`).
- **Creds paths:** `~/.kimi` → `~/.kimi-code` — update `docker-compose.yml` mount, `sync_creds.py` `CREDENTIAL_PAIRS` + `TOKEN_FILES`, and `entrypoint.sh` dir pre-creation.
- **config.py:** review `KIMI_*` (model/output/sandbox) for the new home.

## 5. Test
- Host standalone (`python -m llm_search.providers.kimi …`) + container.
- Run the full provider suite; **assert citations are non-empty** (catches the SearchWeb→WebSearch fix).

## Open questions (unresolved in research — reproduction steps in findings doc)
- Does kimi-code read `KIMI_API_KEY` from env, or config-only?
- Exact `FetchURL` tool-event content shape (no fetch occurred in the test run).
- Whether thinking blocks surface as separate stream-json events.
