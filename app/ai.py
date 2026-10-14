"""Claude-powered task suggestions. Results are stored as status='suggested' until you approve them."""
import json
import os
from datetime import date
from pathlib import Path
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel, Field

from . import documents

MODEL = os.environ.get("HOUSEKEEPING_MODEL", "claude-opus-5-5")
FALLBACK = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
FULL_READ_MAX_PAGES = 100   # longer PDFs are sent as extracted text to keep reads affordable
MAX_TEXT_CHARS = 1_500_000  # roughly 400k tokens

_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            os.environ.pop("ANTHROPIC_API_KEY", None)  # an empty value from .env means "not set"
        _client = anthropic.Anthropic()  # ANTHROPIC_API_KEY or an `ant auth login` profile
    return _client


class SuggestedTask(BaseModel):
    title: str = Field(description="Short imperative, e.g. 'Replace HVAC filter'")
    description: str = Field(description="One or two sentences: how to do it and why it matters")
    recurrence: Literal["interval", "seasonal", "mileage"]
    interval_days: Optional[int] = Field(description="Days between repeats (interval), or the time cap for mileage tasks")
    months: Optional[list[int]] = Field(description="Seasonal only: months (1-12) in which this should be done once per year")
    mileage_interval: Optional[int] = Field(description="Mileage only: miles between repeats")
    priority: Literal[1, 2, 3] = Field(description="1 = safety / expensive-damage prevention, 2 = normal, 3 = nice to have")
    est_minutes: int
    asset_name: Optional[str] = Field(description="Guide import only: which inventory item this belongs to, exactly as listed, or null")


class Suggestions(BaseModel):
    tasks: list[SuggestedTask]


SYSTEM = """You are a practical home-maintenance planner. You produce recurring maintenance tasks for a homeowner's tracker.

Rules for each task:
- Use "interval" for things done every N days regardless of season (filters, cleaning, tests).
- Use "seasonal" for things tied to time of year (winterizing, pruning, fertilizing, gutter cleaning). Pick months that fit the user's location and climate; respect their hemisphere.
- Use "mileage" for vehicle service driven by miles, and also set interval_days as the time cap (e.g. oil every 5000 mi or 180 days).
- Prefer manufacturer-typical intervals for the specific make, model and year when you know them; otherwise use widely accepted guidance.
- Skip tasks the homeowner already has. Don't invent exotic tasks; aim for the handful that actually matter.
- Leave fields null when they don't apply to the chosen recurrence."""


def _context(settings: dict, today: date) -> str:
    return (
        f"Today: {today.isoformat()}\n"
        f"Location: {settings.get('location') or 'unknown'}\n"
        f"Hemisphere: {settings.get('hemisphere') or 'north'}\n"
        f"Climate notes: {settings.get('climate_notes') or 'none'}"
    )


def _parse(system: str, content: list, output_format, effort: str = "medium"):
    response = client().beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        system=system,
        output_config={"effort": effort},
        messages=[{"role": "user", "content": content}],
        output_format=output_format,
        **FALLBACK,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request.")
    if response.parsed_output is None:
        raise RuntimeError(f"Claude returned no result (stop reason: {response.stop_reason}).")
    return response.parsed_output


def _call(prompt: str, attachments: list | None = None) -> list[SuggestedTask]:
    return _parse(SYSTEM, (attachments or []) + [{"type": "text", "text": prompt}], Suggestions).tasks


def _format_notes(doc_notes: list[tuple[str, dict]]) -> str:
    """Compact text version of what Claude previously extracted from each manual."""
    out = []
    for title, n in doc_notes:
        lines = [f"## {title} ({n.get('document_type', 'document')})"]
        if n.get("model_identified"):
            lines.append(f"Model: {n['model_identified']}")
        for m in n.get("maintenance", []):
            page = f" [p. {m['page']}]" if m.get("page") else ""
            lines.append(f"- {m['task']}: {m['frequency']}. {m['details']}{page}")
        if n.get("parts"):
            lines.append("Parts: " + "; ".join(f"{p['name']}: {p['spec']}" for p in n["parts"]))
        out.append("\n".join(lines))
    return "\n\n".join(out)


