# ADR 0026: Independent question review

Status: accepted 2026-11-22. The project owner approved the reliability plan for marking, chat and question generation, and this ADR is its question generation part. It amends ADR 0006, ADR 0015 and ADR 0022, and uses the profiles and re-ask of ADR 0023.

## Context

The question reviewer (ADR 0015) ran on the same model and settings as the writer it checks, so their errors tend to match. It wrote its own answer first, but nothing made it say whether the cited passages support the key, or whether a second option is also right. Marking guidance was written in one call with no review. A reply that ai-core rejected or that could not be read ended the request. The reviewer probe (`scripts/eval/review_probe.yaml`) missed 2 of 13 planted faults:

- `groups-shown`: "Using the three groups shown, which group does 404 belong to?" points at a list only the materials show.
- `transaction-guarantee`: "What does running changes in a transaction guarantee?" has two right options, because the passage gives both atomicity and isolation.

## Decision

**The reviewer runs on the `check` profile.** Question review, practice review (ADR 0022) and the new guidance review send `model_profile: "check"` (`CHECK_MODEL` at `CHECK_REASONING_EFFORT`, ADR 0023). Writers stay on `default`.

**Key checks.** Each review now has two booleans between `own_answer` and `verdict`:

- `evidence_supports_key`: the question's passages state or directly imply the key.
- `other_option_also_correct`: multiple choice only. The reviewer checks every option other than the key against the passages, one at a time.

The prompt asks the reviewer to work out `own_answer` from the cited passages before it reads the key. Our code applies the checks even when the reviewer says `pass`:
- A second right option fails a multiple-choice draft as `AMBIGUOUS`. The generator is told "another option is also right according to the cited passages; make every wrong option plainly wrong".
- An unsupported key fails the draft as `NOT_ANSWERABLE`. The generator is told "the cited passages do not support its answer key; key it to what the passages state".
- A missing or non-boolean answer fails closed.
- `other_option_also_correct` is ignored for short and written answers.
- When the reviewer itself fails a draft, its reason is kept, and the more specific problem is added when a check also fired.

No new reason codes reach the page. The prompts are version 2 (`PROMPT_VERSION`, logged with each request).

**The probe misses.**
- `groups-shown` is a wording fault, so it gets a deterministic check (`checks.points_at_materials`). It matches "the …" plus one to four words and then a pointer word. A pointer word followed by "above", "below", "here" or "earlier" always matches ("the steps listed above"). "shown", "listed", "pictured", "illustrated", "depicted", "above" or "below" alone match only at the end of a clause ("the three groups shown,").
  - It leaves ordinary wording alone: "the companies listed on the exchange", "the values below the minimum", "a drug shown to lower blood pressure".
  - It also leaves pointers at the question's own options or answers ("which of the options listed").
  - It drops the draft as `NEEDS_MATERIALS` before any review, so it costs no model call. The reviewer's `NEEDS_MATERIALS` rule gains the same examples.
- `transaction-guarantee` is a judgement about meaning, so no wording check can catch it. `other_option_also_correct` covers it, and the `AMBIGUOUS` rule now says the option is ambiguous "even when the key is the better answer".
- The probe still sends `groups-shown` to the reviewer, so it keeps measuring the reviewer. Its summary adds `missed_after_checks`, which leaves out what the agent's own checks drop.

**One re-ask for unusable writes.** A generation reply that ai-core rejects (`AI_OUTPUT_REJECTED`) or that has no `questions` list is asked once more through `AgentClients.generate(..., check=..., reask=1)`. The follow-up carries the faulty reply and what was wrong with it.
- Each draft's rule failures (evidence, key, duplicates) still go back through the rounds as `previous_attempts`, as before. The re-ask handles only a reply that cannot be read at all, so nothing is sent back twice.
- Still unusable after the re-ask: `502 AI_OUTPUT_REJECTED` in round 0, and an `UNREADABLE_REPLY` round later, as before.
- The first write is now cut off at the later of the request deadline and its own timeout plus a second. Its re-ask must start inside that too, so round 0 still lasts at most one write timeout. Later calls are cut off at the deadline, as before.

**Call budget, unchanged at six.** `MAX_CALLS = 6` (three writes and three reviews) covers re-asks too.
- A write is re-asked only while the re-ask and the round's review still fit. A retry round starts only while its write and review fit.
- A re-ask therefore costs a later round, never a seventh call. With the review off, a request makes at most 2 calls.
- `model_calls` counts every ai-core request, re-asks included. A call refused before any request still counts once (ADR 0015).
- The 240-second deadline and the "1.5 times the longest round" rule are unchanged. A re-asked round is longer, so it makes a later retry less likely.

**Guidance is reviewed.** `POST /v1/marking-guidance` with the review on (`QUESTION_REVIEW`):
1. The guidance is written, checked, and asked again once if the checks or ai-core reject it.
2. It is reviewed on the `check` profile (`marking.guidance.reviewed`). The reviewer sees the question, the accepted answers or rubric and model answer, the marking points and only the cited passages, never the evidence. It answers `own_answer`, `evidence_supports_key`, a verdict and a reason (`NOT_ANSWERABLE`, `WRONG_KEY`, `AMBIGUOUS`).
3. A failed review uses the one re-ask if it is still unused. The rewrite gets the reviewer's problem and note, and is reviewed again.
4. Still failing: `422 GUIDANCE_REJECTED`.

