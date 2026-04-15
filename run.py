#!/usr/bin/env python3
"""
Daily Digest — standalone runner.
Fetches data from Airtable, Slack, and Google Drive via APIs (in parallel),
pipes it to `claude -p` for synthesis, and sends the result as a Slack DM.

No Claude Code permissions needed. Runs via cron.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent


def _resolve_paths(config_name="config.json"):
    """Resolve config/state/log paths from a config filename."""
    stem = Path(config_name).stem  # e.g. "config-role" from "config-role.json"
    suffix = stem.replace("config", "").strip("-") if stem != "config" else ""
    return {
        "config": SCRIPT_DIR / config_name,
        "state": SCRIPT_DIR / (f"state-{suffix}.json" if suffix else "state.json"),
        "log": SCRIPT_DIR / (f"run-{suffix}.log" if suffix else "run.log"),
        "fallback": SCRIPT_DIR / (f"last-digest-{suffix}.md" if suffix else "last-digest.md"),
    }


_LOG_PATH = None  # set by main()


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    if _LOG_PATH:
        with open(_LOG_PATH, "a") as f:
            f.write(line + "\n")
    else:
        print(line, flush=True)


def load_config(path):
    with open(path) as f:
        return json.load(f)


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"run_count": 0}


def save_state(state, path):
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


# ── Airtable ──────────────────────────────────────────────────────────────

def fetch_airtable(config):
    """Fetch records from Airtable via REST API."""
    base_id = config["sources"]["airtable"]["base_id"]
    fields = config["sources"]["airtable"]["fields"]

    api_key = os.environ.get("AIRTABLE_API_KEY", "")
    if not api_key:
        try:
            claude_cfg_path = config["credentials"].get("claude_config_path", "")
            if claude_cfg_path:
                with open(claude_cfg_path) as f:
                    claude_cfg = json.load(f)
                for section in [claude_cfg, claude_cfg.get("projects", {}).get(str(Path.home()), {})]:
                    servers = section.get("mcpServers", {})
                    for name, srv in servers.items():
                        if "airtable" in name.lower():
                            api_key = srv.get("env", {}).get("AIRTABLE_API_KEY", "")
                            if api_key:
                                break
                    if api_key:
                        break
        except Exception as e:
            log(f"  Warning: could not read Airtable key from claude config: {e}")

    if not api_key:
        return {"error": "No AIRTABLE_API_KEY found", "records": []}

    log("  Fetching Airtable...")

    try:
        req = urllib.request.Request(
            f"https://api.airtable.com/v0/meta/bases/{base_id}/tables",
            headers={"Authorization": f"Bearer {api_key}"}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
        tables = resp.get("tables", [])
    except Exception as e:
        return {"error": f"Failed to list tables: {e}", "records": []}

    table_fields = {}
    for table in tables:
        table_fields[table["name"]] = {f["name"] for f in table.get("fields", [])}

    def _fetch_table(table):
        table_name = table["name"]
        available = table_fields.get(table_name, set())
        valid_fields = [f for f in fields if f in available]
        if valid_fields:
            params = "&".join(f"fields[]={urllib.parse.quote(f)}" for f in valid_fields)
            url = f"https://api.airtable.com/v0/{base_id}/{urllib.parse.quote(table_name)}?{params}&pageSize=100"
        else:
            url = f"https://api.airtable.com/v0/{base_id}/{urllib.parse.quote(table_name)}?pageSize=100"
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
            resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
            records = resp.get("records", [])
            return [dict(
                _table=table_name,
                _url=f"https://airtable.com/{base_id}/{table.get('id', '')}/{r['id']}",
                **r.get("fields", {})
            ) for r in records]
        except Exception as e:
            log(f"  Warning: failed to read {table_name}: {e}")
            return []

    # Parallel table reads
    all_records = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_table, t): t["name"] for t in tables}
        for future in as_completed(futures):
            all_records.extend(future.result())

    log(f"  Airtable: {len(all_records)} records from {len(tables)} tables")
    return {"records": all_records, "tables": [t["name"] for t in tables]}


# ── Slack ─────────────────────────────────────────────────────────────────

def _slack_search_batch(token, workspace_id, queries, label="Slack"):
    """Run Slack searches in parallel and return deduplicated messages."""

    def _search_one(query):
        full_query = f"{query} after:yesterday"
        search_params = {"query": full_query, "count": "20"}
        if workspace_id:
            search_params["team_id"] = workspace_id
        params = urllib.parse.urlencode(search_params)
        url = f"https://slack.com/api/search.messages?{params}"
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
            if not resp.get("ok"):
                return []
            results = []
            for m in resp.get("messages", {}).get("matches", []):
                if m.get("bot_id") or m.get("subtype") == "bot_message":
                    continue
                text = m.get("text", "").strip()
                if not text or len(text) < 10:
                    continue
                results.append({
                    "channel": m.get("channel", {}).get("name", "unknown"),
                    "user": m.get("username", "unknown"),
                    "text": text[:500],
                    "ts": m.get("ts", ""),
                    "permalink": m.get("permalink", ""),
                    "query": query
                })
            return results
        except Exception:
            return []

    all_messages = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(_search_one, q) for q in queries]
        for future in as_completed(futures):
            all_messages.extend(future.result())

    # Deduplicate by timestamp
    seen = set()
    unique = [m for m in all_messages if m["ts"] not in seen and not seen.add(m["ts"])]
    log(f"  {label}: {len(unique)} unique messages from {len(queries)} searches")
    return unique


def fetch_slack(config):
    token = config["credentials"]["slack_token"]
    workspace_id = config["sources"]["slack"].get("workspace_id", "")
    return _slack_search_batch(token, workspace_id, config["coverage"]["slack_searches"], "Slack")


def fetch_slack_company_bets(config):
    bets = config.get("company_bets", {})
    if not bets or not bets.get("slack_searches"):
        return []
    token = config["credentials"]["slack_token"]
    workspace_id = config["sources"]["slack"].get("workspace_id", "")
    return _slack_search_batch(token, workspace_id, bets["slack_searches"], "Bets")


# ── Google Drive ──────────────────────────────────────────────────────────

def _get_drive_access_token(config):
    """Get a fresh Google Drive access token.
    Reads refresh_token from config first (reliable), falls back to keychain.
    """
    gd = config["sources"]["google_drive"]
    refresh_token = gd.get("refresh_token")

    # Fallback: try keychain if no refresh_token in config
    if not refresh_token:
        try:
            result = subprocess.run(
                ["security", "find-generic-password",
                 "-s", gd["keychain_service"], "-a", gd["keychain_account"], "-w"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                creds = json.loads(result.stdout.strip())
                refresh_token = creds.get("refresh_token")
        except Exception as e:
            log(f"  Warning: Keychain access failed: {e}")

    if not refresh_token:
        log("  Warning: No refresh token available (config or keychain)")
        return None

    try:
        data = urllib.parse.urlencode({
            "client_id": gd["oauth_client_id"],
            "client_secret": gd["oauth_client_secret"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token"
        }).encode()
        req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        return resp["access_token"]
    except Exception as e:
        log(f"  Warning: Drive OAuth failed: {e}")
        return None


def _drive_read_content(access_token, doc):
    """Export a Google Drive doc's text content (truncated)."""
    doc_id = doc["id"]
    mime = doc.get("mime", "")
    try:
        if "spreadsheet" in mime:
            export_mime = "text/csv"
        elif "presentation" in mime:
            export_mime = "text/plain"
        elif "document" in mime:
            export_mime = "text/plain"
        else:
            return ""  # skip binary files
        url = f"https://www.googleapis.com/drive/v3/files/{doc_id}/export?mimeType={urllib.parse.quote(export_mime)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
        content = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", errors="replace")
        return content[:2000]  # first 2000 chars — enough to assess relevance
    except Exception:
        return ""


