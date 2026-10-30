# tarb-bot

Mirrors sections of [User talk:InternetArchiveBot](https://meta.wikimedia.org/wiki/User_talk:InternetArchiveBot) into `#tarb-tickets`.

- **New section** → new top-level message (title link, opener, excerpt of the first comment).
- **Maintainer has the latest comment** (Harej, Cyberpower678, GreenC) → `:white_check_mark:` reaction.
- **Anyone else comments after a maintainer's response** → `:white_check_mark:` becomes `:point_up:`, the comment is posted in the thread, and the maintainer who responded is pinged. Further follow-ups are posted without a second ping.
- **A maintainer replies again** → `:point_up:` goes back to `:white_check_mark:`.
- **Comments before any maintainer has responded** → posted in the thread, no reaction.
- **Section renamed** → parent message edited in place, rename noted in the thread. No new post.
- **Section archived/removed** → note in the thread. If it comes back, that's noted too.

## How sections stay stable across renames

Sections are keyed on the DiscussionTools heading `name` (`h-<first author>-<first timestamp>`), which is computed from the opening signature, not the heading text. If even that changes (someone edits the first signature or removes the opening comment), a fallback matches the "new" section to a section that vanished in the same revision by overlapping comment signatures, and re-keys it instead of posting a duplicate.

Comments are keyed on author + timestamp, so they also survive renames.

## Slack app setup

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**. Name it, and pick the workspace that has `#tarb-tickets`. The token is tied to this workspace.
2. **OAuth & Permissions** → **Bot Token Scopes** → add `chat:write` and `reactions:write`.
3. **Install to Workspace** (a workspace admin may need to approve). Copy the **Bot User OAuth Token** (`xoxb-…`).
4. In Slack, open `#tarb-tickets`, type `/invite @your-app-name`.
5. Channel ID: right-click the channel → **View channel details**; the ID (`C…`) is at the bottom.
6. Slack member IDs for the maintainers (for pings): see below.
7. Optional workspace ID (`T…`): the bot logs it on startup, so the easiest way is to run it once and copy it from the log.

## Mapping wiki usernames to Slack users

Pings use Slack **member IDs** (`U…`), not display names or usernames, because those can change and aren't unique. To get one: click the person's name or avatar in Slack → **View full profile** → the **⋮** (More) button → **Copy member ID**.

Then list them in `.env`, keyed by wiki username:

```
TARB_SLACK_USERS=Harej=U0123ABCD,Cyberpower678=U0456EFGH,GreenC=U0789IJKL
```

A maintainer with no mapping is named in bold instead of pinged.

## Running

```sh
pip install -r requirements.txt
cp .env.example .env             # then fill in the token and channel ID
python tarb_bot.py --once -v     # single poll; good first check
python tarb_bot.py               # long-running, polls every 60s
```

Settings are read from `.env` (next to the script or in the working directory); real environment variables take precedence. `.env` is git-ignored.

On startup the bot logs which workspace and bot user the token belongs to. If `SLACK_TEAM_ID` is set and doesn't match, it refuses to run.

Polling is cheap: each cycle is one `prop=info` request, and the full DiscussionTools parse only runs when the page's revision ID changes.

**First run** records every existing section without posting, so the channel isn't flooded. A section gets its Slack post the first time it sees new activity. Use `--post-existing` on the first run to post all current sections instead.

| Variable | Default |
|---|---|
| `SLACK_TEAM_ID` | unset (no workspace check) |
| `TARB_MAINTAINERS` | `Harej,Cyberpower678,GreenC` |
| `TARB_REACTION` | `white_check_mark` |
| `TARB_FOLLOWUP_REACTION` | `point_up` |
| `TARB_SLACK_USERS` | unset (maintainers named, not pinged) |
| `TARB_POLL_INTERVAL` | `60` (seconds) |
| `TARB_DB_PATH` | `tarb_bot.sqlite3` |
| `TARB_PAGE` | `User talk:InternetArchiveBot` |
| `TARB_API_URL` | `https://meta.wikimedia.org/w/api.php` |
| `TARB_USER_AGENT` | set this to include a contact address, per Wikimedia's User-Agent policy |

## Notes

- Only signed comments count; DiscussionTools can't attribute unsigned ones.
- Maintainer comments trigger the reaction but aren't copied into the thread.
- If someone removes the reaction by hand in Slack, the bot doesn't notice until the state next flips.

Tests: `python -m unittest test_tarb_bot`