def suggest_for_asset(asset: dict, existing_titles: list[str], settings: dict, today: date,
                      doc_notes: list[tuple[str, dict]] | None = None) -> list[SuggestedTask]:
    lines = [f"Category: {asset['category']}", f"Name: {asset['name']}"]
    if asset.get("details"):
        lines.append(f"Details: {asset['details']}")
    if asset.get("year"):
        lines.append(f"Year: {asset['year']}")
    if asset.get("mileage"):
        lines.append(f"Current mileage: {asset['mileage']}")
    if asset.get("notes"):
        lines.append(f"Notes: {asset['notes']}")
    existing = "\n".join(f"- {t}" for t in existing_titles) or "(none)"
    prompt = (
        f"{_context(settings, today)}\n\n"
        f"Suggest the recurring maintenance tasks for this item:\n" + "\n".join(lines) + "\n\n"
        f"Tasks already tracked for it:\n{existing}\n\n"
    )
    if doc_notes:
        prompt += (
            "Notes extracted from this item's own manuals follow. Where they give an interval, use it rather than "
            "general guidance, and cite the page in the description (e.g. 'Manual p. 34'). Include part numbers or "
            "sizes in the description when relevant.\n\n" + _format_notes(doc_notes) + "\n\n"
        )
    prompt += "Leave asset_name null."
    return _call(prompt)


def parse_guide(guide_text: str | None, asset_names: list[str], existing_titles: list[str], settings: dict, today: date,
                attachment: dict | None = None) -> list[SuggestedTask]:
    """Turn a guide into tasks. Pass pasted text, or an attachment block from document_block()."""
    assets = "\n".join(f"- {n}" for n in asset_names) or "(none yet)"
    existing = "\n".join(f"- {t}" for t in existing_titles) or "(none)"
    prompt = (
        f"{_context(settings, today)}\n\n"
        "Below is the homeowner's own maintenance guide. Convert every recurring item in it into a task. "
        "Keep their wording and frequencies where given; fill in sensible values where the guide is vague. "
        "If an item clearly belongs to one of their inventory items, set asset_name to that name exactly; otherwise null.\n\n"
        f"Inventory:\n{assets}\n\nAlready tracked (skip duplicates):\n{existing}"
    )
    if attachment:
        return _call(prompt + "\n\nThe guide is the attached document.", [attachment])
    return _call(prompt + f"\n\n<guide>\n{guide_text}\n</guide>")


# ---------- reading documents ----------

class ManualItem(BaseModel):
    task: str
    frequency: str = Field(description="As the document states it, e.g. 'every 3 months', 'annually', '7,500 miles or 12 months'")
    details: str = Field(description="How to do it, specs, cautions; one or two sentences")
    page: Optional[str] = Field(description="Page number(s) where this appears, if known")


class Part(BaseModel):
    name: str = Field(description="e.g. 'Air filter', 'Engine oil', 'Water filter cartridge'")
    spec: str = Field(description="Part number, size or grade, e.g. '16x25x1 MERV 8', '0W-20, 4.4 qt', 'EDR4RXD1'")


class DocNotes(BaseModel):
    document_type: str = Field(description="e.g. 'Owner's manual', 'Maintenance schedule', 'Data plate photo', 'Homeowner checklist'")
    model_identified: Optional[str] = Field(description="Exact model / serial / year the document covers, if stated")
    summary: str = Field(description="Two or three sentences on what this document covers")
    maintenance: list[ManualItem]
    parts: list[Part]
    warnings: list[str] = Field(description="Safety warnings or things that void the warranty, only if maintenance-related")


READER_SYSTEM = """You read appliance manuals, vehicle owner's manuals, plant care guides and homeowner checklists, and pull out
what matters for ongoing maintenance. Be faithful to the document: use its intervals, part numbers and wording.
Don't add maintenance the document doesn't mention. If it's a photo of a data plate or label, capture the model, serial
and manufacture date and anything printed about service or filters."""


