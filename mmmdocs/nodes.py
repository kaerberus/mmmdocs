"""Model nodes: local Ollama and any OpenAI-compatible endpoint.

Both backends expose one function that returns raw string content. The caller
picks `ollama` (default, no API key) or `openai` (a cloud model, e.g.
DeepSeek), so the model per node is a configuration choice, not a code change.
"""
from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request


class ModelError(Exception):
    pass


def _post_json(url, payload, headers=None, timeout=300):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise ModelError("HTTP %s from %s: %s" % (exc.code, url, detail[:500]))
    except Exception as exc:
        raise ModelError("%s (%s)" % (exc, url))


def _b64(path):
    with open(path, "rb") as handle:
        return base64.b64encode(handle.read()).decode("ascii")


def ollama_chat(model, system, user_text, image_paths=None,
                host="http://localhost:11434", timeout=600):
    message = {"role": "user", "content": user_text}
    if image_paths:
        message["images"] = [_b64(p) for p in image_paths]
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, message],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }
    out = _post_json(host.rstrip("/") + "/api/chat", payload, timeout=timeout)
    return (out.get("message") or {}).get("content", "")


def openai_chat(model, system, user_text, image_paths=None,
                base_url="https://api.deepseek.com/v1", api_key=None,
                timeout=600, json_mode=True):
    if not api_key:
        raise ModelError("openai backend requires an API key")
    content = [{"type": "text", "text": user_text}]
    for path in image_paths or []:
        content.append({
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + _b64(path)},
        })
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
        "temperature": 0,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Authorization": "Bearer " + api_key}
    out = _post_json(base_url.rstrip("/") + "/chat/completions", payload, headers, timeout)
    try:
        return out["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ModelError("unexpected response: %s" % json.dumps(out)[:400])


def chat(backend, model, system, user_text, image_paths=None, **opts):
    """Dispatch to the configured backend."""
    if backend == "openai":
        return openai_chat(model, system, user_text, image_paths, **opts)
    return ollama_chat(model, system, user_text, image_paths, **opts)


def _normalize(vec):
    norm = sum(v * v for v in vec) ** 0.5
    return [v / norm for v in vec] if norm else list(vec)


def ollama_embed(model, texts, host="http://localhost:11434", timeout=300):
    """Batch embeddings via Ollama's /api/embed. Returns unit-normalized vectors."""
    out = _post_json(host.rstrip("/") + "/api/embed",
                     {"model": model, "input": list(texts)}, timeout=timeout)
    vectors = out.get("embeddings")
    if vectors is None and "embedding" in out:
        vectors = [out["embedding"]]
    if not vectors or len(vectors) != len(texts):
        raise ModelError("embedding count mismatch from %s" % model)
    return [_normalize(v) for v in vectors]


def openai_embed(model, texts, base_url="https://api.deepseek.com/v1",
                 api_key=None, timeout=300):
    if not api_key:
        raise ModelError("embeddings require an API key")
    out = _post_json(base_url.rstrip("/") + "/embeddings",
                     {"model": model, "input": list(texts)},
                     {"Authorization": "Bearer " + api_key}, timeout)
    data = sorted(out.get("data", []), key=lambda d: d.get("index", 0))
    if len(data) != len(texts):
        raise ModelError("embedding count mismatch from %s" % model)
    return [_normalize(d["embedding"]) for d in data]


def embed(backend, model, texts, **opts):
    """Dispatch batch embeddings to the configured backend."""
    texts = list(texts)
    if not texts:
        return []
    if backend == "openai":
        return openai_embed(model, texts, **opts)
    return ollama_embed(model, texts, **opts)


def parse_json(text):
    """Parse a JSON object from a model reply, tolerating code fences/prose."""
    if not text or not text.strip():
        raise ModelError("empty model response")
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    if start == -1:
        raise ModelError("no JSON object in response: %s" % text[:300])
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception as exc:
                    raise ModelError("invalid JSON object: %s" % exc)
    raise ModelError("unterminated JSON object: %s" % text[:300])
