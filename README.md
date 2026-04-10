# Daily Digest

A Claude Code scheduled task that scrapes Airtable, Slack, and Google Drive every weekday morning and sends you a personalized Slack DM with:

- **Top 10: Financial Impact** — highest-stakes updates across your coverage area
- **Top 10: On Your Radar** — changes most relevant to your role, team, and key people

Each item includes urgency indicators (🔴/🟡/🟢), financial impact where available, and source attribution.

## How it works

1. **Airtable** — Pulls project status, priorities, staffing, and deadlines from your configured base
2. **Slack** — Searches public channels for your configured keywords (e.g. team names, product areas, regulatory topics)
3. **Google Drive** — Finds docs modified in the last 24 hours matching your search terms
4. **Synthesis** — Ranks and deduplicates findings into two non-overlapping lists
5. **Delivery** — Sends a formatted Slack DM to you

The first 3 runs build a baseline inventory. After that, it switches to delta mode — surfacing only what changed since yesterday.

## Setup

### 1. Clone and configure

```bash
git clone <repo-url> ~/daily-digest
cd ~/daily-digest
cp config.example.json config.json
```

Edit `config.json` with your values:

| Field | What to put |
|-------|-------------|
| `user.name` | Your name |
| `user.slack_id` | Your Slack member ID (find it in your Slack profile → three dots → "Copy member ID") |
| `user.role` | Your role — this guides how Claude ranks relevance |
| `coverage.slack_searches` | Keyword pairs Claude searches Slack for (tailored to your domain) |
| `coverage.drive_search_terms` | Keywords for Google Drive doc search |
| `coverage.topics` | Your core topics for relevance filtering |
| `coverage.vocabulary` | Acronyms and jargon Claude should use in the digest |
| `coverage.key_people` | Names of people whose activity is personally relevant to you |
| `ranking.financial_impact` | Criteria for what counts as high financial stakes |
| `ranking.personal_relevance` | Criteria for what counts as personally relevant |
| `sources.airtable.base_id` | Your Airtable base ID (from the base URL) |
| `sources.airtable.fields` | Which Airtable fields to extract |
| `sources.slack.workspace_id` | Your Slack workspace ID |
| `sources.google_drive.*` | OAuth keychain and client credentials for Drive API fallback |
| `credentials.slack_token` | Your Slack user token (`xoxp-...`) |

### 2. Register the scheduled task

In Claude Code, create a scheduled task pointing to `SKILL.md`:

```
Task ID: daily-digest
Cron: 0 9 * * 1-5   (9am weekdays, your local time)
File: /path/to/daily-digest/SKILL.md
CWD: /path/to/daily-digest
```

### 3. MCP servers

The task uses these MCP servers when available (falls back to curl if not):
- **Airtable MCP** — for reading base data
- **Slack MCP** — for searching messages and sending DMs
- **Google Drive MCP** — for searching and reading docs

If an MCP isn't connected, the task uses direct API calls via `curl` using the credentials in your `config.json`.

## Files

| File | Committed | Purpose |
|------|-----------|---------|
| `SKILL.md` | Yes | The Claude prompt — defines the full workflow |
| `config.example.json` | Yes | Template config with placeholder values |
| `config.json` | No (gitignored) | Your personalized config with real credentials |
| `state.json` | No (gitignored) | Runtime state — tracks run count, last snapshot for delta mode |
| `last-digest.md` | No | Fallback output if Slack DM fails |

## Customization

The digest is fully driven by `config.json`. To tailor it for a different role:

- **Change coverage area**: Update `slack_searches`, `drive_search_terms`, and `topics` to match your domain
- **Change ranking criteria**: Edit `ranking.financial_impact` and `ranking.personal_relevance`
- **Change vocabulary**: Update `vocabulary` with your team's acronyms
- **Change key people**: Update `key_people` with the names you want to track
- **Change data sources**: Update `sources.airtable.base_id` and `fields` to point to your team's Airtable base
