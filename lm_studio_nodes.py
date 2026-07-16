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
import json

import numpy as np
import requests
from PIL import Image


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
            resp = requests.get(f"{base_url}/models", timeout=5)
            resp.raise_for_status()
            available = [m["id"] for m in resp.json().get("data", [])]
        except Exception as e:
            raise RuntimeError(
                f"Could not reach LM Studio server at {base_url}. "
                f"Make sure the local server is running "
                f"(LM Studio -> Developer -> Start Server). "
                f"Underlying error: {e}"
            )

        if model and available and model not in available:
            raise RuntimeError(
                f"Model '{model}' is not currently loaded in LM Studio. "
                f"Loaded models: {available}. "
                f"Either load '{model}' in LM Studio, or leave the "
                f"'model' field blank to use whatever is loaded."
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
    Sends a chat completion request to LM Studio's OpenAI-compatible API.
    Supports an optional image input for vision models (sent as a base64
    data URL, same convention OpenAI's vision API uses).
    """

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "connection": ("LM_STUDIO_CONNECTION",),
                "prompt": (
                    "STRING",
                    {
                        "default": "Describe this image.",
                        "multiline": True,
                    },
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
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("generated_text",)
    FUNCTION = "generate"
    CATEGORY = "LM Studio"

    # -- helpers -------------------------------------------------------
    @staticmethod
    def _tensor_to_data_url(image_tensor):
        """ComfyUI IMAGE tensors are [B,H,W,C] float32 0-1. Take the first
        image in the batch and encode it as a base64 PNG data URL."""
        arr = image_tensor[0].cpu().numpy()
        arr = np.clip(arr * 255.0, 0, 255).astype(np.uint8)
        img = Image.fromarray(arr)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        return f"data:image/png;base64,{b64}"

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
    ):
        base_url = connection["base_url"]
        model = connection["model"]
        api_key = connection["api_key"]
        timeout = connection.get("timeout", 120)

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        if image is not None:
            data_url = self._tensor_to_data_url(image)
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
            "stream": False,
        }
        # LM Studio accepts a seed for reproducibility on many backends;
        # harmless to omit if unsupported.
        if seed:
            payload["seed"] = seed

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        try:
            resp = requests.post(
                f"{base_url}/chat/completions",
                headers=headers,
                data=json.dumps(payload),
                timeout=timeout,
            )
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"LM Studio request failed: {e}")

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

        return (text,)


NODE_CLASS_MAPPINGS = {
    "LMStudioConnection": LMStudioConnection,
    "LMStudioGenerateText": LMStudioGenerateText,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LMStudioConnection": "LM Studio Connection",
    "LMStudioGenerateText": "LM Studio Generate Text",
}
