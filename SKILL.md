---
name: daily-digest
description: Daily intelligence digest from Airtable, Slack, and Google Drive — personalized via config.json
allowedTools:
  - Read
  - Write
  - Bash
  - Edit
  - Glob
  - Grep
  - "mcp__airtable__airtable_read_tool"
  - "mcp__slack__search_messages"
  - "mcp__slack__get_user_info"
  - "mcp__slack__get_channel_messages"
  - "mcp__slack__post_message"
  - "mcp__slack__message_tool"
  - "mcp__google-drive__search"
  - "mcp__google-drive__read"
  - "mcp__google-drive__docs_v2_read"
  - "mcp__google-drive__docs_tool"
  - "mcp__google-drive__activity"
  - "mcp__24bf0e3d-3405-4dd1-9417-e368ce498008__list_bases"
  - "mcp__24bf0e3d-3405-4dd1-9417-e368ce498008__list_tables_for_base"
  - "mcp__24bf0e3d-3405-4dd1-9417-e368ce498008__list_records_for_table"
  - "mcp__24bf0e3d-3405-4dd1-9417-e368ce498008__search_records"
  - "mcp__24bf0e3d-3405-4dd1-9417-e368ce498008__get_table_schema"
---

You are running a personalized daily intelligence digest. All user-specific configuration is in `config.json` (same directory as this file). You MUST read it first.

---

## Step 0: Load configuration

Read `config.json` from the same directory as this SKILL.md file. This file contains:

- **user**: name, slack_id, role, company, timezone
- **coverage**: description, slack_searches, drive_search_terms, topics, vocabulary, key_people
- **ranking**: financial_impact criteria, personal_relevance criteria
- **sources**: airtable (base_id, fields), slack (workspace_id), google_drive (keychain, OAuth)
- **credentials**: slack_token, claude_config_path

Use these values everywhere below. Do NOT hardcode any user-specific values.

---

## CRITICAL: Tool strategy — guaranteed fallbacks for every source

MCP tools may or may not be available. You MUST use Bash+curl fallbacks when MCPs fail. Do NOT skip any source. Do NOT abort the run because an MCP is missing.

### Airtable
Use MCP `airtable_read_tool` or any available Airtable MCP tool. Query the base specified in `config.sources.airtable.base_id`.

### Slack — search
**Tier 1**: Slack MCP `search_messages` tool.
**Tier 2 (curl fallback)**: Use the token from `config.credentials.slack_token`:
```bash
curl -s -G "https://slack.com/api/search.messages" \
  -H "Authorization: Bearer $SLACK_TOKEN" \
  --data-urlencode "query=SEARCH_QUERY_HERE" \
  --data-urlencode "count=20"
```
Run one curl per search query from `config.coverage.slack_searches`. Parse the JSON response to extract messages.

### Slack — send DM
**Tier 1**: Slack MCP `post_message` tool to `config.user.slack_id`.
**Tier 2 (curl fallback)**: Write message to a temp file, then post using `config.credentials.slack_token`:
```bash
export SLACK_TOKEN="<from config.credentials.slack_token>"
python3 -c "
import json, urllib.request, os
token = os.environ['SLACK_TOKEN']
msg = open('/tmp/digest_message.txt').read()
data = json.dumps({'channel': '<config.user.slack_id>', 'text': msg, 'mrkdwn': True}).encode()
req = urllib.request.Request('https://slack.com/api/chat.postMessage', data=data, headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
print(json.loads(urllib.request.urlopen(req).read()))
"
```

### Google Drive — search
**Tier 1**: Google Drive MCP `search` tool.
**Tier 2 (curl fallback)**: Retrieve OAuth creds from macOS keychain using `config.sources.google_drive` settings, refresh the access token, call the Drive API:
```bash
# Step 1: Get stored OAuth creds from keychain
CREDS_JSON=$(security find-generic-password -s "<config.sources.google_drive.keychain_service>" -a "<config.sources.google_drive.keychain_account>" -w 2>/dev/null)

# Step 2: Extract refresh token
REFRESH_TOKEN=$(echo "$CREDS_JSON" | python3 -c "import json,sys; print(json.load(sys.stdin)['refresh_token'])")

# Step 3: Get a fresh access token
ACCESS_TOKEN=$(curl -s -X POST "https://oauth2.googleapis.com/token" \
  -d "client_id=<config.sources.google_drive.oauth_client_id>" \
  -d "client_secret=<config.sources.google_drive.oauth_client_secret>" \
  -d "refresh_token=$REFRESH_TOKEN" \
  -d "grant_type=refresh_token" | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")

# Step 4: Search for recent files
curl -s -H "Authorization: Bearer $ACCESS_TOKEN" \
  "https://www.googleapis.com/drive/v3/files?q=modifiedTime>%27YESTERDAY_DATE%27+and+fullText+contains+%27SEARCH_TERM%27&fields=files(id,name,modifiedTime,lastModifyingUser/displayName,mimeType)&orderBy=modifiedTime+desc&pageSize=10"
```
Replace YESTERDAY_DATE with yesterday's date in RFC 3339 format. Replace SEARCH_TERM with each term from `config.coverage.drive_search_terms`.

To read a Google Doc's content after finding it:
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

## Step 1: Load previous state

Read `state.json` (same directory as this file) if it exists.

- `run_count`: how many times this task has run
- `snapshot`: previous data

If missing, treat run_count as 0. Mode:
- `run_count < 3` → **BASELINE MODE**: full inventory
- `run_count >= 3` → **DELTA MODE**: changes only

---

## Step 2: Pull data from ALL three sources

### Source A: Airtable
Use the base ID from `config.sources.airtable.base_id`. Get schema, then list all records from all tables. Extract the fields listed in `config.sources.airtable.fields`.

### Source B: Slack — company-wide search
Run each search query from `config.coverage.slack_searches` (MCP or curl, all with `after:yesterday`).

Capture: channel, author, timestamp, message text. Discard bot messages, alerts, emoji-only posts.

### Source C: Google Drive — recently modified docs
Search for docs modified in the last 24 hours matching each term in `config.coverage.drive_search_terms`.

For each doc found, read a content summary. Focus on: title, last modified by, key content/decisions. Skip meeting templates, 1:1 docs, calendar invites.

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

## Step 6: Send Slack DM

Compose message and send to `config.user.slack_id`:
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

Send via Slack MCP or curl fallback. **The DM must be sent.** If both fail, write to `last-digest.md` in this directory.

---

## Step 7: Save state

Write to `state.json` in this directory:
```json
{
  "run_count": [+1],
  "last_run_date": "[YYYY-MM-DD]",
  "source_status": {
    "airtable": "[success/error]",
    "slack": "[success via MCP/success via curl/error]",
    "google_drive": "[success via MCP/success via curl/error]"
  },
  "snapshot": {
    "airtable": { ... },
    "slack_topics": [ ... ],
    "drive_docs": [ ... ]
  }
}
```

---

## Hard constraints

- NEVER skip a source. Use curl fallbacks if MCPs fail.
- ALWAYS send the DM. Use curl fallback or local file as last resort.
- No duplicates across lists.
- Use current system date.
- Read ALL user-specific values from config.json — never hardcode them.
