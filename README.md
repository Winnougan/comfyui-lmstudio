# ComfyUI LM Studio Nodes
<img width="1536" height="1024" alt="Winnougan&#39;s LM Nodes 01" src="https://github.com/user-attachments/assets/5feb8199-3bb9-44ae-b383-7a043b82d855" />

Use a local [LM Studio](https://lmstudio.ai/) server as the LLM/VLM backend inside ComfyUI — a drop-in replacement for the built-in `CLIPLoader` → `TextGenerate` pair, useful for local image captioning (e.g. building LoRA training datasets) or general text generation, with no API keys or cloud calls required.

## Nodes

### LM Studio Connection
Replaces `CLIPLoader`. Points at your running LM Studio server.

| Input | Description |
|---|---|
| `base_url` | LM Studio's OpenAI-compatible endpoint. Default: `http://localhost:1234/v1` |
| `model` | Exact model id as shown in LM Studio's Developer tab (e.g. `gemma-4-e4b-it-qat`). Leave blank to use whatever model is currently loaded. |
| `api_key` | Ignored by LM Studio, but must be non-empty. Default `lm-studio` is fine. |
| `timeout` | Request timeout in seconds (default 120). |

On connect, it pings `/v1/models` immediately so you get a clear error right away if the server isn't running or the model name doesn't match, instead of a failure mid-batch.

### LM Studio Generate Text
Replaces `TextGenerate`. Sends a chat completion request to LM Studio.

| Input | Description |
|---|---|
| `connection` | Output from `LM Studio Connection`. |
| `prompt` | Your instruction / captioning prompt. |
| `max_tokens` | **See "Minimum tokens" below — this is the setting most likely to bite you.** |
| `temperature`, `top_p` | Standard sampling controls. |
| `seed` | Set to `0` for no fixed seed, or a specific value for reproducibility. |
| `image` *(optional)* | Wire in an IMAGE output (e.g. from `LoadImage`) for vision captioning. Leave unplugged for a plain text assistant. |
| `system_prompt` *(optional)* | Persistent instruction/persona sent as the system message. |

Output `generated_text` is a plain STRING — wire it into `ShowText|pysssss`, `SaveText|pysssss`, or anywhere else a STRING input is expected.

## Setup

1. Drop this folder into `ComfyUI/custom_nodes/` (folder name doesn't matter, just don't nest it inside itself).
2. Restart ComfyUI. The two nodes appear under the **LM Studio** category.
3. In LM Studio: load your model, then go to **Developer → Start Server**. Confirm the port shown there matches `base_url` in the connection node (default `1234`).
4. In ComfyUI: add `LM Studio Connection` → `LM Studio Generate Text`, wire an image in if you want captioning, and connect `generated_text` downstream.

## Minimum tokens

**Set `max_tokens` to at least 2000, and 8000 if your model uses "thinking"/reasoning mode.**

Some models (reasoning-tuned Gemma, Qwen "thinking" variants, DeepSeek-R1 distills, etc.) write out a long internal reasoning trace into a separate `reasoning_content` field before producing the final answer in `content`. If `max_tokens` runs out mid-reasoning, LM Studio returns `finish_reason: "length"` with an **empty `content` field** — the node will fall back to using the raw reasoning text so you still get *something*, but it's messy chain-of-thought, not a clean caption, and you'll see a warning printed in the ComfyUI console when this happens.

Guidance by model type:
- **Non-reasoning models** (e.g. plain Gemma 3/4 IT, most vision-instruct models without a "thinking" toggle): `500–1000` tokens is typically enough for a one-to-two sentence caption.
- **Reasoning/thinking models**: `4000–8000` tokens. These models can burn several hundred to a few thousand tokens reasoning before writing a final answer, and this varies per image — set the ceiling generously rather than tuning it tightly.
- If you want speed over headroom, check whether your model exposes a way to disable thinking mode (a toggle in LM Studio's model settings, or a chat-template flag) — running without reasoning is both faster and keeps `max_tokens` low and predictable.

## Sample workflow

```
LoadImage ──────────────┐
                         ▼
LMStudioConnection ──▶ LMStudioGenerateText ──▶ generated_text ──┬──▶ SaveText|pysssss
                                                                  └──▶ ShowText|pysssss
```

- `LoadImage` → `image` input on `LMStudioGenerateText`
- `LMStudioConnection` → `connection` input on `LMStudioGenerateText`
- `LMStudioGenerateText.generated_text` → both `SaveText|pysssss` (writes captions to disk, e.g. `049.txt`) and `ShowText|pysssss` (preview in-graph) in parallel

## Notes & limitations

- Only the **first image in a batch tensor** is sent per call — if you need one API call per image in a batch (e.g. looping `ImageStackerDrop`'s full batch with per-image `.txt` output), that needs a small extension to the node; ask if you want that added.
- Every call sends the full image as a base64-encoded PNG in the request body — expect slower throughput than a cloud vision API, especially on a modest local GPU.
- The `model` you select must be vision-capable for image inputs to work; a text-only model will error or silently ignore the image.
