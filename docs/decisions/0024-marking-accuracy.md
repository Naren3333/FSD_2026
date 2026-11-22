# ADR 0024: Marking accuracy and critic profile

Status: accepted 2026-11-22. The project owner approved the reliability plan for marking, chat and question generation, and this ADR is its marking part. It amends ADR 0004 and ADR 0020, and uses ADR 0023's model profiles.

## Context

The marker's reply schema let the model name any criterion and give any whole number of points, so a reply with a level the rubric does not have was only caught afterwards, as `INVALID_OUTPUT`. A quote was matched to the answer with only whitespace ignored, so a quote that changed the case, or turned a straight quote or hyphen into a curly quote or dash, failed as `UNGROUNDED`. Any reply that failed a check ended the suggestion, with no second attempt. A short answer that differed from an accepted answer only by a full stop or curly quotes still cost a model call. The critic ran on the same model and settings as the marker it checks. Nothing measured how often suggestions match a teacher's levels.

## Decision

**A schema per question.** `markingSchema(rubric)` limits each criterion of the reply to the rubric's own criteria. A rubric with one criterion (every short answer) has a single item schema: its `criterion_id` is an enum of that id and its `points` an enum of its levels. A rubric with several has `anyOf`, one branch per criterion, each with its own id and levels. OpenAI's strict json_schema supports `anyOf` below the root, and the schema stays far under ai-core's 20,000-character limit. A single union enum of every id and every level was rejected because it still lets a criterion take another criterion's level. The schema cannot require each criterion exactly once, so `checkSuggestion` still checks that. It also checks every level, in case a provider ignores the schema.

**Evidence matched as the student wrote it.** Quotes are matched after `fold`: the text is lowercased, curly single and double quotes become `'` and `"`, Unicode dashes and the minus sign become `-`, zero-width characters and soft hyphens are dropped, and whitespace runs (including no-break spaces) become one space. This applies to evidence and to contradicting quotes. Words still have to be the student's own.

**Exact short answers without a model call.** Accepted answers are stored on each short-answer question (ADR 0004). An answer equal to one under assessment's own rule (trimmed, spaces collapsed, case ignored unless it matters) is already marked correct and never reaches the marker. For the rest, before any model call, grading compares the answer and each accepted answer after `plain` (quotes, dashes and invisible characters as above), collapsed whitespace, a trailing full stop or exclamation mark and one pair of wrapping quotes removed, and case ignored unless the question says it matters. On a match, grading stores a ready suggestion of full marks:
- `model` is `exact_match`, confidence `high`, the answer as its evidence, and a rationale naming the accepted answer and what was ignored.
- It is still only a suggestion. The question still needs marking, and the teacher still marks and finalizes it.
- With the critic on, its review is `not_reviewed` with the reason `EXACT_MATCH`, without a model call. There is no model reply for a second examiner to doubt, and ADR 0020 already ends a review without a call when one would only cost the teacher's allowance (`MARKED_BY_TEACHER`). A review job for an exact-match suggestion, should one be queued, ends the same way.

**The critic on the check profile.** The critic's `marking.review` call sends `model_profile: "check"` (ADR 0023: `CHECK_MODEL` at `CHECK_REASONING_EFFORT`). The marker's `marking.suggestion` call sends `default` (the chat model at low reasoning).

**One re-ask.** When a marking reply is `INVALID_OUTPUT` or `UNGROUNDED`, or ai-core rejects it (`AI_OUTPUT_REJECTED`), grading asks once more before the suggestion or review fails:
- The follow-up is the original messages, then the faulty reply when there is one, then "your previous reply failed these checks: …; return a corrected reply." This is the wording `learning_transport.agents` uses (ADR 0023).
- The faults say what was wrong: which criterion quoted which words that are not in the answer, which criterion gave points above its lowest level without evidence, which criteria to mark, or that the reply was not the schema. The stored failure reason stays the bare code.
- The re-ask is billed to the same teacher and runs inside the same 90-second `markingCall`. It is started only while 20 seconds of it are left.
- A `CONTRADICTED` reply is not asked again. The model saw evidence against its own level, and a second try would only teach it to drop that evidence. `EMPTY_OUTPUT` is not either.
- A re-ask that still fails ends as before, with the last check's reason. A re-ask that fails as a call (spend limit, `AI_BUSY`, a provider failure) follows ADR 0004's waiting and backoff, and the next attempt starts again from the first call. Leases and attempts are unchanged: a re-ask is part of one attempt.
- The critic's re-ask passes back only its own previous reply, so it still never sees the first suggestion.

**Token budget.** `markingTokens` stays 1500. It is the visible reply budget, and ai-core adds the reasoning headroom for each profile (×1.5 for low, ×2.5 for medium, ADR 0023). A reply for ten criteria is expected to fit in 1500 visible tokens. This has not been measured. A cut-off reply would show up as `AI_OUTPUT_REJECTED` in the eval's failure reasons.

**A marking eval.** `scripts/eval/marking_cases.yaml` holds 30 drafted answers to four questions: two free responses with two criteria each, and two short answers. The answers include strong, partial, wrong, hedged, off-topic, prompt-injection, case and whitespace variants, answers assessment's rule marks itself, and exact short answers. Each has the expected level of every criterion and `teacher_rating: null`.
- `load_marking.py` makes the students, classroom and one published assessment with AI marking per question. It makes no paid call.
- `run_marking.py` submits each answer as its own student through Kong, waits for the suggestion and its second check, and scores them. Each case's cost is what ai-core charged the teacher while it ran, because the marker's calls carry no request correlation id. The run stops at `--max-usd` (budget kind `marking`) and writes its result to `.local/eval-runs/`. `--rescore` scores an earlier run's suggestions again without a call.
- `delete_marking.py` deletes the classroom, through Identity, and the eval's topics.
- Metrics: level agreement per criterion, whole-case agreement (all cases and model-made ones), mean absolute points error, the rate and reasons of unusable replies, the critic's doubtful rate, its true-doubt rate (doubtful when the first marking differs from the expected levels), its false-doubt rate (doubtful when it matches), the critic's own agreement, and how many expected flags were raised.

## Results

Measured 2026-11-22 with real keys (`run_marking.py`, 30 cases over 4 questions): level agreement 1.0, mean absolute points error 0, no unusable or ungrounded replies, 3 exact matches made with no model call, 2 auto-marked by Assessment. The critic agreed on all 25 model suggestions (doubtful rate 0, false-doubt rate 0) with both `gpt-6-luna` ($0.011 a run) and `gpt-6.2-sol` ($0.101). No first marking was wrong, so the true-doubt rate is unmeasured. The expected levels are the plan author's draft, which a teacher should confirm through `teacher_rating`; the set needs harder borderline answers before it can separate markers.

## Consequences

- A faulty reply can now cost two calls instead of one, against the same teacher's allowance. A ready suggestion with its review can now cost up to four.
- The critic now runs on the check profile, which costs more per call than the marker's (medium reasoning).
- The review page shows an exact-match suggestion's review as "Not second-checked: the answer matches an accepted answer exactly." (`EXACT_MATCH` in `apps/web/src/features/review/marking.ts`).
- Fake-model unit tests and database tests cover the schema enums, case and Unicode matching, the exact-match path without a call, the critic's profile, the re-ask's success and failure, and duplicate submissions with a re-ask or an exact match. No real-key run was made.
- Upgrade: `docker compose up -d --build`. No migration or `.env` change is needed. ai-core must already accept the `check` profile (ADR 0023) before this Grading starts.
- Not built: re-asking a contradicted reply, an exact match that ignores more than quotes, dashes and closing punctuation, and a measured choice of the check model.
