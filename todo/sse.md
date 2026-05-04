# SSE Streaming Implementation Plan

OpenAI-compatible `stream: true` for the `llm-search` Flask + gunicorn API at `/v1/chat/completions`.

This document is **grounded in the live OpenAI spec, the WHATWG SSE Living Standard, and
the canonical `openai-python` SDK source** (which is generated from the same OpenAPI
schema the docs render). The fetched-on dates seen on each source are listed under
"Spec sources fetched" at the bottom of every numbered section.

---

## 1. Spec snapshot

### 1a. ChatCompletionChunk schema

Per the canonical `openai-python` types and the developers.openai.com streaming-events
reference, every chunk emitted while `stream: true` is in effect is shaped like this:

```python
class ChatCompletionChunk:
    id: str                                   # same id across all chunks for one request
    object: Literal["chat.completion.chunk"]  # constant
    created: int                              # unix seconds, same value across all chunks
    model: str                                # model name
    choices: List[Choice]                     # see below; can be [] on the final usage chunk
    service_tier: Optional[Literal["auto","default","flex","scale","priority"]]
    system_fingerprint: Optional[str]         # deprecated but still emitted
    usage: Optional[CompletionUsage]          # only on final chunk when stream_options.include_usage is true
```

Verbatim doc text (developers.openai.com):

- `id` — "A unique identifier for the chat completion. Each chunk has the same ID."
- `object` — "The object type, which is always `chat.completion.chunk`."
- `created` — "The Unix timestamp (in seconds) of when the chat completion was created. Each chunk has the same timestamp."
- `model` — "The model to generate the completion."
- `choices` — "A list of chat completion choices. Can contain more than one elements if `n` is greater than 1. Can also be empty for the last chunk if you set `stream_options: {\"include_usage\": true}`."
- `service_tier` — "Specifies the processing type used for serving the request" (values `auto`, `default`, `flex`, `scale`, `priority`).
- `system_fingerprint` — "This fingerprint represents the backend configuration that the model runs with" (optional, deprecated).
- `usage` — "An optional field that will only be present when you set `stream_options: {\"include_usage\": true}`."

```python
class Choice:
    index: int                                                   # always 0 for n=1
    delta: ChoiceDelta                                           # see below
    finish_reason: Optional[Literal["stop","length","tool_calls",
                                    "content_filter","function_call"]]
    logprobs: Optional[ChoiceLogprobs]                           # we will emit null

class ChoiceDelta:
    role: Optional[Literal["developer","system","user","assistant","tool"]]
    content: Optional[str]
    tool_calls: Optional[List[ChoiceDeltaToolCall]]
    refusal: Optional[str]
    function_call: Optional[ChoiceDeltaFunctionCall]             # deprecated; we will not emit

class ChoiceDeltaToolCall:
    index: int                                                   # required
    id: Optional[str]                                            # only on the first tool_call delta
    type: Optional[Literal["function"]]                          # only on the first tool_call delta
    function: Optional[ChoiceDeltaToolCallFunction]              # name only on first delta; arguments accumulate
```

`finish_reason` enum (verbatim from streaming-events page):

> "This will be `stop` if the model hit a natural stop point or a provided stop sequence,
> `length` if the maximum number of tokens specified in the request was reached,
> `content_filter` if content was omitted due to a flag from our content filters,
> `tool_calls` if the model called a tool, or `function_call` (deprecated) if the model
> called a function."

The usage object on the final chunk (when `stream_options.include_usage=true`):

```python
class CompletionUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    prompt_tokens_details: Optional[PromptTokensDetails]
    completion_tokens_details: Optional[CompletionTokensDetails]
```

### 1b. SSE wire format

Verbatim raw byte stream from the developers.openai.com streaming-events example
(reproduced exactly as documented):

```
data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1694268190,"model":"gpt-4o-mini","system_fingerprint":"fp_44709d6fcb","choices":[{"index":0,"delta":{"role":"assistant","content":""},"logprobs":null,"finish_reason":null}]}

data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1694268190,"model":"gpt-4o-mini","system_fingerprint":"fp_44709d6fcb","choices":[{"index":0,"delta":{"content":"Hello"},"logprobs":null,"finish_reason":null}]}

data: {"id":"chatcmpl-123","object":"chat.completion.chunk","created":1694268190,"model":"gpt-4o-mini","system_fingerprint":"fp_44709d6fcb","choices":[{"index":0,"delta":{},"logprobs":null,"finish_reason":"stop"}]}

data: [DONE]

```

Wire-format facts (literal):

- Each event line is `data: {json}` followed by `\n\n` (LF LF — blank line separator). Note the **single space** after the colon, matching every official OpenAI example and the openai-python SDK parser.
- The terminator is the literal string `data: [DONE]\n\n`. `[DONE]` is **not** JSON; consumers must skip JSON-parsing on this exact payload.
- Required HTTP response headers: `Content-Type: text/event-stream`, `Cache-Control: no-cache`, `Connection: keep-alive`. We add `X-Accel-Buffering: no` as an nginx-specific hint to disable response buffering (defensive; harmless on other proxies).
- Comment lines (used for keep-alive, see section 7) start with the literal byte `:` and are followed by `\n\n`.

### 1c. 2024–2026 additions worth flagging

- **`stream_options.include_usage`** — added 2024-05-06 by OpenAI (community.openai.com announcement). Default is **false / unset**: when omitted, no usage chunk is emitted and clients receive `data: [DONE]` immediately after the final content chunk. When true, exactly one extra chunk is emitted before `[DONE]` with `choices: []` and a populated `usage` object.
- **`stream_options.include_obfuscation`** — present in the openai-python SDK (`chat_completion_stream_options_param.py`): "When true, stream obfuscation will be enabled. Stream obfuscation adds random characters to an `obfuscation` field on streaming delta events to normalize payload sizes." This protects against side-channel attacks that infer plaintext from chunk lengths. **We will accept and ignore it** in PR1 and never emit an `obfuscation` field — we are not a side-channel target.
- **`service_tier`** — values `auto`, `default`, `flex`, `scale`, `priority`. **Echo whatever the client sent** in the chunk's `service_tier` field if present in the request, otherwise omit. We do not implement tiered behavior.
- **`refusal` delta field** — the model may emit a refusal string instead of content. None of our four CLIs surface refusals as a separate signal; we will leave it `null` in PR1 and revisit if a provider starts emitting one.

**Spec sources fetched for section 1:**
- `https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events` — fetched 2026-05-02; no last-updated date visible on page but content is current.
- `https://raw.githubusercontent.com/openai/openai-python/main/src/openai/types/chat/chat_completion_chunk.py` — fetched 2026-05-02 (HEAD of `main`).
- `https://raw.githubusercontent.com/openai/openai-python/main/src/openai/types/chat/chat_completion_stream_options_param.py` — fetched 2026-05-02.
- `https://raw.githubusercontent.com/openai/openai-python/main/src/openai/types/completion_usage.py` — fetched 2026-05-02.
- `https://community.openai.com/t/usage-stats-now-available-when-using-streaming-with-the-chat-completions-api-or-completions-api/738156` — original post 2024-05-06, fetched 2026-05-02.

---

## 2. API contract changes

### 2a. New request fields accepted

Add to `validate_request_body` in `server.py`:

```python
stream: Optional[bool]                       # default: false (unset = false)
stream_options: Optional[dict]               # only meaningful when stream=true
  └── include_usage: Optional[bool]          # default: false
  └── include_obfuscation: Optional[bool]    # accepted, ignored
service_tier: Optional[str]                  # accepted, echoed in chunks, no behavior change
```

