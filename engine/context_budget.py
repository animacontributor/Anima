"""Local context estimates and admission checks; never logs prompt contents.

These estimates are deliberately conservative for ordinary prose, not a
replacement for the model's tokenizer. Actual Ollama counts remain the meter.
"""
from __future__ import annotations

import json
import math

import config


class ContextFull(RuntimeError):
    """A request was stopped locally, before Ollama could truncate it."""


def text_tokens(text: str) -> int:
    # Non-ASCII text can tokenize much more densely than English prose.
    encoded = str(text).encode("utf-8")
    ascii_bytes = len(encoded.decode("ascii", "ignore"))
    return math.ceil(ascii_bytes / 3 + (len(encoded) - ascii_bytes) / 2)


def estimate(messages: list[dict], definitions: list[dict] | None = None) -> dict:
    parts = {"system": 0, "dialogue": 0, "tool_results": 0, "tool_schemas": 0, "images": 0}
    image_cost = int(getattr(config, "CONTEXT_IMAGE_TOKENS", 8192))
    for message in messages:
        role = message.get("role", "")
        bucket = "system" if role == "system" else "tool_results" if role == "tool" else "dialogue"
        parts[bucket] += 16 + text_tokens(message.get("content") or "")
        for key in ("tool_calls", "thinking"):
            if message.get(key):
                parts[bucket] += text_tokens(json.dumps(message[key], ensure_ascii=False))
        parts["images"] += len(message.get("images") or []) * image_cost
    if definitions:
        parts["tool_schemas"] = text_tokens(json.dumps(definitions, ensure_ascii=False))
    return {"estimated_input": sum(parts.values()), "estimated_parts": parts, "estimated": True}


def check(payload: dict) -> dict:
    """Check every chat POST, including retries, without modifying its input."""
    report = estimate(payload.get("messages") or [], payload.get("tools"))
    options = payload.get("options") or {}
    window = int(options.get("num_ctx") or getattr(config, "NUM_CTX", 0))
    requested = int(options.get("num_predict", 8192))
    output = requested if requested > 0 else 8192
    margin = int(getattr(config, "CONTEXT_RESERVE_TOKENS", 4096))
    report.update(window=window, reserved_output=output, reserve=margin)
    if (getattr(config, "CONTEXT_GUARD", True) and window
            and report["estimated_input"] + output + margin > window):
        raise ContextFull(
            f"Context needs a fold or a smaller attachment before continuing "
            f"(estimated input {report['estimated_input']:,}, output reserve {output:,}, "
            f"safety reserve {margin:,}, window {window:,}). "
            "The request was not sent; the existing conversation is retained.")
    return report
