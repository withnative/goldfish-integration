# Goldfish → Native

Goldfish already notices what matters on your screen and writes short flags
("blocker", "commitment", "artifact", "question"). This kit brings those flags into
Native, so your Native workspace keeps itself up to date without you copying
things across, and personal material stays out.

It is a **starter kit, not a policy**. It works out of the box, and you or your
agent can change any of it: settings, review judgement, wording, cadence.

What's in the box:

| Path | What it is |
| --- | --- |
| `goldfish_review.py` and friends | A small CLI (Python 3.9+ standard library) that reads Goldfish's flags read-only, has Claude review them against Native, and writes to Native. |
| `skills/goldfish-review/` | Claude skill: "what's new from Goldfish?", plus the judgement the background review uses. |
| `skills/goldfish/` | Claude skill for recall ("what was I looking at on Tuesday?"), with the rule that Native, not Goldfish, holds durable state. |
| `agent-instructions.md` | A short block to paste into `~/.claude/CLAUDE.md`, so every Claude session routes between Goldfish and Native the same way. |
| `HOW-IT-WORKS.md` | One page: data flow and who sees what. Read it before you turn on the schedule. |

## What it does by default

Once an hour, the kit reads any new Goldfish flags and asks Claude to review them
alongside what Native already holds. If nothing is new, Claude isn't called. Each
flag then lands in one of three places:

- **Added automatically.** Confident, useful work items, such as a new commitment
  or a comment on an existing task, are written to Native. Each is labelled as
  written automatically from Goldfish, with its basis ("captured" or "inferred").
  There are at most 10 per run, and every one can be undone.
- **Kept out.** Personal, unrelated or already-recorded items stay on your Mac
  with a short reason. Messaging apps (WhatsApp, Messages, Signal, Telegram) are a
  "careful" lane: Claude judges each one, and personal detail never goes into what
  is written.
- **Needs your judgement.** Anything uncertain waits for you, along with any edit
  to someone else's text. You get a macOS notification with counts only, never
  flag text.

To see what happened, ask Claude **"what's new from Goldfish?"**. It leads with
what was added (with undo), then what needs a yes, then what needs your judgement.

Want to approve everything yourself first? Set `review.auto_write` to `false` (see
below). Then every write waits for your yes.

## Set up (about 10 minutes)

You need Goldfish running, Claude Code installed and signed in (`claude auth login`),
and a Native account.

```sh
git clone https://github.com/withnative/goldfish-integration.git
cd goldfish-integration
python3 goldfish_review.py install \
  --native-url https://plugin.withnative.ai/mcp \
  --collection YOUR_COLLECTION_ID
```

`--collection` is the Native collection that new items land in when there is no
better home. Make one called "From Goldfish", or pick an existing one, and copy its
ID. The reviewer can also comment on, or add to, any record it finds elsewhere in
your workspace.

Installation copies the runtime into `~/.local/share/goldfish-review/`, installs
the two skills into `~/.claude/skills/` (an existing `goldfish` skill is kept),
and **enables no schedule**. You can delete the cloned folder afterwards. Running
install again keeps your config and any skill you have edited.

Then:

```sh
cd ~/.local/share/goldfish-review/workspace
./goldfish login     # Native sign-in in your browser
./goldfish doctor    # checks the Goldfish database, Native and your collection
./goldfish review    # first review, by hand: read all three sections
```

Goldfish's database is expected at
`~/Library/Application Support/goldfish/goldfish.db`. The kit only ever opens it
read-only. If yours is somewhere else, change `database` in `config.json`.

When the first review looks right, turn on the hourly run:

```sh
./goldfish schedule enable     # launchd, hourly; catches up after sleep
./goldfish schedule disable
```

The Mac has to be awake for a run. Claude Desktop does not need to be open.

Finally, paste `agent-instructions.md` into `~/.claude/CLAUDE.md` (optional, but it
keeps every Claude session consistent).

## Day to day

```sh
./goldfish briefing                 # everything unhandled + what was added automatically
./goldfish latest                   # the last review again
./goldfish status                   # is a review running? where did the last one get to?
./goldfish undo RECORD_ID           # archive something the kit wrote
./goldfish decide 18,19 dismissed   # local only; flag IDs, not item numbers
./goldfish decide 18 later --hours 24
./goldfish apply REVIEW.json --items 1,3 --approve DIGEST   # send items you said yes to
```

Normally you don't type these yourself: ask Claude "what's new from Goldfish?" and
talk it through. Claude runs `apply` only after your yes. If a write's outcome is
uncertain (a timeout, say), run `./goldfish reconcile REVIEW.json`. Never delete
local state to force a retry.

## Change it

Settings live in `~/.local/share/goldfish-review/config.json`. The review judgement
lives in `~/.claude/skills/goldfish-review/SKILL.md`, and the scheduled review uses
your edited copy.

| Setting | Effect |
| --- | --- |
| `review.auto_write` | `true`: record confident items automatically (default). `false`: everything waits for you. |
| `review.auto_max_per_run` | Limit on automatic writes per run (10). |
| `lanes.work_apps`, `work_domains` | Apps and sites treated as work. Defaults are Native's own; add yours (Slack, Linear, your domain…). |
| `lanes.careful_apps`, `careful_domains` | Messaging sources judged item by item. These rules beat work rules. |
| `lanes.agent_visible` | `["work", "careful"]` by default. `["work"]` keeps messaging flags away from Claude entirely. |
| `review.exclusions` | `explicit` lists every kept-out item; `counts` just counts them. |
| `review.interval_seconds`, `schedule_minute` | Cadence. Disable and re-enable the schedule after changing it. |
| `review.native_search` | Whether the reviewer may search Native (on by default; search terms reach Native's servers). |
| `review.model`, `timeout_seconds` | Which Claude model reviews, and for how long. |

Or ask your agent:

- "Stop writing to Native automatically; I want to approve everything for a week."
- "Hide WhatsApp from Claude, but keep everything else as it is."
- "Treat Slack and linear.app as work."
- "Prefer comments on existing tasks over new ones; show me the skill diff."
- "Brief me twice a day instead of hourly."
- "Add Goldfish's meeting items too." (This needs a reader extension; today only
  `salient_flags` is reviewed, with open loops and calendar items available as context.)

The optional `drift` brief (`review.brief: "drift"`, plus `friction_collection_id`)
uses Goldfish the other way round: it looks for places where Native has fallen out
of step with what you are actually doing, and files product friction. It is what
we run on our own workspace. The default `record` brief is the right start.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The fixture in `tests/fixtures/` is synthetic: Goldfish's real schema, invented content.
