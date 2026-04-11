#!/usr/bin/env python3
"""
Daily Digest — standalone runner.
Fetches data from Airtable, Slack, and Google Drive via APIs,
pipes it to `claude -p` for synthesis, and sends the result as a Slack DM.

No Claude Code permissions needed. Runs via cron.
"""

import json
import os
import subprocess
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
STATE_PATH = SCRIPT_DIR / "state.json"
LOG_PATH = SCRIPT_DIR / "run.log"
FALLBACK_PATH = SCRIPT_DIR / "last-digest.md"


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"run_count": 0}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


# ── Airtable ──────────────────────────────────────────────────────────────

def fetch_airtable(config):
    """Fetch records from Airtable via REST API."""
    base_id = config["sources"]["airtable"]["base_id"]
    fields = config["sources"]["airtable"]["fields"]

    # Get Airtable API key from the claude config or from environment
    api_key = os.environ.get("AIRTABLE_API_KEY", "")
    if not api_key:
        try:
            claude_cfg_path = config["credentials"].get("claude_config_path", "")
            if claude_cfg_path:
                with open(claude_cfg_path) as f:
                    claude_cfg = json.load(f)
                # Search for airtable API key in MCP server configs
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

    log(f"  Fetching Airtable base {base_id}...")

    # List tables
    try:
        req = urllib.request.Request(
            f"https://api.airtable.com/v0/meta/bases/{base_id}/tables",
            headers={"Authorization": f"Bearer {api_key}"}
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
        tables = resp.get("tables", [])
    except Exception as e:
        return {"error": f"Failed to list tables: {e}", "records": []}

    # Build a set of which fields exist in which table
    table_fields = {}
    for table in tables:
        table_fields[table["name"]] = {f["name"] for f in table.get("fields", [])}

    all_records = []
    for table in tables:
        table_name = table["name"]
        log(f"  Reading table: {table_name}")

        # Only request fields that actually exist in this table
        available = table_fields.get(table_name, set())
        valid_fields = [f for f in fields if f in available]

        if valid_fields:
            params = "&".join(
                f"fields[]={urllib.parse.quote(f)}" for f in valid_fields
            )
            url = f"https://api.airtable.com/v0/{base_id}/{urllib.parse.quote(table_name)}?{params}&pageSize=100"
        else:
            # No matching fields — fetch all and let config filter later
            url = f"https://api.airtable.com/v0/{base_id}/{urllib.parse.quote(table_name)}?pageSize=100"

        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
            resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
            records = resp.get("records", [])
            for r in records:
                rec = {"_table": table_name}
                rec.update(r.get("fields", {}))
                all_records.append(rec)
        except Exception as e:
            log(f"  Warning: failed to read {table_name}: {e}")

    log(f"  Airtable: {len(all_records)} records from {len(tables)} tables")
    return {"records": all_records, "tables": [t["name"] for t in tables]}


# ── Slack ─────────────────────────────────────────────────────────────────

def _slack_search(token, workspace_id, queries, label="Slack"):
    """Run a list of Slack search queries and return deduplicated messages."""
    all_messages = []

    for query in queries:
        full_query = f"{query} after:yesterday"
        log(f"  {label} search: {full_query}")

        search_params = {"query": full_query, "count": "20"}
        if workspace_id:
            search_params["team_id"] = workspace_id
        params = urllib.parse.urlencode(search_params)
        url = f"https://slack.com/api/search.messages?{params}"

        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            resp = json.loads(urllib.request.urlopen(req, timeout=30).read())

            if not resp.get("ok"):
                log(f"  Warning: {label} search failed: {resp.get('error', 'unknown')}")
                continue

            matches = resp.get("messages", {}).get("matches", [])
            for m in matches:
                if m.get("bot_id") or m.get("subtype") == "bot_message":
                    continue
                text = m.get("text", "").strip()
                if not text or len(text) < 10:
                    continue
                all_messages.append({
                    "channel": m.get("channel", {}).get("name", "unknown"),
                    "user": m.get("username", "unknown"),
                    "text": text[:500],
                    "ts": m.get("ts", ""),
                    "query": query
                })
        except Exception as e:
            log(f"  Warning: {label} search failed for '{query}': {e}")

    # Deduplicate by timestamp
    seen = set()
    unique = []
    for m in all_messages:
        if m["ts"] not in seen:
            seen.add(m["ts"])
            unique.append(m)

    log(f"  {label}: {len(unique)} unique messages from {len(queries)} searches")
    return unique


def fetch_slack(config):
    """Search Slack for user's coverage area."""
    token = config["credentials"]["slack_token"]
    workspace_id = config["sources"]["slack"].get("workspace_id", "")
    queries = config["coverage"]["slack_searches"]
    return _slack_search(token, workspace_id, queries, "Slack")


def fetch_slack_company_bets(config):
    """Search Slack for company-wide trajectory-changing initiatives."""
    bets = config.get("company_bets", {})
    if not bets or not bets.get("slack_searches"):
        return []
    token = config["credentials"]["slack_token"]
    workspace_id = config["sources"]["slack"].get("workspace_id", "")
    return _slack_search(token, workspace_id, bets["slack_searches"], "Bets")


# ── Google Drive ──────────────────────────────────────────────────────────

def fetch_google_drive(config, override_terms=None, label="Drive"):
    """Search Google Drive via API with OAuth refresh."""
    gd = config["sources"]["google_drive"]
    terms = override_terms or config["coverage"]["drive_search_terms"]

    # Get refresh token from keychain
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", gd["keychain_service"],
             "-a", gd["keychain_account"],
             "-w"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return {"error": "Keychain lookup failed", "docs": []}
        creds = json.loads(result.stdout.strip())
        refresh_token = creds["refresh_token"]
    except Exception as e:
        return {"error": f"Keychain error: {e}", "docs": []}

    # Refresh access token
    try:
        data = urllib.parse.urlencode({
            "client_id": gd["oauth_client_id"],
            "client_secret": gd["oauth_client_secret"],
            "refresh_token": refresh_token,
            "grant_type": "refresh_token"
        }).encode()
        req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        access_token = resp["access_token"]
    except Exception as e:
        return {"error": f"OAuth refresh failed: {e}", "docs": []}

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    all_docs = []

    for term in terms:
        query = f"modifiedTime>'{yesterday}' and fullText contains '{term}'"
        params = urllib.parse.urlencode({
            "q": query,
            "fields": "files(id,name,modifiedTime,lastModifyingUser/displayName,mimeType)",
            "orderBy": "modifiedTime desc",
            "pageSize": "10"
        })
        url = f"https://www.googleapis.com/drive/v3/files?{params}"
        log(f"  {label} search: {term}")

        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
            resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
            files = resp.get("files", [])
            for f in files:
                name = f.get("name", "")
                # Skip meeting templates, 1:1 docs, calendar invites
                if any(skip in name.lower() for skip in ["1:1", "1-1", "meeting notes template", "calendar"]):
                    continue
                all_docs.append({
                    "name": name,
                    "author": f.get("lastModifyingUser", {}).get("displayName", "unknown"),
                    "modified": f.get("modifiedTime", ""),
                    "mime": f.get("mimeType", ""),
                    "id": f.get("id", ""),
                    "search_term": term
                })
        except Exception as e:
            log(f"  Warning: Drive search failed for '{term}': {e}")

    # Deduplicate by doc ID
    seen = set()
    unique = []
    for d in all_docs:
        if d["id"] not in seen:
            seen.add(d["id"])
            unique.append(d)

    log(f"  {label}: {len(unique)} unique docs from {len(terms)} searches")
    return {"docs": unique}


# ── Send Slack DM ─────────────────────────────────────────────────────────

def send_slack_dm(config, message):
    """Send a Slack DM via API."""
    token = config["credentials"]["slack_token"]
    channel = config["user"]["slack_id"]

    data = json.dumps({
        "channel": channel,
        "text": message,
        "mrkdwn": True
    }).encode()

    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
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


# ── Claude synthesis ──────────────────────────────────────────────────────

def synthesize(config, state, airtable_data, slack_data, drive_data, bets_slack_data, bets_drive_data):
    """Pass raw data to claude -p for synthesis."""
    run_count = state.get("run_count", 0)
    mode = "BASELINE" if run_count < 3 else "DELTA"
    today = datetime.now().strftime("%Y-%m-%d")

    prev_snapshot = json.dumps(state.get("snapshot", {}), indent=2) if mode == "DELTA" else "N/A"

    prompt = f"""You are generating a daily intelligence digest for {config['user']['name']}, {config['user']['role']} at {config['user']['company']}.

Today's date: {today}
Run number: {run_count + 1}
Mode: {mode}

## User's coverage area
{config['coverage']['description']}

## User's vocabulary (use these terms)
{', '.join(config['coverage']['vocabulary'])}

## Key people (items involving these people are personally relevant)
{', '.join(config['coverage']['key_people'])}

## Ranking criteria — Financial Impact
{chr(10).join('- ' + c for c in config['ranking']['financial_impact'])}

## Ranking criteria — Personal Relevance
{chr(10).join('- ' + c for c in config['ranking']['personal_relevance'])}

## Ranking criteria — Company Trajectory (big bets, company-changing initiatives)
{chr(10).join('- ' + c for c in config.get('company_bets', {}).get('criteria', []))}

{"## Previous snapshot (for delta comparison)" + chr(10) + prev_snapshot if mode == "DELTA" else ""}

## RAW DATA — Airtable ({len(airtable_data.get('records', []))} records)
{json.dumps(airtable_data, indent=1, default=str)[:15000]}

## RAW DATA — Slack ({len(slack_data)} messages from user's coverage area)
{json.dumps(slack_data, indent=1, default=str)[:12000]}

## RAW DATA — Slack Company Bets ({len(bets_slack_data)} messages from company-wide searches)
{json.dumps(bets_slack_data, indent=1, default=str)[:10000]}

## RAW DATA — Google Drive ({len(drive_data.get('docs', []))} docs from user's coverage area)
{json.dumps(drive_data, indent=1, default=str)[:6000]}

## RAW DATA — Google Drive Company Bets ({len(bets_drive_data.get('docs', []))} docs from company-wide searches)
{json.dumps(bets_drive_data, indent=1, default=str)[:5000]}

## Instructions

Produce the digest in this exact format (Slack mrkdwn):

*📊 Daily Digest — {today}* _(run {run_count + 1} · {mode.lower()})_

*🔴 Top 10: Financial Impact*

[up to 10 items]

---

*🎯 Top 10: On Your Radar*

[up to 10 items]

---

*🚀 Top 5: Company Trajectory*

[up to 5 items — trajectory-changing initiatives and big bets across the ENTIRE company, NOT limited to the user's coverage area. Think: new markets, new product lines, major sales motion shifts, AI/ML bets, strategic partnerships, major org changes, competitive responses. These should be the things that could change the company's trajectory over the next 1-3 years.]

---
_Sources: Airtable {'✅' if not airtable_data.get('error') else '❌'} · Slack {'✅' if slack_data else '❌'} · Drive {'✅' if not drive_data.get('error') else '❌'} · Sent by Claude_

Rules:
- Each item format: [N]. 🔴/🟡/🟢 *[Item name]* _(Airtable/Slack/Drive)_  then 2-3 sentences
- 🔴 = action today, 🟡 = monitor, 🟢 = positive signal
- Lead with "so what" and financial impact, not metadata
- No duplicates across ANY of the three sections
- Company Trajectory items should be DIFFERENT from Financial Impact and On Your Radar — broader, more strategic, company-wide
- If {mode} is DELTA, only include genuine changes vs the previous snapshot
- Output ONLY the formatted digest, nothing else
"""

    log("  Running claude -p for synthesis...")
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--model", "sonnet"],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            log(f"  claude -p failed: {result.stderr[:500]}")
            return None
        return result.stdout.strip()
    except FileNotFoundError:
        # Try full path
        for path in ["/usr/local/bin/claude", str(Path.home() / ".local/bin/claude"), "/opt/homebrew/bin/claude"]:
            try:
                result = subprocess.run(
                    [path, "-p", prompt, "--model", "sonnet"],
                    capture_output=True, text=True, timeout=300
                )
                if result.returncode == 0:
                    return result.stdout.strip()
            except FileNotFoundError:
                continue
        log("  ERROR: claude CLI not found")
        return None
    except subprocess.TimeoutExpired:
        log("  ERROR: claude -p timed out after 5 minutes")
        return None


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    log("=" * 60)
    log("Daily Digest run started")

    config = load_config()
    state = load_state()
    run_count = state.get("run_count", 0)
    log(f"Run #{run_count + 1} | Mode: {'BASELINE' if run_count < 3 else 'DELTA'}")

    # Fetch all data
    log("Fetching Airtable...")
    airtable_data = fetch_airtable(config)
    airtable_ok = "error" not in airtable_data

    log("Fetching Slack (coverage area)...")
    slack_data = fetch_slack(config)
    slack_ok = len(slack_data) > 0

    log("Fetching Slack (company bets)...")
    bets_slack_data = fetch_slack_company_bets(config)

    log("Fetching Google Drive (coverage area)...")
    drive_data = fetch_google_drive(config)
    drive_ok = "error" not in drive_data

    log("Fetching Google Drive (company bets)...")
    bets_drive_terms = config.get("company_bets", {}).get("drive_search_terms", [])
    bets_drive_data = fetch_google_drive(config, override_terms=bets_drive_terms, label="Bets Drive") if bets_drive_terms else {"docs": []}

    # Synthesize with Claude
    digest = synthesize(config, state, airtable_data, slack_data, drive_data, bets_slack_data, bets_drive_data)

    if not digest:
        log("ERROR: Synthesis failed — no digest produced")
        sys.exit(1)

    log(f"Digest generated ({len(digest)} chars)")

    # Send Slack DM
    sent = send_slack_dm(config, digest)

    if not sent:
        log("Slack DM failed — saving to local file")
        with open(FALLBACK_PATH, "w") as f:
            f.write(digest)
        log(f"Saved to {FALLBACK_PATH}")

    # Save state
    new_state = {
        "run_count": run_count + 1,
        "last_run_date": datetime.now().strftime("%Y-%m-%d"),
        "source_status": {
            "airtable": "success" if airtable_ok else "error",
            "slack": "success via curl" if slack_ok else "error",
            "google_drive": "success via curl" if drive_ok else "error"
        },
        "snapshot": {
            "airtable": airtable_data,
            "slack_topics": list(set(m.get("query", "") for m in slack_data)),
            "drive_docs": [{"name": d["name"], "author": d["author"]} for d in drive_data.get("docs", [])]
        }
    }
    save_state(new_state)
    log("State saved")
    log("Daily Digest run complete")
    log("=" * 60)


if __name__ == "__main__":
    main()
