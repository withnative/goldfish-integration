# How it works

```text
Your screen
   │  Goldfish captures text (on your Mac)
   ▼
Goldfish's own model writes short flags  ──►  salient_flags table in goldfish.db
                                                   │  read-only SQL, above a checkpoint
                                                   ▼
                                    lanes: work / careful (messaging) — deterministic
                                                   │  only if there are new flags
                                                   ▼
                     Claude (claude -p, your account) + read-only Native tools
                     groups duplicates, checks what Native already holds, decides:
                                                   │
               ┌───────────────────────────────────┼──────────────────────────┐
               ▼                                   ▼                          ▼
     add automatically (≤10/run)          needs your judgement           keep out
     → Native, labelled + undoable        → local review file            → local review file
                                            + notification (counts)
```

## Who sees what

| Party | Sees | Doesn't see |
| --- | --- | --- |
| **Goldfish** (and its own gateway/model) | Everything it captures. That's Goldfish as you already run it; this kit changes nothing there. | — |
| **Claude** (your Claude Code account) | Flag text for both lanes by default, plus app, site and window metadata, plus the Native records it reads. May keep session history under your Claude settings. | Raw screen snapshots. Messaging flags too, if you set `agent_visible: ["work"]`. |
| **Native** | What gets written: automatic items (labelled) and items you approve. Also the reviewer's short search terms. | Kept-out items, uncertain items, review files, anything pending. Nothing is queued in Native. |
| **macOS notifications** | Counts only. | Any flag text. |

## Guarantees the code keeps

- Goldfish's database is opened read-only (`mode=ro`, `query_only`). The kit never writes to it.
- The background reviewer has **read-only** Native tools and no shell. Writes are made
  afterwards by the CLI, against the saved review.
- Every write carries provenance: `source: goldfish`, its basis (captured or inferred),
  whether it was automatic or approved, and the source flag IDs, kinds, apps and times.
- Automatic writes are only `create` and `comment` (plus repeat-sighting evidence on
  the kit's own friction records, with the `drift` brief). Adding to an existing
  record's body (`update`) always waits for a yes.
- A write-ahead journal and idempotency keys mean a rerun never duplicates a write,
  and an uncertain outcome is reconciled, never replayed.
- A failed run (for example Native unavailable) leaves the checkpoint where it was,
  so the next run catches up.

## What lives where on your Mac

`~/.local/share/goldfish-review/`: `config.json`, `state.json` (decisions, by flag ID
and content hash), `reviews/` (review files), `native-auth.json` (your Native sign-in;
private), `schedule.log`. Back it up if you care about undo history. Delete it to
uninstall, after running `./goldfish schedule disable`.
