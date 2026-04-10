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

## Quick start

```bash
git clone https://github.com/glynn5221/daily-digest.git ~/daily-digest
cd ~/daily-digest
./setup.sh
```

The setup script walks you through everything interactively:
1. Asks for your name, Slack ID, role, and coverage area
2. Collects your search queries, key people, and vocabulary
3. Asks for credentials (Slack token, Airtable base ID, Google Drive OAuth)
4. Writes your `config.json`
5. Registers the scheduled task with Claude Code

After setup, the digest runs automatically at 9am every weekday.

## Manual setup

If you prefer to configure manually instead of using the setup script:

### 1. Create config.json

```bash
cp config.example.json config.json
```

Edit `config.json` with your values:

| Field | What to put |
|-------|-------------|
| `user.name` | Your name |
| `user.slack_id` | Your Slack member ID (Slack profile → ⋯ → Copy member ID) |
| `user.role` | Your role — guides how Claude ranks relevance |
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

In Claude Code, ask:
> "Create a scheduled task pointing to ~/daily-digest/SKILL.md, cron 0 9 * * 1-5, working directory ~/daily-digest"

### 3. MCP servers

The task uses these MCP servers when available (falls back to curl if not):
- **Airtable MCP** — for reading base data
- **Slack MCP** — for searching messages and sending DMs
- **Google Drive MCP** — for searching and reading docs

If an MCP isn't connected, the task uses direct API calls via `curl` using the credentials in your `config.json`.

## Prerequisites

- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed
- A Slack user token (`xoxp-...`) with search and chat permissions
- An Airtable base you want to track
- Google Drive MCP configured (for OAuth keychain access) or OAuth client credentials for curl fallback

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