def document_block(doc: dict, path: Path) -> tuple[dict, str, str | None]:
    """Build the content block Claude reads for a stored document.

    Returns (block, read_mode, files_api_id). PDFs up to FULL_READ_MAX_PAGES and images go through the Files API
    so Claude sees layout, tables and diagrams; longer PDFs and web pages go as text.
    """
    mime, title = doc["mime"], doc["title"]
    file_id = doc.get("anthropic_file_id")

    def upload() -> str:
        nonlocal file_id
        if not file_id:
            file_id = client().files.upload(file=(doc["filename"], path.read_bytes(), mime)).id
        return file_id

    if mime.startswith("image/"):
        return {"type": "image", "source": {"type": "file", "file_id": upload()}}, "full", file_id

    if mime == "application/pdf":
        pages = doc.get("pages") or 0
        if pages <= FULL_READ_MAX_PAGES:
            return {"type": "document", "title": title, "source": {"type": "file", "file_id": upload()}}, "full", file_id
        _, text = documents.pdf_info(path.read_bytes())
        if not text.strip():
            if pages > 600:
                raise RuntimeError(f"This PDF is a {pages}-page scan with no text layer, which is too long for Claude to read.")
            return {"type": "document", "title": title, "source": {"type": "file", "file_id": upload()}}, "full", file_id
    else:
        text = path.read_text(encoding="utf-8", errors="replace")

    if len(text) > MAX_TEXT_CHARS:
        raise RuntimeError("This document is too long for Claude to read in one pass. Upload just the maintenance section.")
    block = {"type": "document", "title": title, "source": {"type": "text", "media_type": "text/plain", "data": text}}
    return block, "text", file_id


def read_document(doc: dict, path: Path, asset: dict | None) -> tuple[DocNotes, str, str | None]:
    block, mode, file_id = document_block(doc, path)
    about = f"{asset['name']} ({asset.get('details') or asset['category']}, {asset.get('year') or 'year unknown'})" if asset else "the home in general"
    prompt = f"This document belongs to: {about}. Extract its maintenance information."
    notes = _parse(READER_SYSTEM, [block, {"type": "text", "text": prompt}], DocNotes)
    return notes, mode, file_id


# ---------- finding manuals online ----------

class FoundDocument(BaseModel):
    title: str
    url: str
    kind: Literal["manual", "maintenance", "guide", "other"]
    why: str


REPORT_TOOL = {
    "name": "report_documents",
    "description": "Report the documents you found. Call this exactly once when you are done searching.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "documents": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "e.g. 'Carrier 59SC5 Owner's Manual'"},
                        "url": {"type": "string", "description": "Direct link to the file"},
                        "kind": {"type": "string", "enum": ["manual", "maintenance", "guide", "other"]},
                        "why": {"type": "string", "description": "One line: what it is and how sure you are it matches"},
                    },
                    "required": ["title", "url", "kind", "why"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["documents"],
        "additionalProperties": False,
    },
}

FINDER_SYSTEM = """You find official maintenance documentation for things a homeowner owns: owner's manuals, use-and-care guides,
maintenance schedules, and for plants, reputable care guides (university extension services are ideal).

- Prefer the manufacturer's own site, then reputable manual archives. Avoid sites that gate downloads behind sign-ups or ads.
- Prefer direct PDF links. Use web_fetch to confirm a link actually serves the document before reporting it.
- If the exact model is unknown, find the closest match for the brand, type and year and say so in "why".
- Report at most 3 documents, best first. Report an empty list rather than guessing.
- When finished, call report_documents."""


def find_documents(asset: dict, settings: dict) -> list[FoundDocument]:
    desc = ", ".join(str(v) for v in [asset["name"], asset.get("details"), asset.get("year")] if v)
    first = {"role": "user", "content": f"Find maintenance documentation for: {desc} (category: {asset['category']}). "
                                        f"Owner location: {settings.get('location') or 'unknown'}."}
    messages = [first]
    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 6},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 8},
        REPORT_TOOL,
    ]
    for _ in range(6):
        response = client().beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=FINDER_SYSTEM,
            tools=tools,
            messages=messages,
            output_config={"effort": "medium"},
            **FALLBACK,
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined this request.")
        report = next((b for b in response.content if b.type == "tool_use" and b.name == "report_documents"), None)
        if report:
            raw = report.input if isinstance(report.input, dict) else json.loads(report.input)
            return [FoundDocument.model_validate(d) for d in raw.get("documents", [])]
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue  # server-side search loop hit its limit; re-send to let it resume
        messages.append({"role": "user", "content": "Call report_documents now with what you found (an empty list is fine)."})
    raise RuntimeError("Claude didn't finish searching. Try again.")


def forget_file(file_id: str | None):
    """Remove a document from Anthropic's file storage when you delete it here."""
    if not file_id:
        return
    try:
        client().files.delete(file_id)
    except (anthropic.APIError, TypeError):
        pass  # already gone, offline, or no credentials; nothing for the user to do
