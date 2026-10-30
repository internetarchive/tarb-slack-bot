import unittest

from tarb_bot import Config, Store, TarbBot, norm_user, parse_sections


def comment(author, ts, text="hi", replies=None, cid=None):
    return {"type": "comment", "author": author, "timestamp": ts, "html": f"<p>{text}</p>",
            "id": cid or f"c-{author}-{ts}", "replies": replies or []}


def heading(title, first_author, first_ts, replies, name=None):
    return {"type": "heading", "headingLevel": 2, "level": 0,
            "name": name or f"h-{first_author}-{first_ts}",
            "id": f"h-{title}-{first_ts}", "linkableTitle": title, "replies": replies}


class FakeWiki:
    def __init__(self):
        self.items, self.rev = [], 1

    def set(self, items):
        self.items, self.rev = items, self.rev + 1

    def latest_revid(self):
        return self.rev

    def sections(self, revid):
        return parse_sections(self.items)


class FakeSlack:
    def __init__(self):
        self.n, self.posts, self.updates, self.reactions = 0, [], [], {}

    def post(self, text, thread_ts=None):
        self.n += 1
        ts = f"{self.n}.0"
        self.posts.append((ts, thread_ts, text))
        return ts

    def update(self, ts, text):
        self.updates.append((ts, text))

    def set_reaction(self, ts, name, on):
        self.reactions[ts] = on

    def thread(self, ts):
        return [t for (_, parent, t) in self.posts if parent == ts]

    def parents(self):
        return [(ts, t) for (ts, parent, t) in self.posts if parent is None]


class BotTest(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(slack_token="x", slack_channel="C1",
                          maintainers=frozenset(map(norm_user, ["Harej", "Cyberpower678", "GreenC"])))
        self.wiki, self.slack = FakeWiki(), FakeSlack()
        self.bot = TarbBot(self.cfg, self.wiki, self.slack, Store(":memory:"))

    def run_bot(self, items, **kw):
        self.wiki.set(items)
        self.bot.run_once(**kw)

    def test_first_run_is_quiet_then_lazy(self):
        old = heading("Old bug", "Alice", "20261001000000", [comment("Alice", "20261001000000")])
        self.run_bot([old])
        self.assertEqual(self.slack.posts, [])
        old["replies"][0]["replies"].append(comment("Bob", "20261002000000", "still broken"))
        self.run_bot([old])
        (ts, _), = self.slack.parents()
        self.assertEqual(len(self.slack.thread(ts)), 1)

    def test_full_lifecycle(self):
        self.run_bot([])  # initialise
        c1 = comment("Alice", "20261028100000", "Bot broke my links")
        sec = heading("Broken links", "Alice", "20261028100000", [c1])
        self.run_bot([sec])
        (ts, parent_text), = self.slack.parents()
        self.assertIn("Broken links", parent_text)
        self.assertEqual(self.slack.thread(ts), [])  # opening comment isn't duplicated
        self.assertNotIn(ts, self.slack.reactions)

        # Maintainer replies -> completed, no thread post
        c1["replies"].append(comment("Cyberpower678", "20261028110000", "Fixed"))
        self.run_bot([sec])
        self.assertTrue(self.slack.reactions[ts])
        self.assertEqual(self.slack.thread(ts), [])

        # Someone else follows up -> reaction removed, thread post
        c1["replies"][0]["replies"].append(comment("Alice", "20261028120000", "Not fixed"))
        self.run_bot([sec])
        self.assertFalse(self.slack.reactions[ts])
        self.assertEqual(len(self.slack.thread(ts)), 1)
        self.assertIn("Not fixed", self.slack.thread(ts)[0])

        # Maintainer again (underscore form of name) -> completed again
        c1["replies"].append(comment("GreenC", "20261028130000", "Really fixed"))
        self.run_bot([sec])
        self.assertTrue(self.slack.reactions[ts])

    def test_rename_does_not_create_new_post(self):
        self.run_bot([])
        c1 = comment("Alice", "20261028100000")
        self.run_bot([heading("Typo titel", "Alice", "20261028100000", [c1])])
        self.run_bot([heading("Typo title", "Alice", "20261028100000", [c1])])
        self.assertEqual(len(self.slack.parents()), 1)
        self.assertEqual(len(self.slack.updates), 1)
        self.assertIn("Typo title", self.slack.updates[0][1])

    def test_rekey_when_heading_name_changes(self):
        self.run_bot([])
        c1, c2 = comment("Alice", "20261028100000"), comment("Bob", "20261028110000")
        self.run_bot([heading("X", "Alice", "20261028100000", [c1, c2])])
        # Opening comment removed -> DT name now derives from Bob's comment
        self.run_bot([heading("X", "Bob", "20261028110000", [c2])])
        self.assertEqual(len(self.slack.parents()), 1)

    def test_archived_section_gets_note(self):
        self.run_bot([])
        self.run_bot([heading("Y", "Alice", "20261028100000", [comment("Alice", "20261028100000")])])
        (ts, _), = self.slack.parents()
        self.run_bot([])
        self.assertIn("archived", self.slack.thread(ts)[0])

    def test_iso_timestamps_and_subheadings(self):
        sub = {"type": "heading", "headingLevel": 3, "level": 0, "name": "h-x", "replies":
               [comment("Carol", "2020-01-02T00:00:00.000Z")]}
        items = [heading("Z", "Alice", "2020-01-01T00:00:00.000Z",
                         [comment("Alice", "2020-01-01T00:00:00.000Z", replies=[sub])])]
        (s,) = parse_sections(items)
        self.assertEqual([c.author for c in s.comments], ["Alice", "Carol"])
        self.assertEqual(s.latest.ts, "20200102000000")


if __name__ == "__main__":
    unittest.main()
