from __future__ import annotations

import re


MENTIONS = re.compile(r"<[@#][!&]?\d+>")
URLS = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
EMAILS = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
TOKENS = re.compile(r"\b(?:mfa\.[\w-]{30,}|[\w-]{20,}\.[\w-]{5,}\.[\w-]{20,}|(?:sk-|ghp_|github_pat_)[\w-]{16,})\b")
NUMBERS = re.compile(r"(?<!\w)\+?\d[\d ().-]{5,}\d(?!\w)")


def sanitize_example(text: str) -> str:
    """Basic redaction only. Human review is mandatory before loading."""
    text = TOKENS.sub("[secret]", text)
    text = URLS.sub("[link]", text)
    text = EMAILS.sub("[email]", text)
    text = MENTIONS.sub("[mention]", text)
    text = NUMBERS.sub("[number]", text)
    text = text.replace("@everyone", "[everyone]").replace("@here", "[here]")
    return " ".join(text.split()).strip()


def clean_input(text: str) -> str:
    # Do not send numeric Discord identities to the model.
    return MENTIONS.sub("[person]", text).strip()


def clean_output(text: str) -> str:
    # Strip internal thinking if a model emits it in its ordinary content field.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    if "<think>" in text.lower():
        text = text[:text.lower().index("<think>")]
    text = TOKENS.sub("[secret]", text)
    text = MENTIONS.sub("[mention]", text)
    text = text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    return text.strip()[:1800]