def _drive_search_batch(access_token, terms, label="Drive"):
    """Search Drive for modified docs, then read their content in parallel."""
    if not access_token:
        return {"error": "No access token", "docs": []}

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")

    def _search_one(term):
        query = f"modifiedTime>'{yesterday}' and fullText contains '{term}'"
        params = urllib.parse.urlencode({
            "q": query,
            "fields": "files(id,name,modifiedTime,lastModifyingUser/displayName,mimeType)",
            "orderBy": "modifiedTime desc",
            "pageSize": "10"
        })
        url = f"https://www.googleapis.com/drive/v3/files?{params}"
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
            resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
            docs = []
            for f in resp.get("files", []):
                name = f.get("name", "")
                if any(skip in name.lower() for skip in ["1:1", "1-1", "meeting notes template", "calendar"]):
                    continue
                doc_id = f.get("id", "")
                mime = f.get("mimeType", "")
                if "spreadsheet" in mime:
                    url = f"https://docs.google.com/spreadsheets/d/{doc_id}"
                elif "presentation" in mime:
                    url = f"https://docs.google.com/presentation/d/{doc_id}"
                elif "document" in mime:
                    url = f"https://docs.google.com/document/d/{doc_id}"
                else:
                    url = f"https://drive.google.com/file/d/{doc_id}"
                docs.append({
                    "name": name,
                    "author": f.get("lastModifyingUser", {}).get("displayName", "unknown"),
                    "modified": f.get("modifiedTime", ""),
                    "mime": mime,
                    "id": doc_id,
                    "url": url,
                    "search_term": term
                })
            return docs
        except Exception:
            return []

    # Phase 1: search for modified docs
    all_docs = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_search_one, t) for t in terms]
        for future in as_completed(futures):
            all_docs.extend(future.result())

    seen = set()
    unique = [d for d in all_docs if d["id"] not in seen and not seen.add(d["id"])]

    # Phase 2: read content of each doc in parallel
    def _read_one(doc):
        doc["content"] = _drive_read_content(access_token, doc)
        return doc

    with ThreadPoolExecutor(max_workers=8) as pool:
        unique = list(pool.map(_read_one, unique))

    docs_with_content = sum(1 for d in unique if d.get("content"))
    log(f"  {label}: {len(unique)} unique docs ({docs_with_content} with content) from {len(terms)} searches")
    return {"docs": unique}


