"""
finamt.agents.llm_backend
~~~~~~~~~~~~~~~~~~~~~~~~~~
Platform-aware local LLM inference — no Ollama required.

On Apple Silicon Macs (darwin / arm64): uses mlx-lm with 4-bit MLX models.
On all other platforms:                 uses transformers with HuggingFace models.

Models are downloaded automatically on first use and cached in
~/.cache/huggingface/hub.  No setup required.

Supported short-names (pass exactly these as model= in config):
  mistral:7b                      ← recommended default
  qwen2.5:7b-instruct-q4_K_M     ← alternative

Any other value is treated as a raw HuggingFace repo ID.
"""

from __future__ import annotations

import platform
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

_IS_APPLE_SILICON: bool = sys.platform == "darwin" and platform.machine() == "arm64"

# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------

# Maps config short-names → (mlx repo, hf repo)
_REGISTRY: dict[str, tuple[str, str]] = {
    "mistral:7b": (
        "mlx-community/Mistral-7B-Instruct-v0.3-4bit",
        "mistralai/Mistral-7B-Instruct-v0.3",
    ),
    "mistral": (
        "mlx-community/Mistral-7B-Instruct-v0.3-4bit",
        "mistralai/Mistral-7B-Instruct-v0.3",
    ),
    "qwen2.5:7b-instruct-q4_k_m": (
        "mlx-community/Qwen2.5-7B-Instruct-4bit",
        "Qwen/Qwen2.5-7B-Instruct",
    ),
    "qwen2.5": (
        "mlx-community/Qwen2.5-7B-Instruct-4bit",
        "Qwen/Qwen2.5-7B-Instruct",
    ),
}


def _resolve(model_name: str) -> str:
    """Return the HuggingFace repo ID for the given model short-name."""
    entry = _REGISTRY.get(model_name.lower())
    if entry is None:
        # Treat as a raw HF repo ID (power users / CI overrides)
        return model_name
    mlx_repo, hf_repo = entry
    return mlx_repo if _IS_APPLE_SILICON else hf_repo


# ---------------------------------------------------------------------------
# Lazy-loaded model cache (one model per repo_id kept in memory)
# ---------------------------------------------------------------------------

_mlx_cache: dict[str, tuple[Any, Any]] = {}  # repo_id → (model, tokenizer)
_hf_cache: dict[str, Any] = {}  # repo_id → transformers pipeline


def _get_mlx(repo_id: str) -> tuple[Any, Any]:
    if repo_id not in _mlx_cache:
        import mlx_lm  # type: ignore[import]

        print(
            f"\n[finamt] First run — downloading model {repo_id}\n"
            f"         (~4 GB, cached in ~/.cache/huggingface/hub)\n"
        )
        model, tokenizer = mlx_lm.load(repo_id)
        _mlx_cache[repo_id] = (model, tokenizer)
        print("[finamt] Model ready.\n")
    return _mlx_cache[repo_id]


def _get_hf_pipeline(repo_id: str) -> Any:
    if repo_id not in _hf_cache:
        import torch  # type: ignore[import]
        from transformers import pipeline  # type: ignore[import]

        print(
            f"\n[finamt] First run — downloading model {repo_id}\n"
            f"         (~4 GB, cached in ~/.cache/huggingface/hub)\n"
        )
        kwargs: dict[str, Any] = {
            "task": "text-generation",
            "model": repo_id,
            "max_new_tokens": 1024,
        }
        if torch.cuda.is_available():
            try:
                # 4-bit quantisation on CUDA via bitsandbytes
                kwargs["model_kwargs"] = {"load_in_4bit": True}
                kwargs["device_map"] = "auto"
            except Exception:
                kwargs.pop("model_kwargs", None)
                kwargs["device"] = 0
        else:
            # CPU fallback — slow but functional
            kwargs["device_map"] = "cpu"
            kwargs["torch_dtype"] = torch.float32
        _hf_cache[repo_id] = pipeline(**kwargs)
        print("[finamt] Model ready.\n")
    return _hf_cache[repo_id]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate(
    prompt: str,
    model_name: str,
    *,
    temperature: float = 0.0,
    top_p: float = 1.0,
    max_tokens: int = 1024,
) -> str:
    """
    Generate text from *prompt* using the local backend appropriate for this platform.

    Parameters
    ----------
    prompt:      The full instruction / user message.
    model_name:  Short config name (e.g. "mistral:7b") or raw HF repo ID.
    temperature: Sampling temperature.  0.0 = greedy / deterministic.
    top_p:       Nucleus sampling threshold.
    max_tokens:  Maximum number of *new* tokens to generate.

    Returns
    -------
    The generated text string (assistant reply only, no prompt echo).
    """
    repo_id = _resolve(model_name)

    if _IS_APPLE_SILICON:
        import mlx_lm  # type: ignore[import]

        model, tokenizer = _get_mlx(repo_id)
        messages = [{"role": "user", "content": prompt}]
        try:
            formatted: str = tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
        except Exception:
            formatted = prompt
        result = mlx_lm.generate(
            model,
            tokenizer,
            prompt=formatted,
            max_tokens=max_tokens,
            verbose=False,
        )
        # mlx_lm may return the full text (prompt + generation) in some versions.
        # Strip the prompt portion if it appears at the start.
        if result and formatted and result.startswith(formatted):
            result = result[len(formatted) :]
        return result.strip()
    else:
        pipe = _get_hf_pipeline(repo_id)
        messages = [{"role": "user", "content": prompt}]
        out = pipe(
            messages,
            max_new_tokens=max_tokens,
        )
        result = out[0]["generated_text"]
        # pipeline returns the full conversation list; last entry is assistant reply
        if isinstance(result, list):
            return str(result[-1].get("content", ""))
        return str(result)
