# Paste into ~/.claude/CLAUDE.md

Optional. The skills carry the same rules, but this makes every Claude session
route between Goldfish and Native consistently, even when no skill is loaded.

---

## Goldfish and Native

`goldfish` (`mcp__goldfish__*`, plus the `goldfish` skill) is a local, read-only record
of what was on my screen: apps, pages, chats, meetings, time tracking, and an inferred
wiki, promise list and work context. **Native is the source of truth for durable state.**

- **Use Goldfish to recall what I saw or did**: "what was I looking at", "find that
  article/email/thread", "what did I do on Tuesday", where my time went.
- **Use Native for durable work context**: decisions, project and task state, handoffs,
  and anything to remember, save for later or pick up next time. Check Native first for
  these. Goldfish can supplement it but never replaces it.
- **Goldfish's wiki, promises and work context are inferences**, not records. If they
  disagree with Native, Native wins. Mention the discrepancy; don't silently pick one.
- **Nothing durable lives only in Goldfish.** If something found there matters beyond
  this conversation, write it into Native and cite where it came from ("from my
  Goldfish, 10 Nov"). Goldfish has no write path; don't treat it as your memory.
- "What's new from Goldfish?" means the `goldfish-review` skill.