# ── Gemini Meeting Notes ──────────────────────────────────────────────────

def fetch_gemini_notes(access_token, config):
    """Fetch Gemini auto-generated meeting notes from the last 24h."""
    if not access_token:
        return {"docs": []}

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    queries = [
        f"modifiedTime>'{yesterday}' and name contains 'Meeting notes'",
        f"modifiedTime>'{yesterday}' and name contains 'Gemini'",
        f"modifiedTime>'{yesterday}' and name contains 'meeting summary'",
    ]

    def _search_one(q):
        params = urllib.parse.urlencode({
            "q": q,
            "fields": "files(id,name,modifiedTime,lastModifyingUser/displayName,mimeType)",
            "orderBy": "modifiedTime desc",
            "pageSize": "20"
        })
        url = f"https://www.googleapis.com/drive/v3/files?{params}"
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
            resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
            docs = []
            for f in resp.get("files", []):
                doc_id = f.get("id", "")
                mime = f.get("mimeType", "")
                if "document" in mime:
                    doc_url = f"https://docs.google.com/document/d/{doc_id}"
                else:
                    continue  # meeting notes are always docs
                docs.append({
                    "name": f.get("name", ""),
                    "author": f.get("lastModifyingUser", {}).get("displayName", "unknown"),
                    "modified": f.get("modifiedTime", ""),
                    "mime": mime,
                    "id": doc_id,
                    "url": doc_url,
                    "search_term": "gemini_meeting_notes"
                })
            return docs
        except Exception:
            return []

    all_docs = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(_search_one, q) for q in queries]
        for future in as_completed(futures):
            all_docs.extend(future.result())

    seen = set()
    unique = [d for d in all_docs if d["id"] not in seen and not seen.add(d["id"])]

    # Read content in parallel
    def _read(doc):
        doc["content"] = _drive_read_content(access_token, doc)
        return doc

    with ThreadPoolExecutor(max_workers=8) as pool:
        unique = list(pool.map(_read, unique))

    with_content = sum(1 for d in unique if d.get("content"))
    log(f"  Gemini: {len(unique)} meeting notes ({with_content} with content)")
    return {"docs": unique}


