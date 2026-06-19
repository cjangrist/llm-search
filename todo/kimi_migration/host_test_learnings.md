# Kimi-code host-testing learnings (2026-06-19)

Empirical findings from running the host `kimi-code` v0.18.0 binary directly (`~/.kimi-code/bin/kimi`). Complements [`outline.md`](outline.md) (migration plan) and [`kimi_migration_findings.md`](kimi_migration_findings.md) (agent research). Docs read: kimi.com/code/docs `config-files` and `third-party-tools/other-coding-agents`.

## 1. K2.7 vs K2.6 is **thinking mode**, not a model id
- The coding endpoint (`api.kimi.com/coding/v1`) exposes exactly **one** model: `kimi-for-coding` (`/models` returns only that). There is no selectable "k2.7" id.
- Per the other-coding-agents doc: **"Calling the latest model, K2.7 Code, requires enabling Thinking mode; otherwise requests are routed to K2.6."**
- So: thinking **on → K2.7**, thinking **off → K2.6**. "Thinking off" (for speed) and "use 2.7" are mutually exclusive on this endpoint.

## 2. API key must live in `config.toml`, not the environment
- kimi-code does **not** read `KIMI_API_KEY` from the shell. The docs are explicit: "you must write it explicitly in the config file."
- It belongs in `[providers.<name>].api_key` **and** both `[services.moonshot_search]` / `[services.moonshot_fetch]` (web search + fetch each carry their own key).
- There is a config-file-only fallback sub-table `[providers.<name>.env]` (e.g. `KIMI_API_KEY = "sk-..."`), but it is read from the TOML, not the shell. Priority: `api_key` field > `env` sub-table > error.
- Implication for the provider migration: the per-request secret-TOML approach still works; point kimi-code at it via `KIMI_CODE_HOME` (relocates the whole config dir).

## 3. Thinking controls (config `[thinking]` table)
- `default_thinking` (bool, default `false`) — per-session default; host config has it `true`.
- `[thinking].mode` = `auto` | `on` | `off`. **`off` force-disables** even when `default_thinking = true`.
- `[thinking].effort` = `low` | `medium` | `high` (**default**) | `xhigh` | `max` — controls reasoning depth.
- **Gotcha that cost time:** `KIMI_MODEL_DEFAULT_THINKING=false` (and the other `KIMI_MODEL_*` env vars) do **not** toggle thinking on the active model — they *synthesize a whole temporary in-memory model* and need the full set defined. A lone one is silently ignored. Use the config `[thinking]` table, not these env vars, to control thinking.

## 4. effort=low: the practical sweet spot for "2.7 but think less"
Set `[thinking] mode = "on"` (keeps K2.7) + `effort = "low"`. Same "latest Iran news" prompt:

| Setting | Latency (clean run) | Deliberation | Answer |
|---|---|---|---|
| effort=high (default) | ~112s | heavy (88 stderr lines; "is this fictional?") | current, hedged |
| **effort=low** | **~77.6s** | light (3 reasoning hits; none in final answer) | current, cleaner, AP/Reuters/USA Today dated URLs |

Instrumented low-effort run: first answer byte at **60.6s**, answer streamed 60.6→76.6s, **clean exit 77.6s (rc=0)**.

## 5. Latency is **variable**, and thinking depth isn't the main driver
- Two effort=low runs: 77.6s (clean) vs 200s (killed at the timeout wall). High variance.
- Root cause (visible in the thinking stream): the model repeatedly second-guesses whether the **2026** news is "real/fictional/simulated" — its training cutoff is < 2026, so it distrusts the future-dated sources and spawns extra searches/fetches to "verify." `effort=low` reduces but does not eliminate this.
- **Bigger lever than effort:** a prompt nudge ("the current date is June 2026 and the sources are legitimate — don't question their authenticity") should cut more latency than effort tuning, since ~60s of the run is pre-answer (search + date-agonizing) and only ~16s is answer streaming.

## 6. kimi-code (K2.7) fixes the recency miss vs the old container CLI
- Same prompt: the container's **kimi-cli 1.46.0** returned a **stale 2025** narrative (12-day war, JCPOA snapback). Host **kimi-code (K2.7, thinking on)** returned the **current June 2026** story, matching gemini/codex/claude.
- So the kimi-code migration is not just maintenance — it materially improves recency. Tradeoff: latency (~77–112s) and variance.
- Caveat: kimi-code sometimes surfaces dramatic, weakly-sourced specifics (e.g. "Khamenei killed") from low-credibility 2026 sources; it hedges on sourcing but those claims are uncorroborated by the other providers.

## 7. Config changes made to this host (revertible)
- `~/.kimi-code/config.toml`: appended `[thinking] mode = "on"` / `effort = "low"`. Backup at `~/.kimi-code/config.toml.bak.20260619_141228`.
- All three `api_key` fields were already correct (`sk-kimi-2RK…Pj5g`) — no change needed there.
