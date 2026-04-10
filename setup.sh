#!/bin/bash
# setup.sh — Interactive setup for daily-digest
# Run once after cloning: ./setup.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG="$SCRIPT_DIR/config.json"
EXAMPLE="$SCRIPT_DIR/config.example.json"

echo ""
echo "═══════════════════════════════════════════════"
echo "  Daily Digest — Setup"
echo "═══════════════════════════════════════════════"
echo ""

# Check prerequisites
if ! command -v claude &>/dev/null; then
  echo "❌ Claude Code CLI not found. Install it first:"
  echo "   https://docs.anthropic.com/en/docs/claude-code"
  exit 1
fi

if [ -f "$CONFIG" ]; then
  echo "⚠️  config.json already exists."
  read -p "Overwrite? (y/N) " -n 1 -r
  echo
  [[ ! $REPLY =~ ^[Yy]$ ]] && echo "Keeping existing config." && SKIP_CONFIG=1
fi

if [ -z "$SKIP_CONFIG" ]; then
  echo "I'll ask you a few questions to build your config.json."
  echo "You can always edit config.json later for fine-tuning."
  echo ""

  # --- User identity ---
  read -p "Your full name: " USER_NAME
  echo ""
  echo "Your Slack member ID (find it: Slack profile → ⋯ → Copy member ID)"
  read -p "Slack ID (e.g. U0XXXXXXXXX): " SLACK_ID
  echo ""
  read -p "Your role (e.g. 'Head of Product for Payments'): " USER_ROLE
  read -p "Your company: " COMPANY
  read -p "Your timezone (e.g. EST, PST, UTC): " TIMEZONE
  echo ""

  # --- Coverage ---
  echo "Now describe your coverage area. This drives what gets searched and how items are ranked."
  echo ""
  read -p "Coverage description (1 sentence, e.g. 'Finance and risk for consumer lending'): " COVERAGE_DESC
  echo ""
  echo "Enter Slack search queries — keyword pairs that capture your domain."
  echo "Enter one per line. Press Enter on a blank line when done."
  echo "Example: lending borrow"
  echo "Example: \"square financial\" SFS"
  SLACK_SEARCHES=()
  while true; do
    read -p "  Search query: " query
    [ -z "$query" ] && break
    SLACK_SEARCHES+=("$query")
  done

  echo ""
  echo "Enter Google Drive search terms (single keywords)."
  echo "Press Enter on a blank line when done."
  DRIVE_TERMS=()
  while true; do
    read -p "  Drive term: " term
    [ -z "$term" ] && break
    DRIVE_TERMS+=("$term")
  done

  echo ""
  echo "Enter your core topics (used for relevance ranking)."
  echo "Press Enter on a blank line when done."
  TOPICS=()
  while true; do
    read -p "  Topic: " topic
    [ -z "$topic" ] && break
    TOPICS+=("$topic")
  done

  echo ""
  echo "Enter vocabulary/acronyms Claude should use in your digest."
  echo "Press Enter on a blank line when done."
  VOCAB=()
  while true; do
    read -p "  Term: " v
    [ -z "$v" ] && break
    VOCAB+=("$v")
  done

  echo ""
  echo "Enter key people whose activity is relevant to you."
  echo "Press Enter on a blank line when done."
  KEY_PEOPLE=()
  while true; do
    read -p "  Name: " person
    [ -z "$person" ] && break
    KEY_PEOPLE+=("$person")
  done

  # --- Sources ---
  echo ""
  echo "── Data Sources ──"
  read -p "Airtable base ID (from URL, e.g. appXXXXXXXXXXXXX): " AIRTABLE_BASE_ID
  read -p "Airtable base name (e.g. 'Product Roadmap'): " AIRTABLE_BASE_NAME
  echo ""
  echo "Airtable fields to extract (one per line, blank to finish):"
  echo "Common: Name, Status, Priority, Expected Completion Date, Notes"
  AIRTABLE_FIELDS=()
  while true; do
    read -p "  Field: " field
    [ -z "$field" ] && break
    AIRTABLE_FIELDS+=("$field")
  done

  echo ""
  read -p "Slack workspace ID (e.g. T0XXXXXXXXX): " WORKSPACE_ID
  echo ""
  read -p "Slack user token (xoxp-...): " SLACK_TOKEN
  echo ""

  echo "Google Drive OAuth (for API fallback when MCP is unavailable):"
  read -p "  Keychain service name [mcp_google_drive]: " GD_SERVICE
  GD_SERVICE=${GD_SERVICE:-mcp_google_drive}
  read -p "  Keychain account name [oauth_token_gdrive]: " GD_ACCOUNT
  GD_ACCOUNT=${GD_ACCOUNT:-oauth_token_gdrive}
  read -p "  OAuth client ID: " GD_CLIENT_ID
  read -p "  OAuth client secret: " GD_CLIENT_SECRET

  # --- Build config.json ---
  python3 -c "
