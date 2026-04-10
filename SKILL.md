---
name: daily-digest
description: Daily intelligence digest from Airtable, Slack, and Google Drive — personalized via config.json
allowedTools:
  - Bash
  - "mcp__24bf0e3d-3405-4dd1-9417-e368ce498008__list_bases"
  - "mcp__24bf0e3d-3405-4dd1-9417-e368ce498008__list_tables_for_base"
  - "mcp__24bf0e3d-3405-4dd1-9417-e368ce498008__list_records_for_table"
  - "mcp__24bf0e3d-3405-4dd1-9417-e368ce498008__search_records"
  - "mcp__24bf0e3d-3405-4dd1-9417-e368ce498008__get_table_schema"
---

You are running a personalized daily intelligence digest.

## CRITICAL RULE: Only use Bash and Airtable MCP tools

**Do NOT use Read, Write, Edit, Glob, Grep, or any Slack/Google Drive MCP tools.** These will trigger permission prompts and block the automated run.

Instead:
- Read files → `cat` via Bash
- Write files → `python3` or heredoc via Bash
- Search Slack → `curl` via Bash
- Search Google Drive → `curl` via Bash
- Send Slack DM → `curl` via Bash
- Query Airtable → Airtable MCP tools (always approved)

---

## Step 0: Load configuration

```bash
cat /Users/glynn/daily-digest/config.json
```

This file contains:
- **user**: name, slack_id, role, company, timezone
- **coverage**: description, slack_searches, drive_search_terms, topics, vocabulary, key_people
- **ranking**: financial_impact criteria, personal_relevance criteria
- **sources**: airtable (base_id, fields), slack (workspace_id), google_drive (keychain, OAuth)
- **credentials**: slack_token, claude_config_path

Use these values everywhere below. Do NOT hardcode any user-specific values.

---

## Step 1: Load previous state

```bash
cat /Users/glynn/daily-digest/state.json 2>/dev/null || echo "{}"
```

- `run_count`: how many times this task has run
- `snapshot`: previous data

If missing or empty, treat run_count as 0. Mode:
- `run_count < 3` → **BASELINE MODE**: full inventory
- `run_count >= 3` → **DELTA MODE**: changes only

---

## Step 2: Pull data from ALL three sources

### Source A: Airtable

Use the Airtable MCP tools (`list_bases`, `list_tables_for_base`, `list_records_for_table`, `search_records`, `get_table_schema`). Query the base ID from `config.sources.airtable.base_id`. Get schema, then list all records. Extract the fields listed in `config.sources.airtable.fields`.

### Source B: Slack — company-wide search (curl via Bash)

For each query in `config.coverage.slack_searches`, run:
```bash
SLACK_TOKEN=$(python3 -c "import json; print(json.load(open('/Users/glynn/daily-digest/config.json'))['credentials']['slack_token'])")
curl -s -G "https://slack.com/api/search.messages" \
  -H "Authorization: Bearer $SLACK_TOKEN" \
  --data-urlencode "query=SEARCH_QUERY after:yesterday" \
  --data-urlencode "count=20"
```
Replace SEARCH_QUERY with each entry from `config.coverage.slack_searches`.

Capture: channel, author, timestamp, message text. Discard bot messages, alerts, emoji-only posts.

### Source C: Google Drive — recently modified docs (curl via Bash)

```bash
CONFIG="/Users/glynn/daily-digest/config.json"
GD_SERVICE=$(python3 -c "import json; print(json.load(open('$CONFIG'))['sources']['google_drive']['keychain_service'])")
GD_ACCOUNT=$(python3 -c "import json; print(json.load(open('$CONFIG'))['sources']['google_drive']['keychain_account'])")
GD_CLIENT_ID=$(python3 -c "import json; print(json.load(open('$CONFIG'))['sources']['google_drive']['oauth_client_id'])")
GD_CLIENT_SECRET=$(python3 -c "import json; print(json.load(open('$CONFIG'))['sources']['google_drive']['oauth_client_secret'])")

CREDS_JSON=$(security find-generic-password -s "$GD_SERVICE" -a "$GD_ACCOUNT" -w 2>/dev/null)
REFRESH_TOKEN=$(echo "$CREDS_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['refresh_token'])")

ACCESS_TOKEN=$(curl -s -X POST "https://oauth2.googleapis.com/token" \
  -d "client_id=$GD_CLIENT_ID" \
  -d "client_secret=$GD_CLIENT_SECRET" \
  -d "refresh_token=$REFRESH_TOKEN" \
  -d "grant_type=refresh_token" | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")

curl -s -H "Authorization: Bearer $ACCESS_TOKEN" \
  "https://www.googleapis.com/drive/v3/files?q=modifiedTime>%27YESTERDAY_DATE%27+and+fullText+contains+%27SEARCH_TERM%27&fields=files(id,name,modifiedTime,lastModifyingUser/displayName,mimeType)&orderBy=modifiedTime+desc&pageSize=10"
```