Verbatim from the openai-python SDK type files (the canonical schema):

- `stream` — "If set to true, the model response data will be streamed to the client as it is generated using server-sent events."
- `stream_options` — "Options for streaming response. Only set this when you set `stream: true`."
- `include_usage` — "If set, an additional chunk will be streamed before the `data: [DONE]` message. The `usage` field on this chunk shows the token usage statistics for the entire request."
- `service_tier` — "Specifies the processing type used for serving the request. If set to 'auto', the request uses Project settings. 'default' uses standard pricing. 'flex' or 'priority' use corresponding service tiers."

### 2b. Validation rules

| Field | Validation |
|---|---|
| `stream` | If present, must be `bool`. Anything else returns `400 invalid_request_error`. |
| `stream_options` | If present, must be `dict`. If `stream` is not `true`, ignore (do not error — matches OpenAI behaviour). |
| `stream_options.include_usage` | If present, must be `bool`. |
| `service_tier` | If present, must be one of `auto`, `default`, `flex`, `scale`, `priority`. We do nothing with it beyond echoing. |

### 2c. Fields explicitly rejected with 400 in PR1

- `n` greater than `1` while `stream` is `true` — supported by OpenAI but we serve `n=1` only. Return `invalid_request_error`: "n>1 is not supported with stream=true on this proxy".
- `tools` / `tool_choice` — already silently passed through to the providers' built-in web search; the providers ignore them. No change needed for streaming.

### 2d. Non-streaming behaviour preserved

When `stream` is absent, `null`, or `false`, the request follows the **existing**
`stream_response_with_heartbeat` path that yields whitespace heartbeats and then a
single JSON body — exactly as before commit 49c1c82. Zero behaviour change for
existing non-streaming clients.

**Spec sources fetched for section 2:**
- openai-python SDK `chat/completion_create_params.py` (HEAD on `main`) — fetched 2026-05-02.
- developers.openai.com streaming-events reference — fetched 2026-05-02.

---

## 3. Server-side changes (`server.py`)

### 3a. New handler dispatch

`handle_chat_completions` becomes a one-line if-branch:

```python
def handle_chat_completions():
    body = request.get_json(silent=True)
    body, error_response = validate_request_body(body)   # now also validates stream/stream_options
    if error_response is not None:
        return error_response
    parsed, parse_error = parse_model_field(body.get("model"))
    if parse_error:
        return make_error_response(parse_error, 400, "model")
    provider, model_name = parsed
    prompt = extract_prompt_from_messages(body["messages"])
    if not prompt:
        return make_error_response("messages must contain at least one user message with content", 400, "messages")
    timeout = body.get("timeout") or PROVIDER_DEFAULTS[provider]["timeout"]
    model_string = body.get("model")
    request_id = uuid.uuid4().hex[:12]
    logger.info("POST /v1/chat/completions request_id=%s model=%s stream=%s prompt=%s",
                request_id, model_string, bool(body.get("stream")), prompt[:80])

    if body.get("stream") is True:
        return Response(
            stream_chat_completion_sse(provider, model_name, prompt, body, model_string, request_id, timeout),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return Response(
        stream_response_with_heartbeat(provider, model_name, prompt, body, model_string, request_id, timeout),
        mimetype="application/json",
    )
```

### 3b. New generator function `stream_chat_completion_sse`

Sketch of the exact yields the generator produces, in order:

```python
HEARTBEAT_INTERVAL_SECONDS = 15  # already a module constant; reused

def stream_chat_completion_sse(provider, model_name, prompt, request_body, model_string, request_id, timeout):
    """Yield SSE-framed ChatCompletionChunk events while the provider CLI runs.

    Wire format: 'data: {json}\\n\\n' per chunk; SSE-comment heartbeat
    ': hb\\n\\n' every HEARTBEAT_INTERVAL_SECONDS while waiting for the worker;
    'data: [DONE]\\n\\n' as the final terminator.
    """
    started_at = time.time()
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created_unix = int(started_at)
    include_usage = bool((request_body.get("stream_options") or {}).get("include_usage"))

    base_envelope = lambda delta, finish_reason=None: {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created_unix,
        "model": model_string,
        "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish_reason}],
    }

    # 1. Role chunk — first thing the client sees once we have a real chunk to send.
    #    The role chunk is ALSO when we stop emitting heartbeats and start emitting
    #    real data, so we delay it until the worker thread reports "ok" or "err".
    _worker_thread, result_queue = run_provider_in_thread(provider, model_name, prompt, model_string, request_id, timeout)

    while True:
        try:
            kind, value = result_queue.get(timeout=HEARTBEAT_INTERVAL_SECONDS)
            break
        except queue.Empty:
            yield ": hb\n\n"  # SSE comment heartbeat. Spec: a line starting with ':' is ignored.

    if kind == "err":
        runtime_error = sanitize_exception_for_log(value)
        logger.error("Chat completion failed (request_id=%s): %s", request_id, runtime_error)
        yield f"data: {json.dumps(_error_chunk(request_id))}\n\n"
        yield "data: [DONE]\n\n"
        _write_log_safe(provider, model_name, prompt, request_body, None,
                        time.time() - started_at, runtime_error, OUTPUT_DIR, started_at, request_id)
        return

    response_body = value  # full build_chat_completion_response payload

    # 2. Role chunk — opens the assistant message.
    yield f"data: {json.dumps(base_envelope({'role': 'assistant'}))}\n\n"

    # 3. Tool-call chunks — synthesized from the response body's url_citation web_search_call
    #    item. One pair per call: an opener delta with id/type/function.name, then a single
    #    arguments-fragment delta with the full JSON args. Both deltas have index=0 in the
    #    tool_calls array (per-call; we only emit one tool_call). See section 5 for per-provider
    #    mapping.
    for tool_call_chunk in _yield_tool_call_chunks_from_response(response_body, base_envelope):
        yield tool_call_chunk

    # 4. Content chunks — the assistant text. PR1 strategy is post-hoc chunking
    #    (option A in section 4): slice into ~50-token-equivalent windows and emit
    #    each as a content delta.
    for content_chunk in _yield_content_chunks_from_response(response_body, base_envelope):
        yield content_chunk

    # 5. Final delta with finish_reason — empty delta, finish_reason set.
    finish_reason = "tool_calls" if _had_tool_calls(response_body) else "stop"
    yield f"data: {json.dumps(base_envelope({}, finish_reason=finish_reason))}\n\n"

    # 6. Usage chunk — only if stream_options.include_usage is true.
    if include_usage:
        usage_chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created_unix,
            "model": model_string,
            "choices": [],
            "usage": response_body.get("usage") or {
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            },
        }
        yield f"data: {json.dumps(usage_chunk)}\n\n"

    # 7. Terminator. Literal, not JSON.
    yield "data: [DONE]\n\n"

    latency_seconds = time.time() - started_at
    logger.info("Streaming completed in %.1fs (request_id=%s provider=%s model=%s)",
                latency_seconds, request_id, provider, model_name)
    _write_log_safe(provider, model_name, prompt, request_body, response_body,
                    latency_seconds, None, OUTPUT_DIR, started_at, request_id)
```

### 3c. Error chunk shape

Per OpenAI's mid-stream error handling pattern, an error becomes a single chunk with
an `error` top-level field, immediately followed by `data: [DONE]`:

```json
{"error": {"message": "internal error (request_id=abc123)", "type": "api_error", "param": null, "code": null}}
```

Wire form: `data: {error_json}\n\n` then `data: [DONE]\n\n`.