import json, sys

def to_list(items):
    return json.dumps(items)

config = {
    'user': {
        'name': '''$USER_NAME''',
        'slack_id': '''$SLACK_ID''',
        'role': '''$USER_ROLE''',
        'company': '''$COMPANY''',
        'timezone': '''$TIMEZONE'''
    },
    'coverage': {
        'description': '''$COVERAGE_DESC''',
        'slack_searches': json.loads(sys.argv[1]),
        'drive_search_terms': json.loads(sys.argv[2]),
        'topics': json.loads(sys.argv[3]),
        'vocabulary': json.loads(sys.argv[4]),
        'key_people': json.loads(sys.argv[5])
    },
    'ranking': {
        'financial_impact': [
            'P0/P1 projects blocked, unstaffed, or past deadline',
            'Revenue/cost drivers for your coverage area',
            'Regulatory or compliance items',
            'Staffing gaps on critical work',
            'Slack/Drive signals of financial risk or opportunity'
        ],
        'personal_relevance': [
            'Items matching your core topics',
            'Items involving your key_people',
            'Cross-functional decisions affecting your function',
            'Items not already in financial impact list'
        ]
    },
    'sources': {
        'airtable': {
            'base_id': '''$AIRTABLE_BASE_ID''',
            'base_name': '''$AIRTABLE_BASE_NAME''',
            'fields': json.loads(sys.argv[6])
        },
        'slack': {
            'workspace_id': '''$WORKSPACE_ID'''
        },
        'google_drive': {
            'keychain_service': '''$GD_SERVICE''',
            'keychain_account': '''$GD_ACCOUNT''',
            'oauth_client_id': '''$GD_CLIENT_ID''',
            'oauth_client_secret': '''$GD_CLIENT_SECRET'''
        }
    },
    'credentials': {
        'slack_token': '''$SLACK_TOKEN''',
        'claude_config_path': '$(echo ~)/.claude.json'
    }
}

with open('$CONFIG', 'w') as f:
    json.dump(config, f, indent=2)
print('✅ config.json written')
" \
    "$(python3 -c "import json; print(json.dumps([$(printf '"%s",' "${SLACK_SEARCHES[@]}" | sed 's/,$//')))")" \
    "$(python3 -c "import json; print(json.dumps([$(printf '"%s",' "${DRIVE_TERMS[@]}" | sed 's/,$//')))")" \
    "$(python3 -c "import json; print(json.dumps([$(printf '"%s",' "${TOPICS[@]}" | sed 's/,$//')))")" \
    "$(python3 -c "import json; print(json.dumps([$(printf '"%s",' "${VOCAB[@]}" | sed 's/,$//')))")" \
    "$(python3 -c "import json; print(json.dumps([$(printf '"%s",' "${KEY_PEOPLE[@]}" | sed 's/,$//')))")" \
    "$(python3 -c "import json; print(json.dumps([$(printf '"%s",' "${AIRTABLE_FIELDS[@]}" | sed 's/,$//')])" )"
fi

# --- Register scheduled task ---
echo ""
echo "═══════════════════════════════════════════════"
echo "  Registering scheduled task with Claude Code"
echo "═══════════════════════════════════════════════"
echo ""

claude -p "Create a scheduled task with these exact settings:
- Task ID: daily-digest
- Cron expression: 0 9 * * 1-5
- SKILL.md path: $SCRIPT_DIR/SKILL.md
- Working directory: $SCRIPT_DIR
- Enable it immediately
Use the mcp__scheduled-tasks__create_scheduled_task tool." \
  --allowedTools "mcp__scheduled-tasks__create_scheduled_task" \
  2>/dev/null

echo ""
echo "✅ Scheduled task registered — runs weekdays at 9am."
echo ""
echo "To trigger the first run now, open Claude Code and say:"
echo "  'Run my daily-digest scheduled task now'"
echo ""
echo "To edit your config later:  $CONFIG"
echo ""
