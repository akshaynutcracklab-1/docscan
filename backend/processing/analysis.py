"""Regex-based analysis of the recognised text (no Azure calls)."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

import dateparser

# Candidates first, then dateparser decides whether they are real dates. Day-first (India).
_DATE_CANDIDATE = re.compile(
    r"""\b(
        \d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}                       # 15/01/2025, 15-01-25
      | \d{4}-\d{2}-\d{2}                                       # 2025-01-15
      | \d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\.?,?\s+\d{4}    # 15 Jan 2025, 1st January, 2025
      | [A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}    # Jan 15, 2025
    )\b""",
    re.VERBOSE,
)
_MONTHS = re.compile(r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec", re.I)
_DATE_SETTINGS = {"DATE_ORDER": "DMY", "PREFER_DAY_OF_MONTH": "first", "REQUIRE_PARTS": ["day", "month", "year"]}

_AMOUNT = re.compile(
    r"(?P<cur>₹|Rs\.?|INR|\$|USD)\s?(?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)",
    re.I,
)
_CURRENCY_CODES = {"₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR", "$": "USD", "usd": "USD"}

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Indian mobile numbers: optional +91 / 91 / 0 prefix, 10 digits starting 6-9, optional space after 5.
_PHONE = re.compile(r"(?<![\d+])(?:\+91[\s-]?|91[\s-]|0)?([6-9]\d{4})[\s-]?(\d{5})(?!\d)")
_GSTIN = re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_WORD = re.compile(r"\w+", re.UNICODE)


def _unique(items):
    seen, out = set(), []
    for item in items:
        key = repr(item)
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def find_dates(text: str) -> list[dict]:
    found = []
    for match in _DATE_CANDIDATE.finditer(text):
        raw = match.group(1)
        if raw[0].isalpha() and not _MONTHS.match(raw):
            continue
        parsed = dateparser.parse(raw, languages=["en"], settings=_DATE_SETTINGS)
        if parsed:
            found.append({"raw": raw, "date": parsed.date().isoformat()})
    return _unique(found)


def find_amounts(text: str) -> list[dict]:
    found = []
    for match in _AMOUNT.finditer(text):
        try:
            value = Decimal(match.group("num").replace(",", ""))
        except InvalidOperation:
            continue
        found.append({
            "raw": match.group(0),
            "currency": _CURRENCY_CODES.get(match.group("cur").lower(), match.group("cur")),
            "value": str(value),
        })
    return _unique(found)


def find_emails(text: str) -> list[str]:
    return _unique(m.lower() for m in _EMAIL.findall(text))


def find_phones(text: str) -> list[str]:
    return _unique(f"+91{a}{b}" for a, b in _PHONE.findall(text))


def find_gstins(text: str) -> list[str]:
    return _unique(_GSTIN.findall(text.upper()))


def find_pans(text: str) -> list[str]:
    return _unique(_PAN.findall(text.upper()))


def word_count(text: str) -> int:
    return len(_WORD.findall(text))


def analyze_text(text: str, *, low_confidence_pages: list[dict] | None = None) -> dict:
    text = text or ""
    return {
        "word_count": word_count(text),
        "character_count": len(text),
        "dates": find_dates(text),
        "amounts": find_amounts(text),
        "emails": find_emails(text),
        "phones": find_phones(text),
        "gstins": find_gstins(text),
        "pans": find_pans(text),
        "low_confidence_pages": low_confidence_pages or [],
    }