**`type` selection matters** (folded in from Round 8 audit, finding #3):

| `error.type` | OpenAI SDK retry behaviour | Use for |
|---|---|---|
| `invalid_request_error` | non-retryable (caller did something wrong) | request validation failures (bad model, missing messages, malformed JSON) |
| `api_error` | retryable | provider CLI failures, upstream 5xx surfacing through the CLI, network drops, stream truncation |
| `server_error` | retryable | internal logic errors (uncaught exceptions in our own code) |

Today both `make_error_response` and the streaming error chunk emit
`invalid_request_error` for everything, which makes transient upstream failures
look permanent to OpenAI SDK clients. PR1 must split `make_error_response` to
accept an `error_type` parameter (default `invalid_request_error` for validation
paths) and pass `api_error` from the streaming error path.

This still matches the body shape `make_error_response` already produces for the
non-streaming path, so a client that handles non-streaming errors handles streaming
errors with no code change.

### 3d. Response headers

| Header | Value | Why |
|---|---|---|
| `Content-Type` | `text/event-stream` | Tells clients & proxies this is SSE; browsers' EventSource and openai-python's stream parser both require it. |
| `Cache-Control` | `no-cache` | Prevents intermediate caches from buffering; required by SSE spec. |
| `Connection` | `keep-alive` | Stops HTTP/1.1 from closing the socket between chunks. |
| `X-Accel-Buffering` | `no` | Defensive nginx-specific hint to disable response buffering. Harmless on other proxies. |

**Spec sources fetched for section 3:**
- developers.openai.com streaming-events reference — fetched 2026-05-02.
- WHATWG SSE Living Standard, sections 9.2.6 and 9.2.7 — fetched 2026-05-02 (Living Standard, last updated 2026-05-02 per page).

---

## 4. Provider-side changes — three options, ranked

### Option A — Best-effort post-hoc chunking (PR1, ship first)

- Keep `run_provider_in_thread` and `PROVIDER_RUNNERS[provider]` exactly as they are.
- After the worker thread returns the full `build_chat_completion_response` body,
  split `model_response_text` into N synthetic chunks (target: ~80 chars per chunk
  or 1 chunk per sentence, whichever is smaller). Yield each as a `content` delta.
  Use a small `time.sleep(0.01)` between chunks **only when** the response is short
  enough that all chunks would otherwise hit the wire in the same TCP packet — we
  want clients to actually exercise their stream-parsing code path.
- TTFT is **not** improved (still gated on full CLI completion), but the wire
  format is OpenAI-compliant and any client that works with real OpenAI streaming
  works with us.
- Estimated work: 1 PR, ~150 lines.

### Option B — Real incremental streaming via `sh` `_iter=True`

- Replace each provider's `sh.cli(...)` blocking call with `sh.cli(..., _iter=True)`
  which yields each stdout line as the CLI emits it. (See the `python-pip-sh` skill
  reference: `_iter=True` returns an iterator of strings.)
- Add `iter_stream_events(line_iterator)` to each provider that:
  1. yields incremental "tool_use opening" / "tool_use args" events
  2. yields incremental "text delta" events
  3. yields a final "stop" event
- The server-side generator consumes these events and translates each into an
  OpenAI ChatCompletionChunk delta on the fly.
- Real TTFT: tens of ms (CLI startup) instead of tens of seconds (full CLI run).
- Risk: rewriting parsers that took six audit rounds to harden. Each parser becomes
  stateful (must accumulate partial text-block content across `content_block_delta`
  events for claude, partial `parts[].text` for gemini, etc.) and must produce
  identical aggregate output to the post-mortem parser when fully drained — i.e.
  the existing `extract_model_response`/`extract_search_queries` functions become
  the **golden test oracle** for the streaming parser.
- Estimated work: 1 PR per provider (4 PRs), ~300 lines each.

### Option C — Hybrid (A globally + B for one provider)

- Ship A in PR1, then upgrade **claude** to B in PR2 (claude has the cleanest
  stream-json shape: `assistant` events with `content_block_start`/`content_block_delta`/
  `content_block_stop` for both `text` and `tool_use` blocks — close to a 1:1
  mapping with OpenAI ChatCompletionChunk deltas).
- Decide per-provider after measuring A's perceived latency in production. If A is
  acceptable for the other three, never do B for them.

### Recommendation: **Option C**.

Rationale:
1. **Ships SSE on day 1.** Clients that need OpenAI-compatible streaming (LangChain,
   Vercel AI SDK, custom EventSource consumers) work immediately, even if the wire
   stays bursty.
2. **Real TTFT only where it matters.** Claude is by far the most common provider
   in this proxy and the easiest to incrementalize. Codex, gemini, kimi keep their
   battle-tested post-mortem parsers.
3. **Failure isolation.** If the incremental claude parser regresses, fall back to
   the non-streaming code path with a single config flag — the post-mortem parser
   is still the source of truth.

**Spec sources fetched for section 4:**
- openai-python SDK `chat_completion_chunk.py` — fetched 2026-05-02.
- (No external spec sources; this section is design.)

---

## 5. Per-provider streaming hooks (option B sketch)

For each provider, this section spells out the incremental-event protocol that the
streaming parser yields, and how each event maps to an OpenAI ChatCompletionChunk
delta. PR2 implements these one provider at a time.

### 5a. Claude — `claude.py`

Claude's stream-json (with `--output-format stream-json --verbose`) emits these
event types we care about, in approximate order:

| Claude event | OpenAI delta yield |
|---|---|
| `{"type":"system","subtype":"init",...}` | (ignored — internal CLI init) |
| `{"type":"assistant","message":{"content":[{"type":"text","text":""}],...}}` (start) | `{"choices":[{"delta":{"role":"assistant"},...}]}` once at the very start of the stream. |
| `{"type":"assistant","message":{"content":[{"type":"text","text":"<chunk>"}],...}}` (delta) | `{"choices":[{"delta":{"content":"<chunk>"},...}]}` |
| `{"type":"assistant","message":{"content":[{"type":"tool_use","id":"toolu_xxx","name":"WebSearch","input":{}}],...}}` (start) | `{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"toolu_xxx","type":"function","function":{"name":"WebSearch","arguments":""}}]},...}]}` |
| `{"type":"assistant","message":{"content":[{"type":"tool_use","input":{"query":"..."}}],...}}` (delta) | `{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"<json fragment>"}}]},...}]}` (note: index only — id and type omitted on subsequent deltas, per the openai-python `ChoiceDeltaToolCall` spec) |
| `{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_xxx",...}]}}` | (ignored on the SSE wire — the tool result is consumed by claude itself, not surfaced to the OpenAI client. Annotations are folded into the content stream.) |
| `{"type":"result","is_error":false,...}` | Final chunk: `{"choices":[{"delta":{},"finish_reason":"tool_calls" if any tool_use seen else "stop"}]}` |
| `{"type":"result","is_error":true,"result":"<msg>"}` | Mid-stream error: emit error chunk + `[DONE]` per section 3c. |

State the parser must keep:
- whether the role chunk has already been sent (only emit once)
- the running `tool_calls` array index (always 0 for n=1; we only ever surface one tool_call)
- whether any `tool_use` event has been seen (decides `finish_reason`)

### 5b. Codex — `codex.py`

Codex emits JSONL with these types:

| Codex event | OpenAI delta yield |
|---|---|
| `{"type":"item.started","item":{"type":"agent_message","text":""}}` | `{"choices":[{"delta":{"role":"assistant"}}]}` (once) |
| `{"type":"item.delta","item":{"type":"agent_message","text":"<chunk>"}}` | `{"choices":[{"delta":{"content":"<chunk>"}}]}` |
| `{"type":"item.completed","item":{"type":"agent_message","text":"<final>"}}` | (no yield; we already emitted incrementally) |
| `{"type":"item.started","item":{"type":"web_search","action":{"type":"search","queries":[...]}}}` | tool_call opener: `{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"<item.id>","type":"function","function":{"name":"web_search","arguments":""}}]}}]}` |
| `{"type":"item.completed","item":{"type":"web_search","action":{"type":"search","queries":[...]}}}` | tool_call args: `{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\"queries\":[...]}"}}]}}]}` |
| `{"type":"turn.completed",...}` | Final chunk: `{"choices":[{"delta":{},"finish_reason":"tool_calls" if any web_search seen else "stop"}]}` |
| `{"type":"turn.failed","error":{"message":"..."}}` | error chunk + `[DONE]` |
| `{"type":"error","message":"..."}` | error chunk + `[DONE]` |

The RUST_LOG=trace SSE-event capture (used for native `web_search_call` annotations
in the non-streaming path) is **post-hoc only** — it's parsed from the trace file
after the CLI exits. PR2 keeps it post-hoc for codex (annotations only land in the
final aggregated content), so codex streaming under option B is text-only and the
url_citation annotations are absent from the streamed body. **Documented limitation.**

### 5c. Gemini — `gemini.py`

Gemini-cli stream-json emits these events:

| Gemini event | OpenAI delta yield |
|---|---|
| First `{"type":"message","role":"assistant","content":""}` | `{"choices":[{"delta":{"role":"assistant"}}]}` |
| `{"type":"content_delta","content":"<chunk>"}` (or `{"role":"assistant","content":"<chunk>"}`) | `{"choices":[{"delta":{"content":"<chunk>"}}]}` |
| `parts[].functionCall` with `name=="google_web_search"` | tool_call opener + args (queries from `args.query`) |
| `groundingMetadata` blocks | (no SSE yield — annotations require post-hoc redirect resolution which can take 30s; emit as part of the final aggregated body via the existing path, OR fold inline into the content stream after the model finishes if redirect resolution completes in the timeout window). PR2 default: text-only; citations in the final body only. |
| End-of-stream sentinel (no events for >100ms after CLI exit) | Final chunk + `[DONE]` |

Gemini is the most complex because grounding metadata is interleaved with text
parts and Vertex redirect resolution requires a network round-trip per URI. PR2
deliberately defers the streaming-grounding work and only streams the text deltas;
annotations remain best-effort post-hoc.

### 5d. Kimi — `kimi.py`

Kimi's stream-json uses OpenAI-style `role`/`content`/`tool_calls` directly:

| Kimi event | OpenAI delta yield |
|---|---|
| `{"role":"assistant","content":[{"type":"text","text":""}]}` (first) | `{"choices":[{"delta":{"role":"assistant"}}]}` |
| `{"role":"assistant","content":[{"type":"text","text":"<chunk>"}]}` | `{"choices":[{"delta":{"content":"<chunk>"}}]}` |
| `{"role":"assistant","tool_calls":[{"id":"call_xxx","function":{"name":"SearchWeb","arguments":"{\"query\":\"...\"}"}}]}` | tool_call opener + args (in two deltas: opener with id/type/name, then args-only delta) |
| `{"role":"tool","tool_call_id":"call_xxx","content":[...]}` | (consumed internally; not surfaced to OpenAI client) |
| `{"role":"error","content":"..."}` or `{"type":"error",...}` | error chunk + `[DONE]` |
| Stream end (CLI exits clean) | Final chunk + `[DONE]` |

Kimi is the tightest fit to OpenAI's wire shape — its stream-json is already
OpenAI-style. PR2 for kimi is mostly identity translation.

**Spec sources fetched for section 5:**
- developers.openai.com streaming-events reference (chunk shape) — fetched 2026-05-02.
- openai-python `ChoiceDeltaToolCall` definition (id/type only on first delta) — fetched 2026-05-02.
- Provider docs: claude/codex/gemini/kimi event types are inferred from the
  existing post-mortem parsers in `src/llm_search/providers/*.py`, not external
  documentation.

---

## 6. Error handling

Per the OpenAI streaming-error pattern:

1. If the CLI fails **before** we have emitted the role chunk, the worker thread's
   exception arrives in the result queue. The handler emits exactly two SSE events:
   ```
   data: {"error": {"message": "internal error (request_id=…)", "type": "api_error", "param": null, "code": null}}

   data: [DONE]

   ```
   No role chunk, no content chunks, no finish-reason chunk. This matches the
   shape clients see for non-streaming errors and lets a client distinguish
   "request validation failed" (`invalid_request_error`, returned from validation
   path with status 400) from "request started, then upstream broke"
   (`api_error`, retryable per OpenAI SDK semantics — see section 3c).
2. If the CLI fails **after** the role chunk has been emitted (only possible in
   option B once we are emitting incremental text), we emit:
   ```
   data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

   data: {"error": {"message": "…", "type": "api_error", …}}

   data: [DONE]

   ```
   The empty-delta finish_reason chunk closes out the assistant message cleanly so
   a strict-parsing client doesn't see a dangling open turn; the error chunk
   immediately after surfaces the failure; `[DONE]` terminates.
3. Once `Connection: keep-alive` has flushed bytes onto the wire, the HTTP status
   is committed at 200 — same constraint that applied to the whitespace-heartbeat
   path before. The error is surfaced **in the body**, not via status code, and
   OpenAI-compatible clients (openai-python, langchain, vercel ai-sdk) all handle
   this correctly by checking `chunk.error` first.

The spec citation for "errors emit on the SSE channel, not via status, once the
stream has started" comes from the openai-python `Stream._iter_events` source
(grounded via the SDK type files) and the developers.openai.com streaming-events
page which lists `error` as a streaming event type.

**Spec sources fetched for section 6:**
- developers.openai.com streaming-events reference — fetched 2026-05-02.
- openai-python SDK `chat_completion_chunk.py` (no `error` field on the chunk
  itself; errors are emitted as a separate top-level `{error: {…}}` JSON
  envelope) — fetched 2026-05-02.

---

## 7. Heartbeat strategy

The non-streaming path uses a whitespace heartbeat (`yield " "`) to keep the
upstream proxy (Cloudflare ~100s idle timeout) from sending HTTP 524. For SSE,
whitespace inside an event is illegal (would corrupt JSON parsing); the canonical
keep-alive is the SSE **comment line**.

### 7a. Spec citation for SSE comments

WHATWG HTML Living Standard, section 9.2.6 ("Interpreting an event stream"):

> "If the line starts with a U+003A COLON character (:) — Ignore the line."

WHATWG HTML Living Standard, section 9.2.7 ("Notes"):

> "Legacy proxy servers are known to, in certain cases, drop HTTP connections
> after a short timeout. To protect against such proxy servers, authors can
> include a comment line (one starting with a ':' character) every 15 seconds or so."

This is verbatim from the spec page (fetched 2026-05-02, last-updated 2026-05-02
per the Living Standard footer). The "every 15 seconds or so" interval matches
the existing `HEARTBEAT_INTERVAL_SECONDS = 15` constant in `server.py` — no
change needed.

### 7b. Implementation

```python
yield ": hb\n\n"
```

Emitted from the same `result_queue.get(timeout=HEARTBEAT_INTERVAL_SECONDS)`
loop that already produces whitespace heartbeats in `stream_response_with_heartbeat`.
The byte sequence is `0x3A 0x20 0x68 0x62 0x0A 0x0A` (`: hb\n\n`) — six bytes,
sub-MTU, guaranteed not to be coalesced into a JSON-bearing chunk by any sane
proxy.

### 7c. When to stop emitting heartbeats

Once the first `data:` chunk goes onto the wire (option A: when the worker
returns; option B: when the CLI emits its first text/tool_use event), the
regular chunk cadence keeps the connection alive on its own. Heartbeats are
**only** emitted while the result queue is empty.

In option B, if the CLI is generating tokens but they all fall in one
HEARTBEAT_INTERVAL_SECONDS window with a long pause after, the loop should
also emit heartbeats during the pause. Concretely: the streaming path uses
the same "queue.get with timeout" structure as the existing path, but the
queue carries incremental events instead of one final result.

### 7d. Disconnect handling and finally-block (Round 8 prerequisite)

The current `stream_response_with_heartbeat` (commit 49c1c82) has three gaps
that must be closed BEFORE — or AS PART OF — PR1, since SSE inherits the
exact same threading model:

1. **No `try/except GeneratorExit`.** Flask raises `GeneratorExit` on the
   generator the moment the client closes the socket. Today the generator
   unwinds silently; the worker thread keeps running the upstream CLI (paying
   for tokens), eventually finishes, and `result_queue.put(...)` lands in a
   queue with no reader. The CLI subprocess survives until its own
   `_timeout` fires.
2. **No `BrokenPipe` / `ConnectionReset` capture on `yield`.** A heartbeat
   write to a half-closed socket raises `BrokenPipeError` from inside the
   generator. Today this propagates out of Flask uncaught.
3. **`write_request_log` is not in `finally:`.** When the client disconnects,
   the request leaves zero trace in `LOGS_DIR` — the operator has no record
   that work happened.

The fix is a single rewrite of the generator (also covered in PR1 below):

```python
def stream_response_with_heartbeat(provider, model_name, prompt, request_body,
                                   model_string, request_id, timeout):
    started_at = time.time()
    deadline = started_at + timeout + HEARTBEAT_INTERVAL_SECONDS  # outer wall clock
    _worker, result_queue, cancel_event = run_provider_in_thread(...)
    response_body, runtime_error, client_disconnected = None, None, False

    try:
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                runtime_error = f"worker exceeded budget ({timeout}s)"
                yield json.dumps({"error": {"message": f"timeout (request_id={request_id})",
                                            "type": "api_error",
                                            "param": None, "code": "timeout"}})
                return
            try:
                kind, value = result_queue.get(timeout=min(HEARTBEAT_INTERVAL_SECONDS, remaining))
                break
            except queue.Empty:
                try:
                    yield "\n"   # newline > space: line-buffered intermediaries flush on \n
                except (BrokenPipeError, ConnectionResetError, OSError) as conn_err:
                    logger.warning("Heartbeat write failed (request_id=%s): %s",
                                   request_id, conn_err)
                    raise GeneratorExit() from conn_err
        # ... emit body or error ...
    except GeneratorExit:
        client_disconnected = True
        cancel_event.set()
        logger.warning("Client disconnected at %.1fs (request_id=%s)",
                       time.time() - started_at, request_id)
        raise
    finally:
        latency_seconds = time.time() - started_at
        log_status = "client_disconnected" if client_disconnected else runtime_error
        try:
            write_request_log(provider, model_name, prompt, request_body, response_body,
                              latency_seconds, log_status, OUTPUT_DIR, started_at, request_id)
        except Exception as log_error:
            logger.error("write_request_log failed (request_id=%s): %s", request_id, log_error)
```

`run_provider_in_thread` returns an additional `cancel_event` so the worker
can drop its result on the floor when the client disappeared. Killing the
actual subprocess on disconnect requires `_bg=True` plumbing through every
provider — out of PR1 scope; the cancel flag at least drops the result, and
the CLI's own `_timeout` bounds the leak (≤290s by default).

The same pattern applies VERBATIM to the SSE generator — only the yielded
bytes differ (`": hb\n\n"` for SSE comments, `"\n"` for whitespace). Land
this rewrite in PR0 (or as the first commit of PR1) so SSE inherits a
correct foundation.

**Spec sources fetched for section 7:**
- WHATWG HTML Living Standard sections 9.2.6 and 9.2.7 — fetched 2026-05-02.
- MDN "Using server-sent events" — fetched 2026-05-02 (last modified 2025-05-15).

---

## 8. Compatibility with the existing whitespace-heartbeat path

| Request | Existing path | New path |
|---|---|---|
| `stream` absent | `stream_response_with_heartbeat` | unchanged |
| `stream: false` | `stream_response_with_heartbeat` | unchanged |
| `stream: null` | `stream_response_with_heartbeat` | unchanged |
| `stream: true` | (was a passthrough that ignored the field) | **`stream_chat_completion_sse`** (new) |
| `stream: true, stream_options.include_usage: true` | n/a | `stream_chat_completion_sse` + extra usage chunk |

The branch is a **single `if body.get("stream") is True:`** at the top of the
streaming dispatch in `handle_chat_completions`. No existing callers see any
behavioural change. Validation rejects `stream` values that aren't bool with a
400 — which is also a behaviour clients of real OpenAI already handle.

**Spec sources fetched for section 8:**
- (No external spec sources; this section documents internal compatibility.)

---

## 9. Testing matrix

All curl invocations target `http://127.0.0.1:8041/v1/chat/completions` (the
docker-compose-mapped port). `KIMI_API_KEY` must be Doppler-injected per RULE_15.

### 9a. Streaming success — each provider

```bash
# Claude
curl -N -sS http://127.0.0.1:8041/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude/haiku","messages":[{"role":"user","content":"What is the population of Reykjavik in 2026?"}],"stream":true}'

# Codex
curl -N -sS http://127.0.0.1:8041/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"codex/gpt-5.5","messages":[{"role":"user","content":"What is the latest Python version?"}],"stream":true}'

# Gemini
curl -N -sS http://127.0.0.1:8041/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gemini/gemini-3-flash-preview","messages":[{"role":"user","content":"What movies came out this week?"}],"stream":true}'

# Kimi
curl -N -sS http://127.0.0.1:8041/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"kimi","messages":[{"role":"user","content":"Latest news from Beijing"}],"stream":true}'
```

`-N` disables curl's output buffering so we can see chunks as they arrive.

Expected event sequence for each:
1. zero or more `: hb` heartbeat lines while the CLI runs
2. one `data: {…delta:{role:"assistant"}…}` chunk
3. one or more `data: {…delta:{tool_calls:[…]}…}` chunks (if the model used web_search)
4. one or more `data: {…delta:{content:"…"}…}` chunks
5. one `data: {…delta:{},finish_reason:"stop"|"tool_calls"}…}` chunk
6. one `data: [DONE]` terminator

### 9b. Streaming with `stream_options.include_usage=true`

```bash
curl -N -sS http://127.0.0.1:8041/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude/haiku","messages":[{"role":"user","content":"Hi"}],"stream":true,"stream_options":{"include_usage":true}}'
```

Expected: same sequence as 9a, **plus** a penultimate chunk with `choices: []`
and `usage: {prompt_tokens:N, completion_tokens:M, total_tokens:N+M}` (PR1: all
zeros; PR4: real counts) **before** the `data: [DONE]` line.

### 9c. Streaming error path — bogus model

```bash
curl -N -sS http://127.0.0.1:8041/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude/this-model-does-not-exist","messages":[{"role":"user","content":"Hi"}],"stream":true}'
```

Expected:
1. zero or more `: hb` heartbeats during CLI startup
2. one `data: {"error": {"message":"internal error (request_id=…)","type":"invalid_request_error","param":null,"code":null}}` chunk
3. `data: [DONE]`

No role chunk, no content chunks, no finish_reason chunk.

### 9d. Streaming error path — invalid request body

```bash
curl -sS http://127.0.0.1:8041/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude/haiku","messages":[{"role":"user","content":"Hi"}],"stream":"yes"}'
```

Expected: HTTP 400 (validation rejects non-bool `stream` **before** the
streaming response starts), JSON body `{"error":{"message":"stream must be a boolean","type":"invalid_request_error","param":"stream","code":null}}`. Not SSE.

### 9e. Non-streaming path unchanged

```bash
curl -sS http://127.0.0.1:8041/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude/haiku","messages":[{"role":"user","content":"Hi"}]}'
```

Expected: `Content-Type: application/json`, optional whitespace prefix, single
JSON body matching `build_chat_completion_response` shape — **byte-identical to
pre-PR1 behaviour**.

### 9f. Streamed body == non-streamed body

For each provider, run the same prompt twice (once `stream:true`, once not),
join all `delta.content` fragments from the streamed run, and compare to the
non-streamed `choices[0].message.content`. Should match modulo whitespace
boundaries that were chosen by the post-hoc chunker (option A) — **bit-identical**
in option B because the underlying `extract_model_response` is the same code
path.

### 9g. Existing integration test

`scripts/integration_test.py` — extend it to add `stream:true` cases for each
provider and assert the SSE event sequence per 9a. Already runs in CI (per
the README the user mentioned at commit 49c1c82) so this catches regressions
before merge.

**Spec sources fetched for section 9:**
- developers.openai.com streaming-events reference — fetched 2026-05-02.
- (Curl examples are concrete invocations against this proxy; no external spec.)

---

## 10. Backwards compat / breaking changes

**None for non-streaming clients.**

- Adding `stream`, `stream_options`, `service_tier`, `include_usage`,
  `include_obfuscation` to the validator's allowlist is additive: clients
  that don't send them see no change.
- The `stream:true` branch is gated behind `body.get("stream") is True`; any
  non-true value (`null`, `false`, absent, `"yes"`-string-rejected-by-validator)
  takes the existing path.
- The non-streaming code path's whitespace-heartbeat behaviour from commit
  49c1c82 is preserved verbatim (untouched function `stream_response_with_heartbeat`).
- The OpenAI Chat Completions response shape from `build_chat_completion_response`
  is unchanged.

**One subtle point**: the per-request log file written by `write_request_log`
gets a new field `streaming_mode: "sse" | "single-json"`. Downstream log
analyzers must tolerate the new field but never break (additive only).

**Spec sources fetched for section 10:**
- (No external spec sources; internal compatibility.)

---

## 11. Risks and limitations

| Risk | Severity | Mitigation |
|---|---|---|
| Gunicorn sync workers block per request — already true. **4 workers cap concurrent streams at 4.** | High | Documented; concurrency is bounded the same way today. Switch to `gthread` worker class is a one-line change in the gunicorn invocation if streams pile up; a future PR. |
| Refactoring providers to incremental parsing in option B risks regressing the parsers that took six audit rounds to harden. | High | PR2 lands one provider at a time; the existing post-mortem parser remains as the test oracle (assert: streamed deltas, when concatenated, equal post-mortem output for the same fixture). Feature flag `LLM_SEARCH_STREAMING_MODE=incremental|posthoc` lets us roll back per-provider without code revert. |
| `sh` `_iter=True` in option B can deadlock if the parser blocks on a line that's still buffered in the OS pipe. | Medium | Set `_bufsize=1` (line-buffered) on the `sh.cli(...)` call. Adds CPU cost but eliminates the deadlock. The python-pip-sh skill reference confirms `_bufsize` is the right flag. |
| `data: [DONE]` is not JSON; clients that JSON-parse every `data:` payload crash. | Low | This is the OpenAI-standard wire format; any client that handles real OpenAI streaming handles ours. Document in the README. |
| SSE comment heartbeat (`: hb\n\n`) might be eaten by an aggressive proxy that strips lines starting with `:`. | Low | Switch to a `data: {"object":"heartbeat"}` chunk that real OpenAI clients will silently ignore (the chunk has neither `choices` nor `error`), if we ever see this in practice. Not a known issue with Cloudflare or nginx. |
| Token counting in PR1 is `usage: {0,0,0}` because no provider gives us reliable counts post-hoc. | Low | PR4 implements per-provider tokenizers (`tiktoken` for codex/claude, `sentencepiece` for gemini, `tiktoken` for kimi). Stage in last so the streaming wire format is locked in first. |
| `stream_options.include_obfuscation` is accepted but ignored — a client that depends on it for side-channel resistance gets none. | Low | Documented limitation. We are not a side-channel target (no per-token network observability path in the proxy). |
| PR2 (option B for claude) changes timing semantics: a client that relied on receiving the **whole** response at once at TTFT would see incremental tokens instead. | Low | This is the explicit goal of streaming; document in the changelog. |

**Spec sources fetched for section 11:**
- (No external spec sources; risk analysis.)

---

## 12. Implementation milestones

### PR0 — Streaming-path prerequisite hardening (Round 8 audit)

Scope: ~120 lines, one file (`server.py`). Lands BEFORE PR1.

The current whitespace-heartbeat path (commit 49c1c82) has three gaps that the
SSE work would inherit verbatim. Land the fix once, in the existing path; PR1
then reuses the same generator structure.

- **Wrap generator in `try/finally`** to guarantee `write_request_log` runs on
  every exit path (success, failure, disconnect, timeout).
- **Catch `GeneratorExit`** (raised by Flask on client disconnect) — set a
  `cancel_event` so the worker drops its result, log the disconnect with
  `request_id`, then re-raise so Flask cleans up.
- **Catch `BrokenPipeError` / `ConnectionResetError` / `OSError` on the
  `yield`** — convert to `GeneratorExit`.
- **Outer wall-clock deadline** = `timeout + HEARTBEAT_INTERVAL_SECONDS`.
  `sh._timeout` only bounds the CLI subprocess; non-CLI hangs (stuck redirect
  resolution, FS hang) yield whitespace forever today.
- **`run_provider_in_thread` returns `cancel_event`** alongside the result
  queue. Worker checks it after the CLI returns.
- **Switch heartbeat byte from `" "` to `"\n"`** — line-buffered
  intermediaries flush on `\n`. Both are valid JSON whitespace.
- **`make_error_response` accepts `error_type` parameter** (default
  `invalid_request_error` for validation paths). Streaming error path passes
  `api_error` so OpenAI SDK clients treat upstream failures as retryable.
- **Add `Cache-Control: no-cache, no-transform` and `X-Accel-Buffering: no`**
  to the existing whitespace-heartbeat Response headers. nginx and
  cloudflare-via-tunnel buffer responses by default; without these the
  heartbeat sits in a proxy buffer and the connection still idles to a 524.

Reference fix sketch in section 7d.

**Acceptance**:
- Existing `pytest scripts/integration_test.py` passes unchanged.
- New test: kill curl mid-heartbeat with `timeout 5 curl -sN ...`; the
  request log file is written within the `timeout + slack` window with
  `error: "client_disconnected"`.
- New test: confirm `Cache-Control` and `X-Accel-Buffering` headers in the
  response.

### PR1 — SSE wire format + option A + heartbeat (ship after PR0)

Scope: ~250 lines, one file (`server.py`), no provider changes.

- Validate `stream`, `stream_options`, `service_tier` in `validate_request_body`.
- New generator `stream_chat_completion_sse` (option A: post-hoc chunking).
- New helper `_yield_tool_call_chunks_from_response` (synthesizes one `tool_calls` delta from the response body's `web_search_call` item, if present).
- New helper `_yield_content_chunks_from_response` (slices content into ~80-char chunks, emits each as a `content` delta).
- New helper `_error_chunk` (builds the error envelope shape from section 3c — uses `type: "api_error"` for upstream failures, `type: "invalid_request_error"` only for validation paths).
- SSE comment heartbeat (`": hb\n\n"`) in the wait loop, identical structure to PR0's heartbeat.
- Updated headers (`text/event-stream`, `Cache-Control: no-cache`, `Connection: keep-alive`, `X-Accel-Buffering: no`).
- Integration tests in `scripts/integration_test.py` for each provider against curl examples in section 9.
- Reuses PR0's `try/finally` + `GeneratorExit` + `cancel_event` machinery.

**Acceptance**: section 9 tests all pass against a Doppler-injected docker-compose deployment.

### PR2 — Option B per provider, claude first

Scope: ~400 lines per provider PR, scoped to `src/llm_search/providers/<provider>.py`.

- Refactor each provider's `call_<provider>` to expose an iterator interface
  alongside the existing blocking interface (`run_search` stays as a façade for
  backwards compat with the standalone CLI entry points).
- Add `iter_stream_events(line_iterator)` that yields incremental events per
  the per-provider mapping in section 5.
- Server-side: extend `stream_chat_completion_sse` with an `if option_b_enabled:`
  branch that consumes events as they arrive rather than waiting for the worker.
- Feature flag: `LLM_SEARCH_STREAMING_MODE` env var (`posthoc` | `incremental`,
  default `posthoc` until each provider is hardened).
- Test oracle: streamed deltas concatenated MUST equal post-mortem
  `extract_model_response` output for a corpus of fixtures (10 prompts per
  provider, captured stream-json saved alongside the test).

Order of provider rollout: claude → kimi → codex → gemini (in increasing
complexity per section 5).

### PR3 — `stream_options.include_usage` and basic usage estimation

Scope: ~80 lines, `response.py` + `server.py`.

Round 8 finding #12: `build_chat_completion_response` in `response.py` today
hardcodes `usage: {prompt_tokens:0, completion_tokens:0, total_tokens:0}`.
Clients that compute cost or tokens-per-second see zeros and divide by zero.
Land a `len(text)//4` heuristic now (better than zero, signals "approximate")
and replace with real tokenizers in PR4.

- Replace the `0,0,0` literal with a `_estimate_token_count(text)` helper:
  `max(1, len(text)//4) if text else 0`.
- Apply to both the prompt (sum of all message contents) and the response.
- Emit `total_tokens = prompt_tokens + completion_tokens`.
- Read `stream_options.include_usage` from the validated body.
- Emit the extra usage chunk with `choices: []` and the populated `usage`
  object before `data: [DONE]` when `include_usage=true`.
- Wire `include_usage=false` (and absent) to skip the chunk.
- Add tests per section 9b.

PR3 can ship in parallel with PR1 — the only dependency is that PR1 lands the
basic streaming framing first. The usage-estimate change applies to BOTH the
non-streaming and streaming paths immediately (current `0,0,0` is wrong on
both).

### PR4 — Token counting

Scope: ~200 lines, new module `src/llm_search/tokenizers.py`.

- Per-provider tokenizer selection:
  - claude: `tiktoken` `cl100k_base` is wrong; claude uses its own tokenizer. Use the `anthropic-bedrock-tokenizer` shipped with the anthropic SDK, or fall back to a `len(text)/4` estimate with a clear "estimated" flag in the log.
  - codex: `tiktoken` `o200k_base` (gpt-4o family).
  - gemini: `sentencepiece` Vertex tokenizer if available, else `len(text)/4` estimate.
  - kimi: `tiktoken` `cl100k_base` (kimi follows OpenAI tokenizer compatibility per their docs).
- Replace the `usage: {0,0,0}` in `build_chat_completion_response` with real
  counts.
- Same shape applies to streaming usage chunks (section 9b).

**Spec sources fetched for section 12:**
- (No external spec sources; engineering plan.)

---

## 13. Round 8 audit — non-SSE improvement queue

Round 8 hh review (commit `49c1c82` baseline, 8/9 providers reporting, synthesis
at `tmp/2026-05-04-10-42-42_703b63d_o-new-session-minimax-minimax-m2-7-highspeed/synthesis.md`)
flagged 18 non-security findings. Items #1, #3, #4, #7, #10, #11, #12 are folded
into PR0 / PR1 / PR3 above. The remaining 11 are tracked here as discrete
improvements that do NOT block SSE; pick them up in any order between SSE PRs
or as housekeeping.

| # | sev | category | files | one-line fix | consensus |
|---|-----|----------|-------|--------------|-----------|
| R8-2 | high | correctness / error_handling | `claude.py:78-84`, `codex.py:112-118`, `gemini.py:369-377` | Extract shared `parse_jsonl_objects(text, source_label, skip_prefixes=())` helper that `try/except`s per-line `JSONDecodeError`; one bad line currently aborts the whole request with HTTP 500. `kimi_parsing.py:25-50` already does it correctly — converge on its pattern. | 6/8 |
| R8-5 | high | code_quality | `claude.py:192`, `codex.py:247`, `gemini.py:431`, `kimi_parsing.py:235` and the `seen_keys`-walrus dedupe block at the same offsets | `_ensure_failure_string` and the annotation dedupe-and-sort block are duplicated VERBATIM 4 times. RULE_09 #11 violation. Land both in `subprocess_safety.py` (or `providers/_failure.py`) and `response.py` respectively; each provider becomes a one-liner. | 7/8 |
| R8-6 | high | api_ergonomics / correctness | `server.py:144-160` `extract_prompt_from_messages` | Returns the LAST user message only — system prompt + prior turns silently dropped. Multi-turn callers receive incoherent answers with no signal. Either concatenate all turns with `role:` tags, or fail loud on `len(messages)>1` + any system message. | 4/8 |
| R8-8 | medium | correctness | `gemini.py:272-274` `build_annotations` | `model_text.find(segment_text)` always returns the FIRST occurrence — repeated grounded sentences map both citations to the same offset, second is silently lost. Prefer Gemini's API-provided `segment.startIndex`/`endIndex` when present; fall back to a moving cursor. | 3/8 |
| R8-9 | medium | correctness | `claude.py:214-216`, `kimi_parsing.py:286-288` | Symmetric substring URL match (`a in b or b in a`) lets `https://en.wikipedia.org` match a deep-page wiki article and re-point the citation. Require exact match or `link.startswith(source) AND len(source) > 24`. | 2/8 |
| R8-10 | medium | performance | `server.py:75-92, 132-135` | In `LLM_SEARCH_LOG_PROMPTS=0` (the production default), `read_provider_files` reads multi-MB JSONL bodies on every request and throws everything but the dict KEYS away. Split into `list_provider_files(provider, dir, rid)` that does N stat calls instead of N reads + parses. | 1/8 — solo, source confirms |
| R8-13 | medium | correctness | `scripts/sync_creds.py:156-160` | `shutil.copystat(host, container)` runs immediately before `os.replace(tmp, container)` — the copystat work is overwritten and discarded. Apply to `tmp_path` BEFORE the replace, or delete the block entirely (freshness check uses JWT `expiresAt`, not file mtime). | 2/8 |
| R8-14 | medium | correctness | `config.py:19-23` | `PROVIDER_DEFAULTS` is hardcoded above the env-driven `*_DEFAULT_MODEL` constants. Setting `CODEX_MODEL=foo` shifts `CODEX_DEFAULT_MODEL` everywhere except `PROVIDER_DEFAULTS`, which is what `parse_model_field("codex")` and `/providers` consult. Move the dict definition below the constants and reference them. | 1/8 — solo, source confirms |
| R8-15 | low | code_quality | `gemini.py:33-38` `PROMPTS_DIRECTORY` | Computed at module load, never referenced. Dead code. Delete. | 4/8 |
| R8-16 | low | performance | `prompts/__init__.py:8-12` `load_system_prompt` | Re-reads the markdown file on every request from 3 of 4 providers. Trivial `@functools.lru_cache(maxsize=1)` win. | 2/8 |
| R8-17 | low | observability | `codex.py:378-379, 385-386` | File written as `codex_raw_*.jsonl` extension but content is `json.dump(events, indent=2)` — pretty-printed JSON array, not JSONL. `jq -c '.[]'` produces garbage on operator inspection. Rename to `.json` or write as line-delimited. | 1/8 — solo, source confirms |
| R8-18 | low | performance | `docker/entrypoint.sh:44` | `dd bs=1 count=$size` for stale-secret scrub at startup is pathologically slow on multi-MB trace logs. Switch to `bs=4096 count=$(( (size+4095)/4096 ))`. | 1/8 — solo, source confirms |

**Round 8 deferred / dropped** (recorded for completeness, not actioned):

- claude--opus #1 — wrap error envelope as a ChatCompletion shape with
  `finish_reason: "error"` instead of bare `{"error":{...}}`. Solo,
  contestable; OpenAI emits the bare envelope on non-200 paths and the SDK
  parses both. Defer until a real client breaks.
- kimi--kimi `json.dumps(..., ensure_ascii=False)` — solo, valid JSON either
  way; cosmetic.
- goose Claude `extract_model_response` "skip events containing tool_use"
  — solo, plausible but risks dropping legitimate inline-tool answers
  without a real Claude trace fixture. Verify on a real trace before
  changing.
- pi `tiktoken`-based token counting — covered by PR4 above with a deeper
  per-provider plan; no need for a transitional `tiktoken` import.
- pi `requests` instead of `sh.curl` for redirect resolution — adds a
  runtime dep; current code is bounded by the parallel + overall-timeout
  pattern. Defer.
- claude--opus `write_request_log` 10 positional args → keyword-only — fair,
  bundle into the next refactor pass.
- `make_request_sandbox` / `discard_request_sandbox` 2x duplication — fair,
  bundle with R8-5's dedup pass.
- gemini `invoke_gemini_via_node` carries unused `script_path` — minor wart,
  delete with R8-15.
- log-level adjustments INFO→DEBUG — cosmetic.
- `parse_sse_body` silent failure on garbled body — low impact.
- kimi JWT base64 padding off-by-4 — Python 3.11+ tolerates extra padding;
  no behaviour change.
- `parse_model_field("codex/")` empty-after-slash rejection — one-line
  guard, bundle with PR0/PR1 validate pass.
- claude stderr pollutes worker logs — observability hygiene; lower
  priority than the cancel/disconnect work.

---

## Appendix — Spec sources fetched

All fetches occurred on **2026-05-02** (current date). No source page exposed a
human-readable "last updated" stamp except WHATWG (Living Standard, last updated
2026-05-02 per the spec footer) and MDN (last modified 2025-05-15).

| URL | Status | Notes |
|---|---|---|
| `https://platform.openai.com/docs/api-reference/chat/streaming` | 403 | Cloudflare-blocked the WebFetch user-agent. |
| `https://platform.openai.com/docs/api-reference/chat/create` | 403 | Same. |
| `https://platform.openai.com/docs/guides/streaming-responses` | 403 | Same. |
| `https://platform.openai.com/docs/api-reference/chat-streaming/chunk-object` | 403 | Same. |
| `https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events` | 200 | **Primary source for the chunk schema in section 1a.** |
| `https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create` | 200 (truncated mid-page) | The page rendered enough parameter docs but cut off before `stream`/`stream_options`. Mitigated via openai-python SDK fetch (canonical schema). |
| `https://developers.openai.com/api/docs/guides/streaming-responses` | 200 (high-level only) | Did not include literal SSE byte format. Mitigated via the streaming-events reference + openai-python SDK. |
| `https://developers.openai.com/cookbook/examples/how_to_stream_completions` | 200 | **Marked "archived" with original date 2022-09-02**; still useful for ChatCompletionChunk repr examples and `stream_options.include_usage` shape. |
| `https://raw.githubusercontent.com/openai/openai-python/main/src/openai/types/chat/chat_completion_chunk.py` | 200 | **Canonical schema for sections 1a and 5.** Generated by Stainless from OpenAI's OpenAPI spec. |
| `https://raw.githubusercontent.com/openai/openai-python/main/src/openai/types/chat/chat_completion_stream_options_param.py` | 200 | **Canonical schema for `include_usage` and `include_obfuscation`.** |
| `https://raw.githubusercontent.com/openai/openai-python/main/src/openai/types/chat/completion_create_params.py` | 200 | **Canonical schema for `stream`, `stream_options`, `service_tier` request fields.** |
| `https://raw.githubusercontent.com/openai/openai-python/main/src/openai/types/completion_usage.py` | 200 | **Canonical CompletionUsage shape.** |
| `https://html.spec.whatwg.org/multipage/server-sent-events.html` | 200 | **Canonical citation for SSE comment heartbeats** ("a comment line every 15 seconds or so", section 9.2.7). |
| `https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events` | 200 | Cross-reference for SSE comment semantics. |
| `https://community.openai.com/t/usage-stats-now-available-when-using-streaming-with-the-chat-completions-api-or-completions-api/738156` | 200 | **Original 2024-05-06 announcement of `stream_options.include_usage`**, including the literal final-chunk JSON shape (`choices: []`, `usage: {…}`). |

### Ambiguities / judgment calls flagged

1. **Default value of `stream_options.include_usage`** — the openai-python TypedDict
   declares the field as `total=False` optional with no explicit default. We
   document the default as `false` (per the official 2024-05-06 announcement which
   says users "must explicitly set" the flag) and reject ambiguous values in the
   validator.
2. **Whether to emit a finish_reason chunk before the error chunk on mid-stream
   failure** (section 6, case 2) — the OpenAI streaming-events page lists `error`
   as a streaming event type but does not show a literal example of "stream
   started cleanly then errored mid-content". We chose to emit a finish_reason
   chunk first to keep the assistant-turn structurally complete for strict
   parsers, then the error chunk, then `[DONE]`. Defensible because it's
   strictly additive — clients ignore unknown chunks.
3. **`X-Accel-Buffering: no` header** — not in the OpenAI spec. Added as a
   defensive nginx hint (matches every OpenAI-compatible proxy I've seen). No
   client cares about its presence; nginx eats it; harmless on every other
   proxy.
4. **Single space after `data:`** — every official OpenAI example shows
   `data: {…}` with one space; the WHATWG SSE spec allows zero or one and the
   parser strips it. We emit with one space to match the OpenAI cookbook
   convention exactly.
5. **`obfuscation` field on chunks** — the openai-python SDK documents it but
   no doc page shows what value to use. We will not emit it (we don't obfuscate),
   even when `stream_options.include_obfuscation=true`. Documented as a
   limitation in section 11.
6. **`refusal` deltas** — the openai-python SDK has the field but our four CLIs
   don't surface refusals as a separate signal. Left null; no behavior in PR1.
7. **`logprobs` on streaming chunks** — always `null` in our implementation.
   Real OpenAI emits `null` unless `logprobs: true` is requested in the request,
   which we don't support.