# ── Multi-hop Reference Following ────────────────────────────────────────

_DOC_URL_RE = re.compile(
    r'https://docs\.google\.com/(?:document|spreadsheets|presentation)/d/([a-zA-Z0-9_-]+)'
)
_DRIVE_URL_RE = re.compile(
    r'https://drive\.google\.com/(?:file/d|open\?id=)([a-zA-Z0-9_-]+)'
)


def _extract_doc_ids(texts):
    """Extract unique Google Doc/Sheet/Slide IDs from a list of text strings."""
    ids = set()
    for text in texts:
        ids.update(_DOC_URL_RE.findall(text))
        ids.update(_DRIVE_URL_RE.findall(text))
    return ids


def follow_references(access_token, slack_messages, drive_docs, existing_doc_ids):
    """Follow Google Doc URLs found in Slack messages and Drive docs (1 hop)."""
    if not access_token:
        return {"docs": []}

    # Collect all text that might contain URLs
    texts = [m.get("text", "") for m in slack_messages]
    texts += [d.get("content", "") for d in drive_docs if d.get("content")]

    # Extract doc IDs and remove ones we already have
    found_ids = _extract_doc_ids(texts)
    new_ids = found_ids - existing_doc_ids

    if not new_ids:
        log("  Refs: no new document references found")
        return {"docs": []}

    log(f"  Refs: following {len(new_ids)} new document references...")

    def _fetch_one(doc_id):
        try:
            # Get metadata
            params = urllib.parse.urlencode({
                "fields": "id,name,modifiedTime,lastModifyingUser/displayName,mimeType"
            })
            url = f"https://www.googleapis.com/drive/v3/files/{doc_id}?{params}"
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
            meta = json.loads(urllib.request.urlopen(req, timeout=15).read())

            mime = meta.get("mimeType", "")
            name = meta.get("name", "")

            if "spreadsheet" in mime:
                doc_url = f"https://docs.google.com/spreadsheets/d/{doc_id}"
            elif "presentation" in mime:
                doc_url = f"https://docs.google.com/presentation/d/{doc_id}"
            elif "document" in mime:
                doc_url = f"https://docs.google.com/document/d/{doc_id}"
            else:
                return None  # skip binary/unknown

            doc = {
                "name": name,
                "author": meta.get("lastModifyingUser", {}).get("displayName", "unknown"),
                "modified": meta.get("modifiedTime", ""),
                "mime": mime,
                "id": doc_id,
                "url": doc_url,
                "search_term": "reference_follow"
            }
            doc["content"] = _drive_read_content(access_token, doc)
            return doc
        except Exception:
            return None

    docs = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_fetch_one, did) for did in list(new_ids)[:30]]  # cap at 30
        for future in as_completed(futures):
            result = future.result()
            if result:
                docs.append(result)

    with_content = sum(1 for d in docs if d.get("content"))
    log(f"  Refs: {len(docs)} linked docs fetched ({with_content} with content)")
    return {"docs": docs}


# ── Person-based Search Expansion ────────────────────────────────────────

def fetch_people_slack(config):
    """Search Slack for recent messages FROM key people (not just about keywords)."""
    people = config["coverage"].get("key_people_identifiers", [])
    if not people:
        return []

    token = config["credentials"]["slack_token"]
    workspace_id = config["sources"]["slack"].get("workspace_id", "")
    queries = [f"from:@{p['slack']}" for p in people if p.get("slack")]

    return _slack_search_batch(token, workspace_id, queries, "People-Slack")


