#!/usr/bin/env python3
"""
tarb-bot: mirror sections of User_talk:InternetArchiveBot into a Slack channel.

- One top-level Slack message per talk page section.
- When a maintainer (Harej, Cyberpower678, GreenC by default) has the latest
  comment in a section, the message gets the :white_check_mark: reaction.
- When anyone else comments after a maintainer's response, the reaction becomes
  :point_up:, the comment is posted into the thread, and the maintainer who
  responded is pinged. Once a maintainer replies, it goes back to
  :white_check_mark:.
- Other comments are posted into the thread as they arrive.

Section identity is rename-proof: sections are keyed on the DiscussionTools
heading "name" (h-<first author>-<first timestamp>), which does not depend on
the heading text. A comment-overlap fallback catches the rare case where that
name changes too (e.g. the opening signature is edited).
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Iterable, Optional
from urllib.parse import quote

import requests
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

log = logging.getLogger("tarb-bot")

DEFAULT_MAINTAINERS = "Harej,Cyberpower678,GreenC"

# Section states (stored in sections.completed for backward compatibility)
OPEN, DONE, FOLLOWUP = 0, 1, 2


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

@dataclass
class Config:
    slack_token: str
    slack_channel: str
    maintainers: frozenset
    reaction: str = "white_check_mark"
    followup_reaction: str = "point_up"
    slack_users: dict = field(default_factory=dict)  # normalised wiki username -> Slack member ID
    api_url: str = "https://meta.wikimedia.org/w/api.php"
    page: str = "User talk:InternetArchiveBot"
    db_path: str = "tarb_bot.sqlite3"
    poll_interval: int = 60
    user_agent: str = "tarb-bot/1.0 (Slack mirror of User talk:InternetArchiveBot)"
    slack_team_id: Optional[str] = None  # if set, refuse to run against any other workspace

    @property
    def wiki_base(self) -> str:
        # https://meta.wikimedia.org/w/api.php -> https://meta.wikimedia.org/wiki/
        return re.sub(r"/w/api\.php$", "/wiki/", self.api_url)

    @classmethod
    def from_env(cls) -> "Config":
        missing = [k for k in ("SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID") if not os.environ.get(k)]
        if missing:
            sys.exit(f"Missing required environment variables: {', '.join(missing)}")
        maint = os.environ.get("TARB_MAINTAINERS", DEFAULT_MAINTAINERS)
        slack_users = {}
        for pair in os.environ.get("TARB_SLACK_USERS", "").split(","):
            if not pair.strip():
                continue
            wiki_user, sep, member_id = pair.partition("=")
            member_id = member_id.strip()
            if not sep or not re.fullmatch(r"[UW][A-Z0-9]+", member_id):
                sys.exit(f"TARB_SLACK_USERS entry {pair.strip()!r} should look like WikiName=U0123ABCD "
                         "(a Slack member ID, not a display name)")
            slack_users[norm_user(wiki_user)] = member_id
        return cls(
            slack_token=os.environ["SLACK_BOT_TOKEN"],
            slack_channel=os.environ["SLACK_CHANNEL_ID"],
            maintainers=frozenset(norm_user(u) for u in maint.split(",") if u.strip()),
            reaction=os.environ.get("TARB_REACTION", "white_check_mark").strip(":"),
            followup_reaction=os.environ.get("TARB_FOLLOWUP_REACTION", "point_up").strip(":"),
            slack_users=slack_users,
            api_url=os.environ.get("TARB_API_URL", cls.api_url),
            page=os.environ.get("TARB_PAGE", cls.page),
            db_path=os.environ.get("TARB_DB_PATH", cls.db_path),
            poll_interval=int(os.environ.get("TARB_POLL_INTERVAL", cls.poll_interval)),
            user_agent=os.environ.get("TARB_USER_AGENT", cls.user_agent),
            slack_team_id=os.environ.get("SLACK_TEAM_ID") or None,
        )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def norm_user(name: str) -> str:
    """MediaWiki username normalisation: underscores -> spaces, ucfirst."""
    name = name.replace("_", " ").strip()
    return name[:1].upper() + name[1:]


def norm_ts(ts: str) -> str:
    """DiscussionTools emits YYYYMMDDHHMMSS (newer) or ISO 8601 (older). Normalise to the former."""
    if re.fullmatch(r"\d{14}", ts or ""):
        return ts
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y%m%d%H%M%S")
    except (ValueError, AttributeError):
        return ts or ""


def human_ts(ts: str) -> str:
    try:
        return datetime.strptime(ts, "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return ts


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "br", "li", "div", "dd", "dt", "pre", "tr", "h2", "h3", "h4"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html or "")
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in "".join(p.parts).split("\n")]
    return "\n".join(ln for ln in lines if ln)


def excerpt(text: str, limit: int = 500) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def slack_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def blockquote(text: str) -> str:
    return "\n".join("> " + slack_escape(ln) for ln in (text or "(no text)").split("\n"))


# --------------------------------------------------------------------------
# Talk page model + parsing
# --------------------------------------------------------------------------

@dataclass
class Comment:
    key: str      # rename-proof: "<author>|<timestamp>[#n]"
    author: str
    ts: str
    id: str       # DiscussionTools id, used only for permalinks
    text: str


@dataclass
class Section:
    key: str
    title: str
    comments: list[Comment] = field(default_factory=list)  # sorted by timestamp

    @property
    def first(self) -> Comment:
        return self.comments[0]

    @property
    def latest(self) -> Comment:
        return self.comments[-1]


def _walk_comments(items: Iterable[dict], out: list[dict]) -> None:
    for item in items or []:
        if item.get("type") == "comment":
            out.append(item)
        # Recurse into replies of comments and of level-3+ subheadings alike.
        _walk_comments(item.get("replies") or [], out)


def parse_sections(thread_items: list[dict]) -> list[Section]:
    sections: list[Section] = []
    seen_keys: dict[str, int] = {}
    for item in thread_items:
        if item.get("type") != "heading" or item.get("placeholderHeading"):
            continue
        raw: list[dict] = []
        _walk_comments(item.get("replies") or [], raw)
        if not raw:
            continue  # no signed comment yet; pick it up once someone signs

        comments: list[Comment] = []
        dupes: dict[str, int] = {}
        for c in raw:
            author = norm_user(c.get("author", ""))
            ts = norm_ts(c.get("timestamp", ""))
            base = f"{author}|{ts}"
            n = dupes.get(base, 0)
            dupes[base] = n + 1
            comments.append(Comment(
                key=base if n == 0 else f"{base}#{n}",
                author=author, ts=ts, id=c.get("id", ""),
                text=html_to_text(c.get("html", "")),
            ))
        comments.sort(key=lambda c: c.ts)  # stable: tree order breaks ties

        key = item.get("name") or ""
        if key in ("", "h-"):
            key = f"h-{comments[0].author.replace(' ', '_')}-{comments[0].ts}"
        n = seen_keys.get(key, 0)
        seen_keys[key] = n + 1
        if n:
            key = f"{key}#{n}"

        title = (item.get("linkableTitle") or item.get("text")
                 or html_to_text(item.get("html", "")) or "(untitled)")
        sections.append(Section(key=key, title=title.strip(), comments=comments))
    return sections


# --------------------------------------------------------------------------
# Wiki client
# --------------------------------------------------------------------------

class WikiClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.http = requests.Session()
        self.http.headers["User-Agent"] = cfg.user_agent

    def _get(self, **params) -> dict:
        params.update(format="json", formatversion="2")
        r = self.http.get(self.cfg.api_url, params=params, timeout=60)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"MediaWiki API error: {data['error']}")
        return data

    def latest_revid(self) -> int:
        page = self._get(action="query", prop="info", titles=self.cfg.page)["query"]["pages"][0]
        if page.get("missing"):
            raise RuntimeError(f"Page does not exist: {self.cfg.page}")
        return int(page["lastrevid"])

    def sections(self, revid: int) -> list[Section]:
        data = self._get(
            action="discussiontoolspageinfo", page=self.cfg.page, oldid=revid,
            prop="threaditemshtml", threaditemsflags="excludesignatures",
        )
        return parse_sections(data["discussiontoolspageinfo"]["threaditemshtml"])


# --------------------------------------------------------------------------
# State store
# --------------------------------------------------------------------------

class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sections (
                key        TEXT PRIMARY KEY,
                title      TEXT NOT NULL,
                slack_ts   TEXT,
                completed  INTEGER NOT NULL DEFAULT 0,
                present    INTEGER NOT NULL DEFAULT 1,
                first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS comments (
                section_key TEXT NOT NULL,
                comment_key TEXT NOT NULL,
                author      TEXT NOT NULL,
                ts          TEXT NOT NULL,
                PRIMARY KEY (section_key, comment_key)
            );
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        """)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(sections)")}
        if "responder" not in cols:  # added in v1.1
            with self.db:
                self.db.execute("ALTER TABLE sections ADD COLUMN responder TEXT")

    def get_meta(self, k: str) -> Optional[str]:
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row["v"] if row else None

    def set_meta(self, k: str, v) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, str(v)))

    def sections(self) -> dict[str, sqlite3.Row]:
        return {r["key"]: r for r in self.db.execute("SELECT * FROM sections")}

    def section(self, key: str) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM sections WHERE key=?", (key,)).fetchone()

    def insert_section(self, key: str, title: str, slack_ts: Optional[str]) -> None:
        with self.db:
            self.db.execute("INSERT INTO sections (key, title, slack_ts) VALUES (?, ?, ?)",
                            (key, title, slack_ts))

    def update_section(self, key: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.db:
            self.db.execute(f"UPDATE sections SET {cols} WHERE key=?", (*fields.values(), key))

    def rekey(self, old: str, new: str) -> None:
        with self.db:
            self.db.execute("UPDATE sections SET key=? WHERE key=?", (new, old))
            self.db.execute("UPDATE comments SET section_key=? WHERE section_key=?", (new, old))

    def comment_keys(self, section_key: str) -> set[str]:
        return {r["comment_key"] for r in
                self.db.execute("SELECT comment_key FROM comments WHERE section_key=?", (section_key,))}

    def add_comment(self, section_key: str, c: Comment) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO comments (section_key, comment_key, author, ts) VALUES (?, ?, ?, ?)",
                (section_key, c.key, c.author, c.ts))


