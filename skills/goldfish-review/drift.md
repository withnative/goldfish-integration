## Brief: hygiene (find where Native has drifted from reality)

This brief replaces "what should be recorded?" with "where has Native drifted
from what actually happened?". It suits a workspace where agents already record
most work. Look for four kinds of drift, and start each item's `description`
with its kind in square brackets:

1. **[unrecorded friction]** Problems the person ran into (errors, failures,
   things that would not load or save) that no record describes. Search for the
   product area and the error wording's distinctive words before deciding.
2. **[stale status]** A record whose status, summary or open questions no longer
   match what Goldfish saw: a task still in progress after its PR merged, a
   question that was answered, a blocker that cleared. Read the record and its
   recent history (`records_read/get_history`) before saying it is stale.
3. **[contradiction]** A flag and a record disagree about a fact (a PR number, a
   date, who did what). Say which looks right and why, or that you cannot tell.
4. **[unrecorded commitment]** Something the person or someone else committed to
   (a follow-up, a promise, a meeting outcome) that no record holds. Use the
   `goldfish_open_loops` and `goldfish_attention` evidence too, when supplied.

Something already recorded accurately is not drift: keep it out, citing the
record.

**Before concluding something is unrecorded**, look at least three ways: two
searches with different wording (the product area plus the symptom, then the
error text or the fix, e.g. a PR number), and one SQL name search over live
work, e.g. `SELECT id, name, lifecycle FROM records WHERE type='WorkItem' AND
archived=0 AND lower(name) LIKE '%slack%' AND lower(name) LIKE '%load%' LIMIT 20`.
Also check the `friction_records`. Search terms are AND-ed, so fewer words find
more. In the reason, say what you searched. If you still find nothing but the
problem sounds like something an agent was actively fixing, mark it uncertain
rather than unrecorded.

## How to record each kind

- **Friction:** one record per distinct problem in `friction_collection`
  (`create` with `destination_id` set to it, `name` a plain description of the
  problem). Give it an `issue_key`: a short stable slug for the problem, e.g.
  `docs-create-blocked-while-loading`. First check `friction_records`: if one
  already describes the same problem (same key or same meaning), use `evidence`
  with `target_id` set to it, and write only the new sighting ("Seen again on
  12 Nov, 3 times in Comet") rather than a duplicate. Friction no record
  describes and no friction collection configured: `create` in the fallback.
- **Relationships** ("probably the same bug as", "user-visible symptom of",
  "lands on the demo route in"): put them in `links` as `{target_id, note}` on
  the friction record you create or add evidence to, not as prose elsewhere.
  Use only records you read. Say in the note whether the link is inferred.
- **Stale status and contradictions:** for now, a `comment` on the drifted
  record saying what looks out of date and what Goldfish saw. Status changes are
  proposals in the comment text, never actions. Basis `inferred` unless the
  flag states the fact directly.
- **Unrecorded commitments:** a `create` task in the collection the work belongs
  in, or uncertain if you cannot tell whose it is.

Set `issue_key` to an empty string and `links` to [] where they do not apply.
A contradiction stays drift until one side is corrected; a note that mentions
it does not resolve it.

When `auto_write` is true, mark friction `create` and `evidence` items `auto`
when you are confident they are real friction; leave stale-status comments on
other people's or agents' records with `auto` false, so the person sees them
first.