Call counts:
- At most 4: write, review, rewrite, review.
- Or 3: write, re-ask, review. Then a failed review is refused, because the re-ask is used up.
- With the review off, 1 or 2.

All calls are billed to the teacher. Every call after the first is cut off at the request deadline (`QUESTION_REVIEW_DEADLINE`).

When the first review cannot run (unreachable, out of time, unreadable), the guidance is returned as before, because the teacher reviews it before use. A rewrite whose review cannot run is not served. The response shape is unchanged.

## Results

Measured 2026-11-22 with real keys. The reviewer probe on the `check` profile caught 13 of 13 planted faults with no good draft failed (was 11 of 13), and held that over three passes on both `gpt-6-luna` (reason named 97.4%, $0.0070) and `gpt-6.2-sol` (100%, $0.0567). `groups-shown` and `transaction-guarantee` are now caught. Generation A/B (`run_generation.py`, 13 runs, $0.021): with the review on, 22 of 22 questions returned (20 passed, 2 flagged) after the reviewer sent back 19 drafts (13 weak wrong options, 3 with a second right answer, 3 invalid keys), where on 2026-10-27 it sent back none; without it 21 of 22 (one `EVIDENCE_NOT_FOUND`). The review cost 32 model calls against 6 and p50 41 s against 8.8 s. Practice (`--practice`, first run, $0.014): 17 of 20 returned, all passed; three sets came back one multiple-choice question short after six calls, mostly from weak-distractor rejections; the off-topic request was refused with no generate call.

After the amendment below (2026-11-22, `PROMPT_VERSION` 3): the probe at `QUESTION_REVIEW_EFFORT=low` caught 97.4% with 8.3% of good drafts wrongly failed, against 100% and 0% at medium, so the reviewer stays at the profile's medium. Teacher generation with the review on: 22 of 22 returned (16 passed, 6 flagged weak and served without a rewrite), 14 model calls against 32 before, p50 16.8 s against 41 s (8.8 s with the review off), $0.009. Practice: unchanged at 17 of 20 returned, all passed; weak-distractor rejections fell from 14 to 10, p50 40 s. The wrong-option rules helped the writer only a little; short practice sets remain.

## Consequences

- A review now uses reasoning tokens on the `check` profile, so it costs more and takes longer than before. A slow round leaves less room for a retry inside the 240 s deadline.
- A guidance request costs up to four model calls instead of one. `QUESTION_REVIEW=off` turns the guidance review off along with the question review.
- The pointer check can drop an ordinary question whose clause ends on "shown" or "listed" ("Name one of the four elements shown."). The generator gets the draft back with the `NEEDS_MATERIALS` problem and can reword it.
- ai-core must accept `model_profile: "check"` (ADR 0023) before this agent is deployed.
- `AgentClients.generate` does not report how many requests a call made, so the agent counts them itself by overriding `_generate_once` in its own client. Reporting the count from the shared helper would remove that override.
- Not built: a re-ask for an unreadable review (the drafts come back `not_reviewed`, or the practice set is refused), and extra searching for passages when a draft is sent back.

## Amendment: faster generation and better distractors

Approved 2026-11-22 with the plan to speed up question generation.

**A teacher's weak-distractor draft is not rewritten.** A draft that the reviewer fails only as `WEAK_DISTRACTORS`, with both key checks holding, now counts toward its type. It is still served flagged with the reviewer's note, so it is no longer worth a rewrite round when the teacher reviews it anyway.
- It joins the questions held so far, so a later round's writer and reviewer see it under `existing_questions`, and it is not sent back as a previous attempt or counted in `sent_back`.
- A weak-distractor draft whose key checks failed, and a `LONG_KEY` draft, are still sent back as before. A passed rewrite still replaces its flagged original (`REWRITE_RATIO`).
- Practice is unchanged: only passed drafts count or are served, and a weak draft is sent back and then dropped (ADR 0022).

**`QUESTION_REVIEW_EFFORT`.** The question, practice and guidance reviewers send it as the call's `reasoning_effort` (ADR 0023 amendment): `none`, `low` or `medium`, any case. Unset or empty keeps the `check` profile's own effort (`CHECK_REASONING_EFFORT`). Any other value stops the agent at start. The writers are not affected. The reviewer probe uses the agent's setting, or `run_generation.py --review-effort` for one run.

**Wrong-option rules for the writer.** The generation and practice instructions now open with rules for wrong options, taken from the reviewer's `WEAK_DISTRACTORS` rubric:
- each is a mistake a student who half-knows the topic would make
- it matches the key's length, grammar and detail
- no "all of the above", "none of the above", jokes or off-topic options
- none restates the key.

The reviewer now names the weak wrong options in `weak_options` on a `WEAK_DISTRACTORS` failure. When a weak draft goes back, its `previous_attempts` entry carries those options as the draft wrote them, and the writer is told to replace each one. A name that does not match one of the draft's wrong options is left out. The prompts are version 3.