def fetch_people_drive(access_token, config):
    """Search Drive for docs recently modified by key people."""
    people = config["coverage"].get("key_people_identifiers", [])
    if not people or not access_token:
        return {"docs": []}

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")

    def _search_person(person):
        email = person.get("email", "")
        if not email:
            return []
        # Search for docs this person has edited recently
        query = f"modifiedTime>'{yesterday}' and '{email}' in writers"
        params = urllib.parse.urlencode({
            "q": query,
            "fields": "files(id,name,modifiedTime,lastModifyingUser/displayName,mimeType)",
            "orderBy": "modifiedTime desc",
            "pageSize": "5"
        })
        url = f"https://www.googleapis.com/drive/v3/files?{params}"
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
            resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
            docs = []
            for f in resp.get("files", []):
                name = f.get("name", "")
                if any(skip in name.lower() for skip in ["1:1", "1-1", "meeting notes template", "calendar"]):
                    continue
                doc_id = f.get("id", "")
                mime = f.get("mimeType", "")
                if "spreadsheet" in mime:
                    doc_url = f"https://docs.google.com/spreadsheets/d/{doc_id}"
                elif "presentation" in mime:
                    doc_url = f"https://docs.google.com/presentation/d/{doc_id}"
                elif "document" in mime:
                    doc_url = f"https://docs.google.com/document/d/{doc_id}"
                else:
                    doc_url = f"https://drive.google.com/file/d/{doc_id}"
                docs.append({
                    "name": name,
                    "author": f.get("lastModifyingUser", {}).get("displayName", person["name"]),
                    "modified": f.get("modifiedTime", ""),
                    "mime": mime,
                    "id": doc_id,
                    "url": doc_url,
                    "search_term": f"person:{person['name']}"
                })
            return docs
        except Exception:
            return []

    all_docs = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_search_person, p) for p in people]
        for future in as_completed(futures):
            all_docs.extend(future.result())

    seen = set()
    unique = [d for d in all_docs if d["id"] not in seen and not seen.add(d["id"])]

    # Read content
    def _read(doc):
        doc["content"] = _drive_read_content(access_token, doc)
        return doc

    with ThreadPoolExecutor(max_workers=8) as pool:
        unique = list(pool.map(_read, unique))

    with_content = sum(1 for d in unique if d.get("content"))
    log(f"  People-Drive: {len(unique)} docs ({with_content} with content) from {len(people)} people")
    return {"docs": unique}


# ── Send Slack DM ─────────────────────────────────────────────────────────

def send_slack_dm(config, message):
    token = config["credentials"]["slack_token"]
    channel = config["user"]["slack_id"]
    data = json.dumps({"channel": channel, "text": message, "mrkdwn": True, "unfurl_links": False, "unfurl_media": False}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    )
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
        if resp.get("ok"):
            log("  Slack DM sent successfully")
            return True
        else:
            log(f"  Slack DM failed: {resp.get('error', 'unknown')}")
            return False
    except Exception as e:
        log(f"  Slack DM error: {e}")
        return False


# ── Prompt formatting helpers ─────────────────────────────────────────────

def _format_airtable_for_prompt(airtable_data, max_chars=15000):
    lines = []
    for r in airtable_data.get("records", []):
        name = r.get("Name", r.get("_table", "unnamed"))
        url = r.get("_url", "")
        fields = {k: v for k, v in r.items() if k not in ("_table", "_url")}
        line = f"- {name} | LINK: {url} | {json.dumps(fields, default=str)}"
        lines.append(line)
        if sum(len(l) for l in lines) > max_chars:
            break
    return "\n".join(lines) if lines else "(no records)"


def _format_slack_for_prompt(messages, max_chars=12000):
    lines = []
    for m in messages:
        line = f"- #{m['channel']} @{m['user']}: {m['text'][:300]} | LINK: {m.get('permalink', 'none')}"
        lines.append(line)
        if sum(len(l) for l in lines) > max_chars:
            break
    return "\n".join(lines) if lines else "(no messages)"


