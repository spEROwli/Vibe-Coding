#!/usr/bin/env python3
"""
pmfarm_gmail_sync.py — Sync applied companies from Gmail to gmail_applied.txt.

One-time setup (creates OAuth token, saved to TOKEN_FILE):
  python3 pmfarm_gmail_sync.py --setup

Subsequent syncs (run before each pmfarm run, or schedule via launchd):
  python3 pmfarm_gmail_sync.py

Requirements (one-time install):
  pip3 install google-api-python-client google-auth-oauthlib

You will need a Google Cloud project with the Gmail API enabled and an
OAuth 2.0 client (Desktop app type). Download the client_secret JSON and
save it as ~/.config/pmfarm/client_secret.json before running --setup.

Docs: https://developers.google.com/gmail/api/quickstart/python
"""

import argparse, json, os, re, sys
from pmfarm import _normalize_company as _normalize

TOKEN_FILE  = os.path.expanduser("~/.config/pmfarm/gmail_token.json")
SECRET_FILE = os.path.expanduser("~/.config/pmfarm/client_secret.json")
OUT_FILE    = "gmail_applied.txt"
SCOPES      = ["https://www.googleapis.com/auth/gmail.readonly"]
DAYS        = 45

# Confirmation subject patterns (case-insensitive OR)
SUBJECT_QUERIES = [
    "thank you for applying",
    "application received",
    "received your application",
    "thanks for applying",
    "we received your application",
]


def _gmail_query() -> str:
    subj_parts = " OR ".join(f'subject:("{s}")' for s in SUBJECT_QUERIES)
    return f"({subj_parts}) newer_than:{DAYS}d"


def _extract_company(subject: str, sender_domain: str) -> str:
    """Best-effort company name from subject line.
    Strips boilerplate like 'Thank you for applying to <Company>'.
    Falls back to sender domain (e.g. 'greenhouse-mail.io' → skip)."""
    patterns = [
        r"(?:thank(?:s)? for (?:applying|your interest) (?:to|at|with)|"
        r"application received[!.]?\s+[-–]?\s*|"
        r"we['’]ve received your application[!.]?\s+[-–]?\s*|"
        r"applying to)\s+(.+?)(?:\s*[!|,\-]|\s*$)",
    ]
    for pat in patterns:
        m = re.search(pat, subject, re.IGNORECASE)
        if m:
            name = m.group(1).strip().rstrip("!.,")
            if name and len(name) < 60:
                return name
    # Fall back: strip ATS relay domains
    if sender_domain in ("us.greenhouse-mail.io", "ashbyhq.com", "lever.co"):
        return ""
    # Use the domain root (e.g. "fanduel.com" → "fanduel")
    parts = sender_domain.split(".")
    if len(parts) >= 2 and parts[-2] not in ("greenhouse", "ashby", "lever"):
        return parts[-2].capitalize()
    return ""



def _load_creds():
    """Load or refresh OAuth credentials. Raises on missing files."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        sys.exit("Missing deps. Run: pip3 install google-api-python-client google-auth-oauthlib")

    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_token(creds)
        return creds
    if not os.path.exists(SECRET_FILE):
        sys.exit(f"Client secret not found: {SECRET_FILE}\n"
                 "Create a Google Cloud project → Gmail API → OAuth client (Desktop) "
                 "→ download JSON → save to that path.")
    flow = InstalledAppFlow.from_client_secrets_file(SECRET_FILE, SCOPES)
    creds = flow.run_local_server(port=0)
    _save_token(creds)
    return creds


def _save_token(creds):
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    print(f"Token saved → {TOKEN_FILE}")


def sync():
    try:
        from googleapiclient.discovery import build
    except ImportError:
        sys.exit("Missing deps. Run: pip3 install google-api-python-client google-auth-oauthlib")

    creds   = _load_creds()
    service = build("gmail", "v1", credentials=creds)

    print(f"Querying Gmail ({DAYS}-day window)…")
    msgs, page_token = [], None
    while True:
        kwargs = {"userId": "me", "q": _gmail_query(), "maxResults": 200}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        msgs.extend(resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    print(f"  {len(msgs)} matching messages found")

    entries: dict[str, tuple] = {}  # normalized → (raw_company, subject, date)
    for m in msgs:
        detail = service.users().messages().get(
            userId="me", id=m["id"], format="metadata",
            metadataHeaders=["Subject", "From", "Date"]
        ).execute()
        headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
        subject = headers.get("Subject", "")
        sender  = headers.get("From", "")
        date    = headers.get("Date", "")[:16].strip()
        # Extract domain from sender "Name <addr@domain.com>"
        m2 = re.search(r"@([\w.\-]+)", sender)
        domain  = m2.group(1).lower() if m2 else ""
        company = _extract_company(subject, domain)
        if not company:
            continue
        norm = _normalize(company)
        if norm and norm not in entries:
            entries[norm] = (company, subject, date)

    if not entries:
        print("  No companies extracted — check subject patterns or OAuth scope.")
        return

    import datetime
    today = datetime.date.today().isoformat()
    lines = [
        f"# gmail_applied.txt — synced from Gmail by pmfarm_gmail_sync.py",
        f"# Last sync: {today} ({len(entries)} companies, {DAYS}-day window)",
        f"# Format: company_name TAB email_subject TAB date",
        f"# Known limit: 'CLEAR' slug is 'clearme' — add to applied.csv manually",
        "",
    ]
    for norm in sorted(entries):
        raw, subj, date = entries[norm]
        lines.append(f"{raw}\t{subj}\t{date}")

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"Wrote {len(entries)} companies → {OUT_FILE}")
    for norm, (raw, subj, _) in sorted(entries.items()):
        print(f"  {raw:<30s} ← {subj[:55]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--setup", action="store_true",
                    help="Run OAuth flow (browser opens once; token saved to disk)")
    args = ap.parse_args()
    if args.setup:
        _load_creds()
        print("Auth complete. Run without --setup to sync.")
    else:
        sync()
