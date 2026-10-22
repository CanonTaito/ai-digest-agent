"""
AI News Digest — Azure Function (Timer Trigger)
------------------------------------------------
Runs daily, scrapes The Rundown AI + TLDR Tech, filters by topic keywords,
and emails a digest via Gmail SMTP.

Configuration (App Settings, not code):
  GMAIL_FROM          - your Gmail address
  GMAIL_TO            - recipient (usually the same)
  GMAIL_APP_PASSWORD  - Gmail app password
  SEND_EVEN_IF_EMPTY  - "true" to always send

State (seen links) is stored in Blob Storage using the
built-in AzureWebJobsStorage connection.
"""

import json
import logging
import os
import re
import smtplib
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urljoin, urlparse

import azure.functions as func
import requests
from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient
from bs4 import BeautifulSoup

app = func.FunctionApp()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}

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

MIN_SCORE = 1
MAX_STORIES_PER_SOURCE = 15
REQUEST_TIMEOUT = 20
BLOB_CONTAINER = "digest-state"
BLOB_NAME = "seen_links.json"


# ---------------------------------------------------------------------------
# Fetching & parsing
# ---------------------------------------------------------------------------
def fetch(url: str):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        logging.warning("Failed to fetch %s: %s", url, exc)
        return None


def score_story(text: str) -> int:
    lowered = text.lower()
    return sum(1 for kw in TOPIC_KEYWORDS if kw in lowered)


def parse_rundown() -> list[dict]:
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
        title = re.sub(r"^(AI|Tech|Robotics)\s+", "", title)
        title = re.split(
            r"\sPLUS:|\s+[A-Z][a-z]+\s+[A-Z][a-z]+\s*[\u2022•]?\s*\d+\s*minutes?"
            r"|\b\d+\s*minutes?\b",
            title,
        )[0].strip()
        score = score_story(title)
        if score >= MIN_SCORE:
            stories.append({"source": "The Rundown AI", "title": title,
                            "url": url, "score": score})
    stories.sort(key=lambda s: s["score"], reverse=True)
    return stories[:MAX_STORIES_PER_SOURCE]


def parse_tldr() -> list[dict]:
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
# Blob state
# ---------------------------------------------------------------------------
def load_seen() -> set[str]:
    conn = os.environ.get("AzureWebJobsStorage")
    try:
        client = BlobServiceClient.from_connection_string(conn)
        blob = client.get_blob_client(BLOB_CONTAINER, BLOB_NAME)
        data = blob.download_blob().readall()
        return set(json.loads(data))
    except Exception:
        return set()  # container/blob missing on first run


def save_seen(seen: set[str]) -> None:
    conn = os.environ.get("AzureWebJobsStorage")
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            client = BlobServiceClient.from_connection_string(conn)
            try:
                client.create_container(BLOB_CONTAINER)
            except ResourceExistsError:
                pass
            blob = client.get_blob_client(BLOB_CONTAINER, BLOB_NAME)
            trimmed = list(seen)[-500:]
            blob.upload_blob(json.dumps(trimmed), overwrite=True)
            return
        except Exception as e:
            last_err = e
            logging.warning("save_seen attempt %d failed: %s", attempt + 1, e)
            time.sleep(2 * (attempt + 1))
    raise last_err  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def build_html(stories: list[dict]) -> str:
    if not stories:
        return "<p>No new stories matched your topics today.</p>"
    by_source: dict[str, list[dict]] = {}
    for s in stories:
        by_source.setdefault(s["source"], []).append(s)
    sections = []
    for source, items in by_source.items():
        rows = "".join(
            f'<li style="margin-bottom:10px">'
            f'<a href="{i["url"]}" style="color:#1a0dab;font-weight:600;'
            f'text-decoration:none">{i["title"]}</a>'
            f'<br><span style="color:#666;font-size:12px">{i["source"]} '
            f"&middot; relevance {i['score']}</span></li>"
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


def send_email(html: str, story_count: int) -> None:
    from_addr = os.environ["GMAIL_FROM"]
    to_addr = os.environ["GMAIL_TO"]
    password = os.environ["GMAIL_APP_PASSWORD"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = (
        f"AI Digest — {story_count} new stories "
        f"({datetime.now().strftime('%d %b')})"
    )
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(html, "html"))

    # Port 465 (SSL) is allowed outbound from Azure; port 25 is blocked.
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(from_addr, password)
        server.sendmail(from_addr, to_addr, msg.as_string())


# ---------------------------------------------------------------------------
# Timer trigger — daily. Default 22:00 UTC = 8:00 AM AEST (Queensland, no DST).
# Override in the portal with: NCRONTAB="0 0 <hour> * * *"
# ---------------------------------------------------------------------------
@app.timer_trigger(
    schedule="0 0 22 * * *",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def digest_timer(timer: func.TimerRequest) -> None:
    logging.info("AI digest run started.")

    seen = load_seen()
    stories = parse_rundown() + parse_tldr()
    new_stories = [s for s in stories if s["url"] not in seen]
    logging.info("%d matched, %d already seen", len(stories), len(stories) - len(new_stories))

    send_empty = os.environ.get("SEND_EVEN_IF_EMPTY", "false").lower() == "true"

    if not new_stories and not send_empty:
        logging.info("Nothing new - skipping send.")
        return

    send_email(build_html(new_stories), len(new_stories))
    seen.update(s["url"] for s in new_stories)
    try:
        save_seen(seen)
    except Exception:
        logging.error(
            "Emailed %d stories but state save failed; they will be re-sent "
            "on the next run unless this is fixed.",
            len(new_stories),
            exc_info=True,
        )
        return
    logging.info("Emailed %d stories to %s", len(new_stories), os.environ["GMAIL_TO"])
