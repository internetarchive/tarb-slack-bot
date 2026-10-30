# tarb-bot

Mirrors sections of [User talk:InternetArchiveBot](https://meta.wikimedia.org/wiki/User_talk:InternetArchiveBot) into `#tarb-tickets`.

- **New section** → new top-level message (title link, opener, excerpt of the first comment).
- **Maintainer has the latest comment** (Harej, Cyberpower678, GreenC) → `:white_check_mark:` reaction.
- **Anyone else comments after that** → reaction removed, comment posted in the thread.
- **Section renamed** → parent message edited in place, rename noted in the thread. No new post.
- **Section archived/removed** → note in the thread. If it comes back, that's noted too.

## How sections stay stable across renames

Sections are keyed on the DiscussionTools heading `name` (`h-<first author>-<first timestamp>`), which is computed from the opening signature, not the heading text. If even that changes (someone edits the first signature or removes the opening comment), a fallback matches the "new" section to a section that vanished in the same revision by overlapping comment signatures, and re-keys it instead of posting a duplicate.

Comments are keyed on author + timestamp, so they also survive renames.

## Slack app setup

1. Create an app at api.slack.com/apps, add bot scopes `chat:write` and `reactions:write`, install it.
2. Invite the bot to `#tarb-tickets` and grab the channel ID.

## Running

```sh
pip install -r requirements.txt
export SLACK_BOT_TOKEN=xoxb-...
export SLACK_CHANNEL_ID=C0123456789
python tarb_bot.py --once -v     # single poll; good first check
python tarb_bot.py               # long-running, polls every 60s
```

Polling is cheap: each cycle is one `prop=info` request, and the full DiscussionTools parse only runs when the page's revision ID changes.

**First run** records every existing section without posting, so the channel isn't flooded. A section gets its Slack post the first time it sees new activity. Use `--post-existing` on the first run to post all current sections instead.

| Variable | Default |
|---|---|
| `TARB_MAINTAINERS` | `Harej,Cyberpower678,GreenC` |
| `TARB_REACTION` | `white_check_mark` |
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
