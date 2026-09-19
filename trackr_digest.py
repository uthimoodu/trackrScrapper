#!/usr/bin/env python3
"""Email a daily digest of recently opened Trackr spring week programmes."""

from __future__ import annotations

import argparse
import html
import json
import os
import smtplib
import ssl
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


TRACKR_API_URL = "https://api.the-trackr.com/programmes"
TRACKR_URL = "https://app.the-trackr.com/uk-finance/spring-weeks"
DATE_DISPLAY_FORMAT = "%d %b %Y"


@dataclass(frozen=True)
class Programme:
    id: str
    company: str
    name: str
    opening_date: date
    closing_date: date | None
    url: str | None
    categories: tuple[str, ...]
    process: tuple[str, ...]
    rolling: bool | None
    cover_letter: str | None
    written_answers: str | None
    sponsors_visa: str | None
    notes: str | None


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE lines without requiring python-dotenv."""
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None

    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        return None


def format_date(value: date | None) -> str:
    if value is None:
        return "-"
    return value.strftime(DATE_DISPLAY_FORMAT)


def infer_query_from_trackr_url(trackr_url: str) -> tuple[str | None, str | None, str | None]:
    normalized_url = trackr_url.rstrip("/")
    if normalized_url != TRACKR_URL:
        raise RuntimeError(f"This scraper is locked to the exact Spring Weeks URL: {TRACKR_URL}")

    parts = [part for part in urlparse(trackr_url).path.split("/") if part]
    region = None
    industry = None
    programme_type = None

    if parts:
        tracker_slug = parts[0]
        if "-" in tracker_slug:
            region_slug, industry_slug = tracker_slug.split("-", 1)
            region = region_slug.upper()
            industry = industry_slug.replace("-", " ").title()

    if len(parts) >= 2:
        programme_type = parts[1]

    return region, industry, programme_type


def fetch_programmes(
    *,
    region: str,
    industry: str,
    season: str,
    programme_type: str,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    query = urlencode(
        {
            "region": region,
            "industry": industry,
            "season": season,
            "type": programme_type,
        }
    )
    request = Request(
        f"{TRACKR_API_URL}?{query}",
        headers={
            "Accept": "application/json",
            "User-Agent": "trackr-digest/1.0 (+https://app.the-trackr.com)",
        },
    )

    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Trackr API returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach Trackr API: {exc.reason}") from exc

    data = json.loads(payload)
    programmes = data.get("programmes")
    if not isinstance(programmes, list):
        raise RuntimeError("Trackr API response did not include a programmes list.")
    return programmes


def normalize_programme(raw: dict[str, Any], *, fallback_url: str) -> Programme | None:
    opening_date = parse_iso_date(raw.get("openingDate"))
    if opening_date is None:
        return None

    company = raw.get("company") if isinstance(raw.get("company"), dict) else {}
    company_name = str(company.get("name") or raw.get("companyId") or "Unknown company")
    categories = tuple(str(item) for item in raw.get("categories") or [])
    process = tuple(str(item) for item in raw.get("process") or [])

    return Programme(
        id=str(raw.get("id") or f"{company_name}:{raw.get('name')}:{raw.get('openingDate')}"),
        company=company_name,
        name=str(raw.get("name") or "Untitled programme"),
        opening_date=opening_date,
        closing_date=parse_iso_date(raw.get("closingDate")),
        url=raw.get("url") or fallback_url,
        categories=categories,
        process=process,
        rolling=raw.get("rolling") if isinstance(raw.get("rolling"), bool) else None,
        cover_letter=raw.get("coverLetter"),
        written_answers=raw.get("writtenAnswers"),
        sponsors_visa=company.get("sponsorsVisa") or raw.get("sponsorsVisa"),
        notes=raw.get("notes"),
    )


def filter_recent_openings(
    raw_programmes: list[dict[str, Any]],
    *,
    today: date,
    lookback_days: int,
    programme_type: str,
    fallback_url: str,
) -> list[Programme]:
    earliest = today - timedelta(days=lookback_days)
    programmes = []

    for raw in raw_programmes:
        raw_type = raw.get("type")
        if raw_type and str(raw_type).lower() != programme_type.lower():
            continue

        programme = normalize_programme(raw, fallback_url=fallback_url)
        if programme is None:
            continue
        if earliest <= programme.opening_date <= today:
            programmes.append(programme)

    return sorted(programmes, key=lambda item: (item.opening_date, item.company, item.name), reverse=True)


def plain_text_digest(programmes: list[Programme], *, lookback_days: int, today: date) -> str:
    lines = [
        f"Trackr Spring Weeks Digest - {today.strftime(DATE_DISPLAY_FORMAT)}",
        "",
        f"{len(programmes)} programme(s) opened in the last {lookback_days} days.",
        "Events are filtered by opening date only; a past closing date does not remove them.",
        "",
    ]

    for programme in programmes:
        closed_marker = ""
        if programme.closing_date is not None and programme.closing_date < today:
            closed_marker = " (closing date has passed)"
        lines.extend(
            [
                f"- {programme.company}: {programme.name}",
                f"  Opening date: {format_date(programme.opening_date)}",
                f"  Closing date: {format_date(programme.closing_date)}{closed_marker}",
                f"  Categories: {', '.join(programme.categories) or '-'}",
                f"  Process: {' > '.join(programme.process) or '-'}",
                f"  Rolling: {programme.rolling if programme.rolling is not None else '-'}",
                f"  Cover letter: {programme.cover_letter or '-'}",
                f"  Written answers: {programme.written_answers or '-'}",
                f"  Sponsors visa: {programme.sponsors_visa or '-'}",
                f"  Link: {programme.url}",
            ]
        )
        if programme.notes:
            lines.append(f"  Notes: {programme.notes}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def html_digest(programmes: list[Programme], *, lookback_days: int, today: date, trackr_url: str) -> str:
    rows = []
    for programme in programmes:
        closing = format_date(programme.closing_date)
        if programme.closing_date is not None and programme.closing_date < today:
            closing = f"{closing} (past)"

        link = programme.url
        rows.append(
            "<tr>"
            f"<td>{html.escape(programme.company)}</td>"
            f"<td><a href=\"{html.escape(link)}\">{html.escape(programme.name)}</a></td>"
            f"<td>{html.escape(format_date(programme.opening_date))}</td>"
            f"<td>{html.escape(closing)}</td>"
            f"<td>{html.escape(', '.join(programme.categories) or '-')}</td>"
            f"<td>{html.escape(' > '.join(programme.process) or '-')}</td>"
            f"<td>{html.escape(programme.notes or '')}</td>"
            "</tr>"
        )

    return f"""\