def _merge_drive(d1, d2):
    """Merge two drive result dicts, deduplicating by doc ID."""
    all_docs = d1.get("docs", []) + d2.get("docs", [])
    seen = set()
    unique = [d for d in all_docs if d["id"] not in seen and not seen.add(d["id"])]
    return {"docs": unique}


def _format_drive_for_prompt(drive_data, max_chars=20000):
    lines = []
    total = 0
    for d in drive_data.get("docs", []):
        content = d.get("content", "").strip()
        if not content:
            continue  # skip docs we couldn't read — no way to assess relevance
        snippet = content[:1500].replace("\n", " ").strip()
        line = f"- {d['name']} (by {d['author']}, modified {d['modified']}) | LINK: {d.get('url', 'none')}\n  CONTENT: {snippet}"
        total += len(line)
        if total > max_chars:
            break
        lines.append(line)
    return "\n".join(lines) if lines else "(no docs with readable content)"


# ── Claude synthesis ──────────────────────────────────────────────────────

def synthesize(config, state, airtable_data, slack_data, drive_data, bets_slack_data, bets_drive_data):
    run_count = state.get("run_count", 0)
    mode = "BASELINE" if run_count < 3 else "DELTA"
    today = datetime.now().strftime("%Y-%m-%d")
    prev_snapshot = json.dumps(state.get("snapshot", {}), indent=2) if mode == "DELTA" else "N/A"

    digest = config.get("digest", {})
    title = digest.get("title", "Daily Digest")
    section_header = digest.get("section_header", "Top 20")
    framing = digest.get("framing", "")
    includes = digest.get("includes", "")
    excludes = digest.get("excludes", "")

    prompt = f"""You are generating a daily intelligence digest for {config['user']['name']}, {config['user']['role']} at {config['user']['company']}.

Today's date: {today}
Run number: {run_count + 1}
Mode: {mode}

## Company context
{config['user']['company']} is a financial technology company. Business units: Cash App (consumer), Square (seller), Afterpay (BNPL), TIDAL (music), Bitkey (bitcoin).

## Vocabulary (use these terms when relevant)
{', '.join(config['coverage'].get('vocabulary', []))}

## CRITICAL: What this digest IS and IS NOT

{framing}

{f"WHAT BELONGS:" + chr(10) + includes if includes else ""}

{f"EXPLICITLY EXCLUDED:" + chr(10) + excludes if excludes else ""}

{"## Previous snapshot (for delta comparison)" + chr(10) + prev_snapshot if mode == "DELTA" else ""}

## RAW DATA

IMPORTANT:
1. Each raw data item below has a LINK field. When you reference an item in the digest, you MUST use that item's exact LINK value — do NOT swap links between items or fabricate URLs.
2. Google Drive docs include their actual CONTENT. Read the content to determine what the document is about and what specifically was updated. Do NOT include a Drive doc in the digest just because it was recently modified — only include it if the content is substantively relevant.

### Airtable Roadmap ({len(airtable_data.get('records', []))} records)
{_format_airtable_for_prompt(airtable_data)}

### Slack ({len(slack_data)} messages)
{_format_slack_for_prompt(slack_data, max_chars=15000)}

### Google Drive ({len(drive_data.get('docs', []))} docs)
{_format_drive_for_prompt(drive_data, max_chars=25000)}

## Instructions

Produce the digest in this exact format (Slack mrkdwn).

*📊 {title} — {today}* _(run {run_count + 1} · {mode.lower()})_

*{section_header}*

[20 items ranked by impact magnitude.]

---
_Sources: Airtable {'✅' if not airtable_data.get('error') else '❌'} · Slack {'✅' if slack_data else '❌'} · Drive {'✅' if drive_data.get('docs') else '❌'} · Sent by Claude_

Rules:
- Each item format: [N]. 🔴/🟡/🟢 *<Item name>* (<source link>)  then 2-3 sentences with the update.
- 🔴 = action today, 🟡 = monitor, 🟢 = positive signal
- LINKS: Every item MUST include exactly one clickable link. Copy the EXACT URL from the LINK field of the raw data item you are referencing. Do NOT modify, guess, or fabricate URLs. Format as Slack mrkdwn: <URL|Slack>, <URL|Doc>, or <URL|Airtable>. If an item synthesizes multiple sources, use the link from the single most informative source item.
- SPECIFICITY ON CHANGES: Never say "has been updated" or "has changed." Say WHAT specifically changed. Bad: "The roadmap has been updated." Good: "Roadmap moved LTL launch from Q2 to Q3, citing FDIC review delays." Bad: "Loss forecasts were revised." Good: "Loss forecast revised up 20bps to 3.4% on weaker Q1 vintage performance."
- Lead with "so what" and impact, not metadata
- No duplicates
- Do NOT include items just because a document was recently modified — only if the content reveals a substantive signal
- Every item should pass the test: "Is this a meaningful update, not routine noise?" If no, skip it.
- If {mode} is DELTA, only include genuine changes vs the previous snapshot — and state exactly what changed
- Output ONLY the formatted digest, nothing else
"""

    log("  Running claude -p for synthesis...")
    # Use absolute path — LaunchAgent/cron PATH doesn't include /usr/local/bin
    claude_paths = ["/usr/local/bin/claude", "claude",
                    str(Path.home() / ".local/bin/claude"), "/opt/homebrew/bin/claude"]
    for claude_bin in claude_paths:
        try:
            result = subprocess.run(
                [claude_bin, "-p", prompt, "--model", "sonnet"],
                capture_output=True, text=True, timeout=600
            )
            if result.returncode != 0:
                log(f"  claude -p failed ({claude_bin}): {result.stderr[:500]}")
                return None
            return result.stdout.strip()
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            log("  ERROR: claude -p timed out after 10 minutes")
            return None
    log("  ERROR: claude CLI not found in any known path")
    return None


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    global _LOG_PATH

    # Parse --config argument
    config_name = "config.json"
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--config" and i < len(sys.argv):
            config_name = sys.argv[i + 1]
        elif arg.startswith("--config="):
            config_name = arg.split("=", 1)[1]

    paths = _resolve_paths(config_name)
    _LOG_PATH = paths["log"]

    start_time = datetime.now()
    log("=" * 60)
    log(f"Daily Digest run started (config: {config_name})")

    # Wait for network (handles LaunchAgent catch-up after sleep)
    for attempt in range(18):
        try:
            urllib.request.urlopen("https://slack.com/api/api.test", timeout=5)
            break
        except Exception:
            if attempt < 17:
                log(f"  Network not ready, retrying in 10s (attempt {attempt + 1}/18)...")
                time.sleep(10)
            else:
                log("  WARNING: Network still not ready after 3 min, proceeding anyway")

    config = load_config(paths["config"])
    state = load_state(paths["state"])
    run_count = state.get("run_count", 0)
    log(f"Run #{run_count + 1} | Mode: {'BASELINE' if run_count < 3 else 'DELTA'}")

    # Get Drive OAuth token once (shared across ALL Drive fetches)
    log("Refreshing Drive OAuth token...")
    drive_token = _get_drive_access_token(config)

    # ── Phase 1: Fetch all primary sources + new sources in parallel ──
    log("Phase 1: Fetching all sources in parallel...")
    with ThreadPoolExecutor(max_workers=9) as pool:
        # Existing sources
        f_airtable = pool.submit(fetch_airtable, config)
        f_slack = pool.submit(fetch_slack, config)
        f_bets_slack = pool.submit(fetch_slack_company_bets, config)
        drive_terms = config["coverage"]["drive_search_terms"]
        bets_drive_terms = config.get("company_bets", {}).get("drive_search_terms", [])
        f_drive = pool.submit(_drive_search_batch, drive_token, drive_terms, "Drive")
        f_bets_drive = pool.submit(_drive_search_batch, drive_token, bets_drive_terms, "Bets Drive")

        # NEW: Gemini meeting notes
        f_gemini = pool.submit(fetch_gemini_notes, drive_token, config)
        # NEW: Person-based expansion
        f_people_slack = pool.submit(fetch_people_slack, config)
        f_people_drive = pool.submit(fetch_people_drive, drive_token, config)

        airtable_data = f_airtable.result()
        slack_data = f_slack.result()
        bets_slack_data = f_bets_slack.result()
        drive_data = f_drive.result()
        bets_drive_data = f_bets_drive.result()
        gemini_data = f_gemini.result()
        people_slack_data = f_people_slack.result()
        people_drive_data = f_people_drive.result()

    phase1_elapsed = (datetime.now() - start_time).total_seconds()
    log(f"Phase 1 complete in {phase1_elapsed:.1f}s — "
        f"AT:{len(airtable_data.get('records',[]))} "
        f"SL:{len(slack_data)} BetsSL:{len(bets_slack_data)} PeopleSL:{len(people_slack_data)} "
        f"DR:{len(drive_data.get('docs',[]))} BetsDR:{len(bets_drive_data.get('docs',[]))} "
        f"Gemini:{len(gemini_data.get('docs',[]))} PeopleDR:{len(people_drive_data.get('docs',[]))}")

    # ── Phase 2: Multi-hop reference following ──
    # Extract URLs from all Slack messages and Drive doc content, follow them one hop
    log("Phase 2: Following document references...")
    all_slack = slack_data + bets_slack_data + people_slack_data
    all_drive = _merge_drive(_merge_drive(drive_data, bets_drive_data),
                             _merge_drive(gemini_data, people_drive_data))
    existing_doc_ids = {d["id"] for d in all_drive.get("docs", [])}

    refs_data = follow_references(drive_token, all_slack, all_drive.get("docs", []), existing_doc_ids)

    phase2_elapsed = (datetime.now() - start_time).total_seconds()
    log(f"Phase 2 complete in {phase2_elapsed:.1f}s — Refs:{len(refs_data.get('docs',[]))}")

    # ── Merge all data ──
    # Slack: merge all three streams
    merged_slack = all_slack
    # Deduplicate slack by timestamp
    seen_ts = set()
    merged_slack = [m for m in merged_slack if m["ts"] not in seen_ts and not seen_ts.add(m["ts"])]

    # Drive: merge all five streams
    merged_drive = _merge_drive(all_drive, refs_data)

    airtable_ok = "error" not in airtable_data
    slack_ok = len(merged_slack) > 0
    drive_ok = len(merged_drive.get("docs", [])) > 0

    log(f"Merged totals — SL:{len(merged_slack)} DR:{len(merged_drive.get('docs',[]))}")

    # Synthesize with Claude
    digest = synthesize(config, state, airtable_data, merged_slack, merged_drive, [], {"docs": []})

    if not digest:
        log("ERROR: Synthesis failed — no digest produced")
        sys.exit(1)

    synth_elapsed = (datetime.now() - start_time).total_seconds()
    log(f"Digest generated ({len(digest)} chars) in {synth_elapsed:.1f}s total")

    # Send Slack DM
    sent = send_slack_dm(config, digest)

    if not sent:
        log("Slack DM failed — saving to local file")
        with open(paths["fallback"], "w") as f:
            f.write(digest)
        log(f"Saved to {paths['fallback']}")

    # Save state
    new_state = {
        "run_count": run_count + 1,
        "last_run_date": datetime.now().strftime("%Y-%m-%d"),
        "source_status": {
            "airtable": "success" if airtable_ok else "error",
            "slack": "success" if slack_ok else "error",
            "google_drive": "success" if drive_ok else "error"
        },
        "snapshot": {
            "airtable": airtable_data,
            "slack_topics": list(set(m.get("query", "") for m in merged_slack)),
            "drive_docs": [{"name": d["name"], "author": d["author"]} for d in merged_drive.get("docs", [])]
        }
    }
    save_state(new_state, paths["state"])

    total_elapsed = (datetime.now() - start_time).total_seconds()
    log(f"Daily Digest complete in {total_elapsed:.1f}s")
    log("=" * 60)


if __name__ == "__main__":
    main()
