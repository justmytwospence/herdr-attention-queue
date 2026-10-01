"""Does a finished turn end by asking the user something? Jev decides.

A turn that ends with "Want me to do 1 and 2 now?" leaves the agent waiting on
the user just like an open question dialog, but every harness reports it as an
ordinary finished turn. `attention.py ask-check` asks Jev, TypeSafe's System One
model, about the turn's final message, so harness hooks (and the pi bridge) can
report such turns as blocked.

Off without TYPESAFE_API_KEY, or with {"ask_judge": false} in config.json.
Every failure (no key, network, timeout, bad reply) answers "does not ask".
On 73 labelled turn endings it found every question at threshold 0.8, with 3
false positives, 2 of them requests made earlier in the message.
"""

import json
import os
import urllib.request
from typing import Optional

from . import config

API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
DEFAULT_THRESHOLD = 0.8
TIMEOUT_S = 5.0
# The question usually sits at the end; the rest only adds cost.
MAX_CHARS = 3000

QUESTION = {
    "type": "noul",
    "instructions": (
        "`message` is the final message of an AI coding agent's turn; the agent now waits for the "
        "user. Does it end by asking the user a question, or for a decision or go-ahead, that the "
        "agent needs answered before it continues?"
    ),
    "criteria": {
        "true": "It asks the user something directly: a question to answer, options to choose "
        "between, or permission/go-ahead to proceed",
        "false": "It reports results or status, or only makes a conditional offer (\"I can do X if "
        "you want\", \"tell me if it breaks\") that needs no answer",
    },
}


def settings() -> dict:
    conf = config.load()
    threshold = conf.get("ask_threshold", DEFAULT_THRESHOLD)
    return {
        "enabled": config.enabled("ask_judge"),
        "threshold": threshold if isinstance(threshold, (int, float)) else DEFAULT_THRESHOLD,
        "model": conf.get("ask_model") if isinstance(conf.get("ask_model"), str) else DEFAULT_MODEL,
    }


def _text(content) -> str:
    """Text of a message's content: a string, or a list of {type: text} parts."""
    if isinstance(content, str):
        return content
    parts = []
    for part in content if isinstance(content, list) else ():
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "\n".join(parts)


def from_transcript(path: str) -> Optional[str]:
    """The last assistant text in a Claude Code transcript (JSON lines)."""
    last = None
    try:
        with open(path, errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                message = entry.get("message") if isinstance(entry, dict) else None
                if entry.get("type") == "assistant" and isinstance(message, dict):
                    text = _text(message.get("content")).strip()
                    if text:
                        last = text
    except OSError:
        return None
    return last


def final_message(payload) -> Optional[str]:
    """The turn's final message from a hook payload or {"message": text}."""
    if isinstance(payload, str):
        return payload.strip() or None
    if not isinstance(payload, dict):
        return None
    for key in ("message", "last_assistant_message"):
        text = _text(payload.get(key)).strip()
        if text:
            return text
    path = payload.get("transcript_path")
    if isinstance(path, str) and path:
        return from_transcript(os.path.expanduser(path))
    return None


def probability(text: str, model: str = DEFAULT_MODEL, timeout: float = TIMEOUT_S) -> Optional[float]:
    """Jev's probability that the message asks the user something; None when unavailable."""
    key = (os.environ.get("TYPESAFE_API_KEY") or "").strip()
    if not key or not text:
        return None
    body = json.dumps({
        "model": model,
        "state": {"message": text[-MAX_CHARS:]},
        "questions": {"asks": QUESTION},
    }).encode()
    request = urllib.request.Request(
        os.environ.get("HERDR_ATTENTION_QUEUE_JEV_URL") or API_URL,
        body,
        {"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            answer = (json.loads(reply.read().decode()).get("answers") or {}).get("asks") or {}
    except (OSError, ValueError):
        return None
    value = answer.get("noul")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def check(payload) -> dict:
    """{"asks": bool, "p": probability or None, "reason": why not asked, if not}."""
    conf = settings()
    if not conf["enabled"]:
        return {"asks": False, "p": None, "reason": "ask_judge is off"}
    text = final_message(payload)
    if not text:
        return {"asks": False, "p": None, "reason": "no final message"}
    p = probability(text, conf["model"])
    if p is None:
        return {"asks": False, "p": None, "reason": "Jev unavailable"}
    return {"asks": p >= conf["threshold"], "p": round(p, 3)}