<!doctype html>
<html>
  <body>
    <h2>Trackr Spring Weeks Digest - {html.escape(today.strftime(DATE_DISPLAY_FORMAT))}</h2>
    <p>{len(programmes)} programme(s) opened in the last {lookback_days} days.</p>
    <p>Filtered by opening date only; a past closing date does not remove an event.</p>
    <table border="1" cellpadding="6" cellspacing="0">
      <thead>
        <tr>
          <th>Company</th>
          <th>Programme</th>
          <th>Opening</th>
          <th>Closing</th>
          <th>Categories</th>
          <th>Process</th>
          <th>Notes</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
    <p><a href="{html.escape(trackr_url)}">Open Trackr Spring Weeks</a></p>
  </body>
</html>
"""


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def login_if_configured(smtp: smtplib.SMTP | smtplib.SMTP_SSL, username: str | None, password: str | None) -> None:
    if not username or not password:
        return

    try:
        smtp.login(username, password)
    except smtplib.SMTPAuthenticationError as exc:
        raise RuntimeError(
            "SMTP authentication failed. For Gmail, use your full Gmail address as "
            "SMTP_USERNAME and a Google App Password as SMTP_PASSWORD, not your normal "
            "Google account password."
        ) from exc


def send_email(programmes: list[Programme], *, lookback_days: int, today: date, trackr_url: str) -> None:
    smtp_host = required_env("SMTP_HOST")
    smtp_security = os.getenv("SMTP_SECURITY")
    if smtp_security is None:
        smtp_security = "starttls" if parse_bool(os.getenv("SMTP_USE_TLS"), default=True) else "ssl"
    smtp_security = smtp_security.strip().lower()
    default_port = "465" if smtp_security == "ssl" else "587"
    smtp_port = int(os.getenv("SMTP_PORT", default_port))
    smtp_username = os.getenv("SMTP_USERNAME")
    smtp_password = os.getenv("SMTP_PASSWORD")
    sender = required_env("EMAIL_FROM")
    sender_name = os.getenv("EMAIL_FROM_NAME", "Trackr Digest")
    recipients = [item.strip() for item in required_env("EMAIL_TO").split(",") if item.strip()]
    if not recipients:
        raise RuntimeError("EMAIL_TO must contain at least one recipient address.")

    message = EmailMessage()
    message["Subject"] = f"Trackr Spring Weeks: {len(programmes)} opening(s)"
    message["From"] = formataddr((sender_name, sender))
    message["To"] = ", ".join(recipients)
    message.set_content(plain_text_digest(programmes, lookback_days=lookback_days, today=today))
    message.add_alternative(
        html_digest(programmes, lookback_days=lookback_days, today=today, trackr_url=trackr_url),
        subtype="html",
    )

    if smtp_security == "starttls":
        context = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
            smtp.starttls(context=context)
            login_if_configured(smtp, smtp_username, smtp_password)
            smtp.send_message(message)
    elif smtp_security == "ssl":
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as smtp:
            login_if_configured(smtp, smtp_username, smtp_password)
            smtp.send_message(message)
    elif smtp_security == "none":
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp:
            login_if_configured(smtp, smtp_username, smtp_password)
            smtp.send_message(message)
    else:
        raise RuntimeError("SMTP_SECURITY must be one of: starttls, ssl, none.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send a Trackr spring weeks daily digest.")
    parser.add_argument("--dry-run", action="store_true", help="Print the digest instead of sending email.")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=int(os.getenv("LOOKBACK_DAYS", "14")),
        help="Opening-date lookback window. Defaults to LOOKBACK_DAYS or 14.",
    )
    parser.add_argument(
        "--trackr-url",
        default=os.getenv("TRACKR_URL", TRACKR_URL),
        help="Exact Trackr Spring Weeks URL to monitor.",
    )
    parser.add_argument("--season", default=os.getenv("TRACKR_SEASON", "2027"))
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.getenv("HTTP_TIMEOUT_SECONDS", "30")),
    )
    return parser


def main() -> int:
    load_dotenv(Path(".env"))
    args = build_parser().parse_args()

    if args.lookback_days < 0:
        raise RuntimeError("--lookback-days must be 0 or greater.")

    today = date.today()
    url_region, url_industry, url_type = infer_query_from_trackr_url(args.trackr_url)
    region = url_region or "UK"
    industry = url_industry or "Finance"
    programme_type = url_type or "spring-weeks"

    raw_programmes = fetch_programmes(
        region=region,
        industry=industry,
        season=args.season,
        programme_type=programme_type,
        timeout_seconds=args.timeout_seconds,
    )
    recent_programmes = filter_recent_openings(
        raw_programmes,
        today=today,
        lookback_days=args.lookback_days,
        programme_type=programme_type,
        fallback_url=args.trackr_url,
    )

    if not recent_programmes:
        print("No Trackr spring week openings in the lookback window to email.")
        return 0

    if args.dry_run:
        print(plain_text_digest(recent_programmes, lookback_days=args.lookback_days, today=today))
        return 0

    send_email(recent_programmes, lookback_days=args.lookback_days, today=today, trackr_url=args.trackr_url)
    print(f"Sent Trackr digest with {len(recent_programmes)} programme(s).")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
