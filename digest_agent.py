"""
AI News Digest Agent
--------------------
Scrapes The Rundown AI and TLDR Tech homepages, filters stories by topic
keywords, and emails a daily digest via Gmail SMTP.

Setup:
  1. pip install requests beautifulsoup4
  2. Copy config.example.json -> config.json and fill in your details
  3. For Gmail: create an App Password (https://myaccount.google.com/apppasswords)
  4. python digest_agent.py          (run manually to test)
  5. Schedule daily with Windows Task Scheduler (see schedule_task.bat)
"""

import json
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
STATE_PATH = BASE_DIR / "seen_links.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}

# ---------------------------------------------------------------------------
# Topic keywords — case-insensitive. Stories matching ANY of these are included.
# ---------------------------------------------------------------------------
TOPIC_KEYWORDS = [
    # AI models
    "ai model", "language model", "llm", "frontier model", "open-weight",
    "open weight", "foundation model", "gemini", "gpt", "claude", "llama",
    "mistral", "deepseek", "embedding", "fine-tune", "finetune", "inference",
    # Governance
    "governance", "regulation", "compliance", "policy", "public benefit",
    "trust", "board", "oversight", "liability", "lawsuit", "audit",
    "eu ai act", "executive order", "transparency",
    # Guardrails / safety
    "guardrail", "safety", "alignment", "jailbreak", "red team", "red-team",
    "reward hacking", "sandbox", "agent safety", "harmless", "misuse",
    "content moderation",
    # PII / privacy
    "pii", "privacy", "personal information", "data protection", "gdpr",
    "confidential", "deanonym", "anonymiz", "deepfake", "identity",
    "surveillance", "scraping",
    # Document processing
    "document", "rag", "retrieval", "ocr", "pdf", "extraction",
    "knowledge base", "enterprise search",
    # Agents (cross-cutting)
    "ai agent", "agentic", "autonomous agent", "mcp",
]

MIN_SCORE = 1          # one keyword hit is enough
MAX_STORIES_PER_SOURCE = 15
REQUEST_TIMEOUT = 20


# ---------------------------------------------------------------------------
# Fetching & parsing
# ---------------------------------------------------------------------------
def fetch(url: str) -> BeautifulSoup | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        print(f"[warn] failed to fetch {url}: {exc}", file=sys.stderr)
        return None


def score_story(text: str) -> int:
    lowered = text.lower()
    return sum(1 for kw in TOPIC_KEYWORDS if kw in lowered)


def parse_rundown() -> list[dict]:
    """The Rundown AI — article links under /articles/ and /news/."""
    soup = fetch("https://www.therundown.ai/")
    stories: list[dict] = []
    if not soup:
        return stories
    seen_urls: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not re.search(r"^/(articles|news)/", href):
            continue
        url = urljoin("https://www.therundown.ai/", href)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        title = a.get_text(" ", strip=True)
        if not title or len(title) < 15:
            continue
        # Strip leading category tag ("AI ", "Tech ") and trailing
        # "PLUS: ..." teaser plus "Author Name • 8 minutes" metadata
        title = re.sub(r"^(AI|Tech|Robotics)\s+", "", title)
        title = re.split(r"\sPLUS:|\s+[A-Z][a-z]+\s+[A-Z][a-z]+\s*[\u2022•]?\s*\d+\s*minutes?|\b\d+\s*minutes?\b", title)[0].strip()
        score = score_story(title)
        if score >= MIN_SCORE:
            stories.append({"source": "The Rundown AI", "title": title,
                            "url": url, "score": score})
    stories.sort(key=lambda s: s["score"], reverse=True)
    return stories[:MAX_STORIES_PER_SOURCE]


def parse_tldr() -> list[dict]:
    """TLDR Tech — external story links from the homepage list."""
    soup = fetch("https://tldr.tech/")
    stories: list[dict] = []
    if not soup:
        return stories
    seen_urls: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        parsed = urlparse(href)
        if parsed.hostname in ("tldr.tech", "www.tldr.tech", None):
            continue
        if not parsed.scheme.startswith("http"):
            continue
        if href in seen_urls:
            continue
        seen_urls.add(href)
        title = a.get_text(" ", strip=True)
        if not title or len(title) < 20:
            continue
        score = score_story(title)
        if score >= MIN_SCORE:
            stories.append({"source": "TLDR Tech", "title": title,
                            "url": href, "score": score})
    stories.sort(key=lambda s: s["score"], reverse=True)
    return stories[:MAX_STORIES_PER_SOURCE]


# ---------------------------------------------------------------------------
# State — avoid resending the same story two days running
# ---------------------------------------------------------------------------
def load_seen() -> set[str]:
    if STATE_PATH.exists():
        try:
            return set(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_seen(seen: set[str]) -> None:
    # Keep state file from growing forever
    trimmed = list(seen)[-500:]
    STATE_PATH.write_text(json.dumps(trimmed, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def build_html(stories: list[dict]) -> str:
    if not stories:
        return (
            "<p>No new stories matched your topics today.</p>"
        )
    by_source: dict[str, list[dict]] = {}
    for s in stories:
        by_source.setdefault(s["source"], []).append(s)

    sections = []
    for source, items in by_source.items():
        rows = "".join(
            f'<li style="margin-bottom:10px">'
            f'<a href="{i["url"]}" style="color:#1a0dab;font-weight:600;text-decoration:none">{i["title"]}</a>'
            f'<br><span style="color:#666;font-size:12px">{i["source"]} &middot; relevance {i["score"]}</span>'
            f"</li>"
            for i in items
        )
        sections.append(f"<h3 style='margin-top:24px'>{source}</h3><ul>{rows}</ul>")

    return (
        "<div style='font-family:Segoe UI,Arial,sans-serif;max-width:640px'>"
        "<h2 style='margin-bottom:4px'>Your AI News Digest</h2>"
        f"<p style='color:#888'>{datetime.now().strftime('%A, %d %B %Y')}</p>"
        + "".join(sections)
        + "</div>"
    )


def send_email(config: dict, html: str, story_count: int) -> None:
    gmail = config["gmail"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = (
        f"AI Digest — {story_count} new stories "
        f"({datetime.now().strftime('%d %b')})"
    )
    msg["From"] = gmail["from"]
    msg["To"] = gmail["to"]
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(gmail["from"], gmail["app_password"])
        server.sendmail(gmail["from"], gmail["to"], msg.as_string())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    if not CONFIG_PATH.exists():
        print("[error] config.json not found. Copy config.example.json and fill it in.",
              file=sys.stderr)
        sys.exit(1)

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    seen = load_seen()

    stories = parse_rundown() + parse_tldr()
    new_stories = [s for s in stories if s["url"] not in seen]

    print(f"[info] {len(stories)} matched, {len(stories) - len(new_stories)} already seen")

    if not new_stories:
        if config.get("send_even_if_empty", False):
            send_email(config, build_html([]), 0)
            print("[info] sent empty digest")
        else:
            print("[info] nothing new — skipping send")
        return

    html = build_html(new_stories)
    send_email(config, html, len(new_stories))
    seen.update(s["url"] for s in new_stories)
    save_seen(seen)
    print(f"[info] emailed {len(new_stories)} stories to {config['gmail']['to']}")


if __name__ == "__main__":
    main()