# --------------------------------------------------------------------------
# Slack wrapper
# --------------------------------------------------------------------------

class Slack:
    def __init__(self, client: WebClient, channel: str):
        self.client = client
        self.channel = channel

    def post(self, text: str, thread_ts: Optional[str] = None) -> str:
        resp = self.client.chat_postMessage(
            channel=self.channel, text=text, thread_ts=thread_ts,
            unfurl_links=False, unfurl_media=False)
        return resp["ts"]

    def update(self, ts: str, text: str) -> None:
        self.client.chat_update(channel=self.channel, ts=ts, text=text)

    def set_reaction(self, ts: str, name: str, on: bool) -> None:
        try:
            if on:
                self.client.reactions_add(channel=self.channel, timestamp=ts, name=name)
            else:
                self.client.reactions_remove(channel=self.channel, timestamp=ts, name=name)
        except SlackApiError as e:
            # Already in the desired state (e.g. someone toggled it by hand).
            if e.response.get("error") not in ("already_reacted", "no_reaction"):
                raise


# --------------------------------------------------------------------------
# Bot logic
# --------------------------------------------------------------------------

class TarbBot:
    def __init__(self, cfg: Config, wiki, slack, store: Store):
        self.cfg, self.wiki, self.slack, self.store = cfg, wiki, slack, store

    # --- formatting ---

    def _section_url(self, title: str) -> str:
        page = quote(self.cfg.page.replace(" ", "_"), safe=":/")
        anchor = quote(title.replace(" ", "_"), safe="!$'()*,/:;@~")
        return f"{self.cfg.wiki_base}{page}#{anchor}"

    def _comment_url(self, c: Comment) -> str:
        if c.id:
            return f"{self.cfg.wiki_base}Special:GoToComment/{quote(c.id, safe='')}"
        return f"{self.cfg.wiki_base}{quote(self.cfg.page.replace(' ', '_'), safe=':/')}"

    def _parent_text(self, s: Section) -> str:
        return (f"*<{self._section_url(s.title)}|{slack_escape(s.title)}>*\n"
                f"Opened by *{slack_escape(s.first.author)}* · {human_ts(s.first.ts)}\n"
                f"{blockquote(excerpt(s.first.text))}")

    def _reply_text(self, c: Comment) -> str:
        return (f"New comment from *{slack_escape(c.author)}* · {human_ts(c.ts)} · "
                f"<{self._comment_url(c)}|view on wiki>\n{blockquote(excerpt(c.text))}")

    def is_maintainer(self, user: str) -> bool:
        return norm_user(user) in self.cfg.maintainers

    def mention(self, wiki_user: str) -> str:
        member_id = self.cfg.slack_users.get(norm_user(wiki_user))
        return f"<@{member_id}>" if member_id else f"*{slack_escape(wiki_user)}*"

    def desired_state(self, s: Section) -> tuple[int, Optional[str]]:
        """Derive the state purely from the page, so it self-corrects.
        Returns (state, maintainer whose response it concerns)."""
        responses = [c for c in s.comments if self.is_maintainer(c.author)]
        if not responses:
            return OPEN, None
        if self.is_maintainer(s.latest.author):
            return DONE, s.latest.author
        return FOLLOWUP, responses[-1].author

    def _swap_reactions(self, ts: str, old: int, new: int) -> None:
        names = {DONE: self.cfg.reaction, FOLLOWUP: self.cfg.followup_reaction}
        if old == new:
            return
        if old in names:
            self.slack.set_reaction(ts, names[old], False)
        if new in names:
            self.slack.set_reaction(ts, names[new], True)

    # --- main entry points ---

    def run_once(self, force: bool = False, post_existing: bool = False) -> None:
        revid = self.wiki.latest_revid()
        if not force and self.store.get_meta("last_revid") == str(revid):
            log.debug("No new revisions (still %s)", revid)
            return
        log.info("Processing revision %s", revid)
        ok = self.sync(self.wiki.sections(revid), post_existing=post_existing)
        if ok:
            self.store.set_meta("last_revid", revid)

    def sync(self, sections: list[Section], post_existing: bool = False) -> bool:
        """Reconcile Slack with the current talk page. Returns False if any section failed."""
        if not self.store.get_meta("initialized"):
            if not post_existing:
                self._seed_quietly(sections)
                self.store.set_meta("initialized", 1)
                return True
            self.store.set_meta("initialized", 1)

        tracked = self.store.sections()
        current = {s.key for s in sections}
        vanished = [k for k, r in tracked.items() if r["present"] and k not in current]

        # Fallback rename handling: a section whose DT name changed (e.g. the
        # opening signature was edited) is matched to a vanished one by comments.
        for s in sections:
            if s.key not in tracked and vanished:
                old = self._match_vanished(s, vanished)
                if old:
                    log.info("Re-keying section %s -> %s", old, s.key)
                    self.store.rekey(old, s.key)
                    vanished.remove(old)

        ok = True
        for s in sections:
            try:
                self._sync_section(s)
            except Exception:
                log.exception("Failed to sync section %r", s.title)
                ok = False

        for key in vanished:
            row = self.store.section(key)
            self.store.update_section(key, present=0)
            if row and row["slack_ts"]:
                try:
                    self.slack.post("This section is no longer on the talk page (probably archived).",
                                    thread_ts=row["slack_ts"])
                except Exception:
                    log.exception("Failed to post archive notice for %s", key)
        return ok

    def _seed_quietly(self, sections: list[Section]) -> None:
        """First run: record what's already there without flooding the channel.
        Slack posts for these sections are created lazily on their next activity."""
        log.info("First run: recording %d existing sections without posting", len(sections))
        for s in sections:
            self.store.insert_section(s.key, s.title, None)
            for c in s.comments:
                self.store.add_comment(s.key, c)
            state, responder = self.desired_state(s)
            self.store.update_section(s.key, completed=state, responder=responder)

    def _match_vanished(self, s: Section, vanished: list[str]) -> Optional[str]:
        new_keys = {c.key for c in s.comments}
        best, best_overlap = None, 0
        for old in vanished:
            stored = self.store.comment_keys(old)
            overlap = len(stored & new_keys)
            if stored and overlap >= max(1, (len(stored) + 1) // 2) and overlap > best_overlap:
                best, best_overlap = old, overlap
        return best

    def _sync_section(self, s: Section) -> None:
        row = self.store.section(s.key)
        shown_in_parent: set[str] = set()

        if row is None:
            ts = self.slack.post(self._parent_text(s))
            self.store.insert_section(s.key, s.title, ts)
            shown_in_parent.add(s.first.key)
            row = self.store.section(s.key)
            log.info("New section %r", s.title)
        else:
            if not row["present"]:
                self.store.update_section(s.key, present=1)
                if row["slack_ts"]:
                    self.slack.post("This section is back on the talk page.", thread_ts=row["slack_ts"])
            if row["title"] != s.title:
                log.info("Section renamed %r -> %r", row["title"], s.title)
                self.store.update_section(s.key, title=s.title)
                if row["slack_ts"]:
                    self.slack.update(row["slack_ts"], self._parent_text(s))
                    self.slack.post(f"Section renamed from “{slack_escape(row['title'])}” "
                                    f"to “{slack_escape(s.title)}”.", thread_ts=row["slack_ts"])

        seen = self.store.comment_keys(s.key)
        new = [c for c in s.comments if c.key not in seen]
        want, responder = self.desired_state(s)
        old = row["completed"]
        if not new and want == old:
            return

        ts = row["slack_ts"]
        had_post = bool(ts)
        if not ts:
            if not new:  # quietly-seeded section whose state changed without new comments
                self.store.update_section(s.key, completed=want, responder=responder)
                return
            ts = self.slack.post(self._parent_text(s))
            self.store.update_section(s.key, slack_ts=ts)

        # Reactions: OPEN = none, DONE = completed, FOLLOWUP = follow-up.
        self._swap_reactions(ts, old if had_post else OPEN, want)
        self.store.update_section(s.key, completed=want, responder=responder)

        # Ping the responding maintainer on the first follow-up after their response.
        ping = want == FOLLOWUP and old != FOLLOWUP and bool(new)
        last_response_ts = max((c.ts for c in s.comments if self.is_maintainer(c.author)), default="")

        for c in new:
            if c.key not in shown_in_parent and not self.is_maintainer(c.author):
                text = self._reply_text(c)
                if ping and c.ts >= last_response_ts:
                    text = f"{self.mention(responder)} there's a follow-up after your response.\n{text}"
                    ping = False
                self.slack.post(text, thread_ts=ts)
            self.store.add_comment(s.key, c)  # record per comment so a crash can't double-post


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def check_workspace(client: WebClient, cfg: Config) -> None:
    """Confirm the token works and belongs to the intended Slack workspace."""
    try:
        auth = client.auth_test()
    except SlackApiError as e:
        sys.exit(f"Slack rejected the token: {e.response.get('error')}")
    log.info("Connected to Slack workspace %r (%s) as %s; posting to channel %s",
             auth.get("team"), auth.get("team_id"), auth.get("user"), cfg.slack_channel)
    if cfg.slack_team_id and auth.get("team_id") != cfg.slack_team_id:
        sys.exit(f"Token belongs to workspace {auth.get('team_id')} ({auth.get('team')}), "
                 f"but SLACK_TEAM_ID is {cfg.slack_team_id}. Refusing to run.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run a single poll and exit (for cron)")
    ap.add_argument("--force", action="store_true", help="process even if the revision hasn't changed")
    ap.add_argument("--post-existing", action="store_true",
                    help="on first run, post every existing section instead of seeding quietly")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    try:  # pick up settings from a .env file next to the script or in the working directory
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
        load_dotenv()
    except ImportError:
        pass

    cfg = Config.from_env()
    client = WebClient(token=cfg.slack_token)
    client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=5))
    check_workspace(client, cfg)
    bot = TarbBot(cfg, WikiClient(cfg), Slack(client, cfg.slack_channel), Store(cfg.db_path))

    while True:
        try:
            bot.run_once(force=args.force, post_existing=args.post_existing)
        except Exception:
            log.exception("Poll failed")
            if args.once:
                sys.exit(1)
        if args.once:
            return
        args.force = False
        time.sleep(cfg.poll_interval)


if __name__ == "__main__":
    main()