Replace YESTERDAY_DATE with yesterday's date in RFC 3339 format. Replace SEARCH_TERM with each term from `config.coverage.drive_search_terms`.

To read a Google Doc's content:
```bash
curl -s -H "Authorization: Bearer $ACCESS_TOKEN" \
  "https://docs.googleapis.com/v1/documents/DOC_ID" | python3 -c "
import json,sys
doc = json.load(sys.stdin)
for elem in doc.get('body',{}).get('content',[]):
    for para in elem.get('paragraph',{}).get('elements',[]):
        text = para.get('textRun',{}).get('content','')
        if text.strip(): print(text.strip())
" | head -200
```

Skip meeting templates, 1:1 docs, and calendar invites.

---

## Step 3: Synthesize and rank

Use `config.coverage.description` and `config.user.role` to contextualize all ranking decisions.

### BASELINE MODE (run_count < 3)

**List 1 (up to 10) — Highest financial stakes:**
Apply the criteria from `config.ranking.financial_impact`. Prioritize items with quantifiable financial impact.

**List 2 (up to 10) — Most relevant to this user:**
Apply the criteria from `config.ranking.personal_relevance`. Prioritize items involving people from `config.coverage.key_people` and topics from `config.coverage.topics`.

### DELTA MODE (run_count >= 3)
Compare against `state.snapshot`. Only genuine changes:
- Airtable: status/priority/staffing changes, new/removed projects, date slippage
- Slack: new discussions, decisions, blockers, milestones
- Drive: materially updated or new docs

**List 1 — Changes with largest financial impact since yesterday**
**List 2 — Changes most relevant to this user since yesterday**

---

## Step 4: Deduplicate

Build set of List 1 identifiers. Remove any duplicates from List 2. No overlap. Shorten rather than pad.

---

## Step 5: Format the digest

Rules:
- Lead with the "so what", not metadata. Bad: "Project X changed status." Good: "X is now blocked — puts Aug 15 launch at risk, ~$YM revenue."
- Use the user's vocabulary from `config.coverage.vocabulary`.
- Include financial figures where available or inferred: revenue at risk, customer impact, delay estimates, staffing costs.
- If no number: "material", "high-volume", "regulatory-critical".
- Urgency: 🔴 = action today, 🟡 = monitor, 🟢 = positive signal.

Item format:
```
[N]. 🔴/🟡/🟢 *[Item name]* _(Airtable/Slack/Drive)_
[2-3 sentences: what's happening, financial impact, implied action]
```

---

## Step 6: Send Slack DM (curl via Bash)

First write the formatted message to `/tmp/digest_message.txt` via Bash, then send:
```bash
SLACK_TOKEN=$(python3 -c "import json; print(json.load(open('/Users/glynn/daily-digest/config.json'))['credentials']['slack_token'])")
SLACK_ID=$(python3 -c "import json; print(json.load(open('/Users/glynn/daily-digest/config.json'))['user']['slack_id'])")
python3 -c "
import json, urllib.request
token = '$SLACK_TOKEN'
msg = open('/tmp/digest_message.txt').read()
data = json.dumps({'channel': '$SLACK_ID', 'text': msg, 'mrkdwn': True}).encode()
req = urllib.request.Request('https://slack.com/api/chat.postMessage', data=data, headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
resp = json.loads(urllib.request.urlopen(req).read())
print('ok' if resp.get('ok') else f'ERROR: {resp}')
"
```

Message format:
```
*📊 Daily Digest — [Today's Date]* _(run [N] · [baseline/delta])_

*🔴 Top 10: Financial Impact*

[items]

---

*🎯 Top 10: On Your Radar*

[items]

---
_Sources: Airtable ✅ · Slack [✅/❌] · Drive [✅/❌] · Sent by Claude_
```

**The DM must be sent.** If curl fails, save digest via Bash:
```bash
cat > /Users/glynn/daily-digest/last-digest.md << 'EOF'
[full digest text]
EOF
```

---

## Step 7: Save state (via Bash)

```bash
python3 << 'PYEOF'
import json

state = {
    "run_count": NEW_COUNT,
    "last_run_date": "YYYY-MM-DD",
    "source_status": {
        "airtable": "success or error",
        "slack": "success via curl or error",
        "google_drive": "success via curl or error"
    },
    "snapshot": {
        "airtable": {},
        "slack_topics": [],
        "drive_docs": []
    }
}

# Fill in actual data above before running

with open('/Users/glynn/daily-digest/state.json', 'w') as f:
    json.dump(state, f, indent=2)
print('State saved')
PYEOF
```

---

## Hard constraints

- **ONLY use Bash and Airtable MCP tools.** No Read, Write, Edit, Glob, Grep, or other MCP tools.
- NEVER skip a source. Slack and Drive always use curl via Bash.
- ALWAYS send the DM via curl. Write local file via Bash as last resort.
- No duplicates across lists.
- Use current system date.
- Read ALL user-specific values from config.json via Bash — never hardcode them.
