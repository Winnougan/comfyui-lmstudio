"""
ComfyUI custom nodes for using a local LM Studio server as the LLM backend,
as a drop-in replacement for the built-in CLIPLoader -> TextGenerate pair.

Requires LM Studio running with its local server enabled
(LM Studio -> Developer -> Start Server), with a vision-capable model
loaded if you want image captioning (e.g. a Gemma-3 / Qwen2-VL / LLaVA GGUF).

Install:
  Drop this folder into ComfyUI/custom_nodes/comfyui_lmstudio/
  pip install requests pillow   (both are already ComfyUI deps, usually no-op)
"""

import base64
import io
import time

import numpy as np
import requests
from PIL import Image

# Module-level session: TCP keep-alive / connection reuse across queue items.
_SESSION = requests.Session()
_SESSION.headers.update({"Accept": "application/json"})
# (Content-Type is set automatically by requests when using json=payload,
#  so it's intentionally not set globally here.)

_CONNECT_TIMEOUT = 3       # seconds to establish a TCP connection
_MODELS_READ_TIMEOUT = 5   # seconds to read the /models response
_MAX_RETRIES = 3           # attempts for the chat completion request
_RETRY_BASE_DELAY = 2      # seconds; doubles each retry (2, 4, 8...)


# ---------------------------------------------------------------------------
# Node 1: connection / "loader" node — replaces CLIPLoader
# ---------------------------------------------------------------------------
class LMStudioConnection:
    """
    Holds the connection info for a running LM Studio server.
    Output plugs into LMStudioGenerateText's `connection` input, the same way
    CLIPLoader's CLIP output plugs into TextGenerate's `clip` input.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "base_url": (
                    "STRING",
                    {"default": "http://localhost:1234/v1", "multiline": False},
                ),
                "model": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "tooltip": "Exact model id as shown by LM Studio "
                        "(GET /v1/models), e.g. "
                        "gemma-3-12b-it or google/gemma-3-12b. "
                        "Leave blank to use whatever model is currently "
                        "loaded in LM Studio.",
                    },
                ),
                "api_key": (
                    "STRING",
                    {
                        "default": "lm-studio",
                        "multiline": False,
                        "tooltip": "LM Studio ignores this, but the field "
                        "must be non-empty for OpenAI-style clients.",
                    },
                ),
            },
            "optional": {
                "timeout": ("INT", {"default": 120, "min": 1, "max": 3600}),
            },
        }

    RETURN_TYPES = ("LM_STUDIO_CONNECTION",)
    RETURN_NAMES = ("connection",)
    FUNCTION = "connect"
    CATEGORY = "LM Studio"

    def connect(self, base_url, model, api_key, timeout=120):
        base_url = base_url.rstrip("/")

        # Fail fast with a clear error instead of a cryptic connection
        # error later at generation time.
        try:
            resp = _SESSION.get(
                f"{base_url}/models",
                timeout=(_CONNECT_TIMEOUT, _MODELS_READ_TIMEOUT),
            )
            resp.raise_for_status()
            available = [m["id"] for m in resp.json().get("data", [])]
        except Exception as e:
            raise RuntimeError(
                f"Could not reach LM Studio server at {base_url}. "
                f"Make sure the local server is running "
                f"(LM Studio -> Developer -> Start Server). "
                f"Underlying error: {e}"
            )

        # Warn instead of raising: LM Studio's JIT loading can load an
        # unloaded model on demand, so "not currently loaded" is not fatal.
        if model and available and model not in available:
            print(
                f"[LM Studio] WARNING: model '{model}' is not currently "
                f"loaded. Loaded models: {available}. LM Studio will try to "
                "JIT-load it if enabled; otherwise the request will fail."
            )

        connection = {
            "base_url": base_url,
            "model": model or (available[0] if available else ""),
            "api_key": api_key or "lm-studio",
            "timeout": timeout,
        }
        return (connection,)


# ---------------------------------------------------------------------------
# Node 2: generation node — replaces TextGenerate
# ---------------------------------------------------------------------------
class LMStudioGenerateText:
    """
    Sends chat completion request(s) to LM Studio's OpenAI-compatible API.
    Supports an optional image input for vision models. If a batch of
    images is provided, one request is sent PER IMAGE in the batch and the
    resulting captions are joined with the configured separator — matching
    the behavior of one caption per image rather than only captioning the
    first frame of a batch.

    Images are downscaled to `max_image_size` and sent as JPEG by default:
    JPEG encodes ~5-10x faster than PNG and produces a far smaller payload,
    and the smaller resolution also reduces image token count (and thus
    prompt-processing time) server-side. Switch image_format to "png" for
    lossless transmission of text-heavy / pixel-exact images.
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "connection": ("LM_STUDIO_CONNECTION",),
                "prompt": (
                    "STRING",
                    {"default": "Describe this image.", "multiline": True},
                ),
                "max_tokens": ("INT", {"default": 500, "min": 1, "max": 8192}),
                "temperature": (
                    "FLOAT",
                    {"default": 0.7, "min": 0.0, "max": 2.0, "step": 0.01},
                ),
                "top_p": (
                    "FLOAT",
                    {"default": 0.95, "min": 0.0, "max": 1.0, "step": 0.01},
                ),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFF}),
            },
            "optional": {
                "image": ("IMAGE",),
                "system_prompt": (
                    "STRING",
                    {"default": "", "multiline": True},
                ),
                "max_image_size": (
                    "INT",
                    {
                        "default": 1024,
                        "min": 256,
                        "max": 4096,
                        "step": 64,
                        "tooltip": "Images are downscaled so their longest "
                        "side is at most this many pixels before sending. "
                        "Most VLMs resize internally anyway, so larger "
                        "values mostly cost latency, not quality.",
                    },
                ),
                "image_format": (
                    ["jpeg", "png"],
                    {
                        "default": "jpeg",
                        "tooltip": "jpeg: ~10x faster encode, ~10x smaller "
                        "payload. png: lossless, better for pixel-exact or "
                        "text-heavy images.",
                    },
                ),
                "batch_separator": (
                    "STRING",
                    {
                        "default": "\n\n",
                        "multiline": False,
                        "tooltip": "When `image` is a batch of more than "
                        "one image, each image gets its own request and "
                        "the resulting captions are joined with this "
                        "string.",
                    },
                ),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("generated_text",)
    FUNCTION = "generate"
    CATEGORY = "LM Studio"

    # -- helpers -------------------------------------------------------
    @staticmethod
    def _tensor_to_data_url(single_image_tensor, max_image_size, image_format):
        """Takes a single [H,W,C] float32 0-1 image tensor, downscales, and
        encodes it as a base64 data URL."""
        arr = single_image_tensor.cpu().numpy()
        arr = np.clip(np.rint(arr * 255.0), 0, 255).astype(np.uint8)
        img = Image.fromarray(arr)
        if img.mode != "RGB":
            img = img.convert("RGB")

        if max_image_size and max(img.size) > max_image_size:
            img.thumbnail(
                (max_image_size, max_image_size), Image.Resampling.LANCZOS
            )

        buf = io.BytesIO()
        if image_format == "png":
            img.save(buf, format="PNG")
            mime = "image/png"
        else:
            img.save(buf, format="JPEG", quality=87)
            mime = "image/jpeg"
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:{mime};base64,{b64}"

    @staticmethod
    def _post_with_retry(base_url, api_key, payload, timeout):
        """POST to /chat/completions with basic retry on transient network
        errors. Does NOT retry on HTTP error responses (4xx/5xx) from LM
        Studio itself, since those are almost always deterministic (bad
        payload, context overflow, model not found) and retrying won't help."""
        last_error = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = _SESSION.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=payload,
                    timeout=(_CONNECT_TIMEOUT, timeout),
                )
                return resp
            except requests.exceptions.RequestException as e:
                last_error = e
                if attempt < _MAX_RETRIES - 1:
                    delay = _RETRY_BASE_DELAY * (2 ** attempt)
                    print(
                        f"[LM Studio] Request failed (attempt "
                        f"{attempt + 1}/{_MAX_RETRIES}): {e}. "
                        f"Retrying in {delay}s..."
                    )
                    time.sleep(delay)
        raise RuntimeError(
            f"LM Studio request failed after {_MAX_RETRIES} attempts: "
            f"{last_error}"
        )

    def _generate_one(
        self,
        base_url,
        model,
        api_key,
        timeout,
        prompt,
        max_tokens,
        temperature,
        top_p,
        seed,
        system_prompt,
        image_tensor,
        max_image_size,
        image_format,
    ):
        """Runs a single chat completion request and returns the text."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        if image_tensor is not None:
            data_url = self._tensor_to_data_url(
                image_tensor, max_image_size, image_format
            )
            user_content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]
        else:
            user_content = prompt

        messages.append({"role": "user", "content": user_content})

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "seed": seed,  # always send — `if seed:` would silently drop seed=0
            "stream": False,
        }

        resp = self._post_with_retry(base_url, api_key, payload, timeout)

        # Surface LM Studio's own error message (context overflow, image too
        # large, model not loaded, ...) instead of a bare status code.
        if not resp.ok:
            raise RuntimeError(
                f"LM Studio returned HTTP {resp.status_code}: "
                f"{resp.text[:500]}"
            )

        data = resp.json()
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError):
            raise RuntimeError(f"Unexpected response from LM Studio: {data}")

        text = (message.get("content") or "").strip()

        # Reasoning models (e.g. Gemma "thinking" variants) can spend the
        # whole max_tokens budget on reasoning_content and leave `content`
        # empty, especially if finish_reason == "length". Fall back to
        # reasoning_content so you at least get something, but warn loudly
        # since it means max_tokens was too low for a clean answer.
        if not text:
            reasoning = (message.get("reasoning_content") or "").strip()
            if reasoning:
                print(
                    "[LM Studio] WARNING: 'content' was empty "
                    f"(finish_reason={choice.get('finish_reason')}). "
                    "The model likely ran out of tokens while reasoning "
                    "before writing its final answer. Falling back to "
                    "reasoning_content, but you should raise max_tokens "
                    "and/or disable thinking mode for clean output."
                )
                text = reasoning
            else:
                raise RuntimeError(
                    f"LM Studio returned no content and no reasoning_content. "
                    f"Full response: {data}"
                )

        return text

    # -- main ------------------------------------------------------------
    def generate(
        self,
        connection,
        prompt,
        max_tokens,
        temperature,
        top_p,
        seed,
        image=None,
        system_prompt="",
        max_image_size=1024,
        image_format="jpeg",
        batch_separator="\n\n",
    ):
        base_url = connection["base_url"]
        model = connection["model"]
        api_key = connection["api_key"]
        timeout = connection.get("timeout", 120)

        # No image: single text-only request.
        if image is None:
            text = self._generate_one(
                base_url, model, api_key, timeout,
                prompt, max_tokens, temperature, top_p, seed,
                system_prompt, None, max_image_size, image_format,
            )
            return (text,)

        # Image batch: one request PER image in the batch tensor [B,H,W,C].
        batch_size = image.shape[0]
        results = []
        for i in range(batch_size):
            print(f"[LM Studio] Captioning image {i + 1}/{batch_size}...")
            text = self._generate_one(
                base_url, model, api_key, timeout,
                prompt, max_tokens, temperature, top_p, seed,
                system_prompt, image[i], max_image_size, image_format,
            )
            results.append(text)

        return (batch_separator.join(results),)


NODE_CLASS_MAPPINGS = {
    "LMStudioConnection": LMStudioConnection,
    "LMStudioGenerateText": LMStudioGenerateText,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LMStudioConnection": "LM Studio Connection",
    "LMStudioGenerateText": "LM Studio Generate Text",
}
