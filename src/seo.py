"""AI-generated YouTube SEO: title, description (with hashtags), tags.

Uses the Anthropic API (Claude Haiku -- cheap) when ANTHROPIC_API_KEY is set.
Falls back to a deterministic template so a missing key never drops a slot.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

_MODEL = os.environ.get("SEO_MODEL", "claude-haiku-4-5-20251001")


@dataclass
class Seo:
    title: str
    description: str
    tags: list[str]


_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
         "this", "that", "it", "is", "are", "my", "your", "you", "we", "so"}


def _keywords(text: str, n: int = 8) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z']+", (text or "").lower())
    seen: list[str] = []
    for w in words:
        if len(w) > 2 and w not in _STOP and w not in seen:
            seen.append(w)
        if len(seen) >= n:
            break
    return seen


def _fallback(caption: str, tiktok_tags: list[str], base_tags: list[str],
              is_short: bool) -> Seo:
    cap = (caption or "").strip().replace("\n", " ")
    cap_clean = re.sub(r"#\S+", "", cap).strip(" .,-|")   # drop hashtags from the title
    kws = _keywords(cap) or base_tags[:5] or ["viral", "trending"]
    title = (cap_clean[:80].rstrip(" .,-") if cap_clean else " ".join(kws[:6]).title())
    if is_short and "#shorts" not in title.lower():
        title = (title[:88] + " #Shorts").strip()
    tags = list(dict.fromkeys(
        [*base_tags, *[t.lower() for t in tiktok_tags], *kws,
         "shorts", "viral", "trending", "fyp"]
    ))[:20]
    hashtags = " ".join("#" + re.sub(r"[^a-z0-9]", "", t) for t in
                        (["shorts"] + kws[:4]) if t)
    desc = "\n".join(filter(None, [
        cap[:150] if cap else "",
        "",
        hashtags,
    ])).strip()
    return Seo(title[:100], desc[:4900], tags)


_PROMPT = """You are a YouTube Shorts SEO expert. Given a TikTok video's caption and \
its original hashtags, produce optimized YouTube metadata.

Return ONLY minified JSON with keys: "title", "description", "tags".
Rules:
- title: <= 90 chars, punchy, curiosity-driven, front-load the keyword, natural \
English, no clickbait lies, end with " #Shorts" if is_short is true.
- description: 2-4 short lines. Line 1 = a hook. Then a blank line. Then 4-6 \
relevant hashtags on one line. No links. <= 400 chars.
- tags: 15-20 lowercase search phrases, most specific first, no "#".
Caption: {caption}
Original hashtags: {hashtags}
is_short: {is_short}
"""


def generate(caption: str, tiktok_tags: list[str], base_tags: list[str],
             is_short: bool) -> Seo:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        print("[seo] no ANTHROPIC_API_KEY; using template fallback")
        return _fallback(caption, tiktok_tags, base_tags, is_short)
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model=_MODEL,
            max_tokens=700,
            messages=[{
                "role": "user",
                "content": _PROMPT.format(
                    caption=(caption or "")[:800],
                    hashtags=", ".join(tiktok_tags[:20]) or "(none)",
                    is_short=str(bool(is_short)).lower(),
                ),
            }],
        )
        raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        data = json.loads(raw)
        title = str(data.get("title", "")).strip()[:100]
        desc = str(data.get("description", "")).strip()[:4900]
        tags = [str(t).strip().lower().lstrip("#") for t in data.get("tags", []) if t]
        tags = list(dict.fromkeys([*base_tags, *tags]))[:25]
        if not title or not tags:
            raise ValueError("model returned empty title/tags")
        if is_short and "#shorts" not in title.lower() and len(title) <= 91:
            title = (title + " #Shorts").strip()
        print(f"[seo] AI metadata OK ({_MODEL})")
        return Seo(title, desc, tags)
    except Exception as exc:  # noqa: BLE001
        print(f"[seo] AI generation failed ({exc}); using template fallback")
        return _fallback(caption, tiktok_tags, base_tags, is_short)
