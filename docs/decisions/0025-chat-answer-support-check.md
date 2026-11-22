# ADR 0025: Chat answer support check

Status: accepted 2026-11-22. The project owner approved the reliability plan for marking, chat and question generation, and authorized this new paid call in that round. It amends ADR 0002 (grounded answering) and builds on ADR 0023's `check` profile.

## Context

The chatbot confirms that each `[S#]` tag names a passage that was in the prompt (`citations.py`), and the coverage notice confirms that every topic of a multi-topic question has a citation. Neither confirms that the cited passage says what the sentence claims. An answer can cite a real passage for a claim the passage does not make, or cite nothing and answer from general knowledge. The keyword leg of Content's search used `websearch_to_tsquery`, which requires every word of the query, so a long natural-language question rarely had keyword hits. A failed query embedding failed the whole search.

## Decision

**One support check per cited answer.**

- After the answer stream ends, the chatbot makes one structured ai-core `generate` call on the `check` profile (ADR 0023: `CHECK_MODEL` at `CHECK_REASONING_EFFORT`), under the schema name `answer_check.v1`. It is billed to the same user as the answer (ADR 0003).
- The call gets each answer sentence that carries citations (at most 24, each up to 300 characters) with the tags it cites, and the text of the cited passages only. Passages and sentences are escaped and marked as data, never instructions.
- The reply is one verdict per sentence: `supported`, `partial` or `unsupported`, with a reason of at most 20 words. A reply that misses, repeats or invents a sentence id is asked again once (ADR 0023's re-ask), within a 20 s deadline. The visible budget is 1500 tokens.
- The check is skipped for refusals (`no_results`), small talk, errors and interrupted answers, and for answers with no citations.
- A failed check (provider error, spend limit, timeout, a reply still faulty after the re-ask) logs `answer_check_failed` with its code and shows nothing. It never fails the turn.

**Notices.** They are appended after the answer, and after the coverage notice when there is one. They are streamed as `token` events before `citations` and `done` (amended below: the unsupported-claim notice now follows `done`), and saved in the message's `content`, so a reloaded thread shows them. The web app renders them through the same Markdown path as the coverage notice, with no new component.

- Any `unsupported` sentence: "The cited passages may not support: “…”; “…”." It names up to three sentences, each cut to 80 characters with markup and quotes removed, and then "and N more". `partial` gets no notice.
- An `answered` turn with no citations that states something gets "This answer does not cite the materials." It is recorded as `uncited`, with no model call. A reply counts as stating something when it has a declarative sentence of at least six words. A reply made only of questions does not count, nor does one that says the materials do not cover the question (a "couldn't find" or "don't contain" sentence).

**Storage and logs.**

- Migration `007_answer_check.sql` adds a nullable `answer_check jsonb` to the chatbot's `messages`. It is additive: no default and no rewrite of existing rows.
- The column holds the status (`checked`, `uncited`, `failed`), the prompt version, the verdict counts and, per checked sentence, its text, cited chunk ids and verdict. Reasons are not saved: they may paraphrase a passage, and a deleted document's passages are redacted (ADR 0010) while this column is not.
- It is null for refusals, small talk, errors and answers saved before this change. The public `Message` contract is unchanged.
- The `chat_turn` log line gains `answer_check` (the status) and `unsupported` (the count), plus `check_ms` when the check ran.

**Content search.**

- The keyword leg turns the query into an OR of its `plainto_tsquery` lexemes, with stop words dropped (`replace(plainto_tsquery('english', $5)::text, ' & ', ' | ')::tsquery`), and keeps `ts_rank_cd`. Passages that share more of the words rank first. The query stays a bound parameter.
- When the query embedding fails or times out, content searches with the keyword leg alone instead of failing. It logs `embedding_fallback` and sets `embedding_fallback: true` in the response (contract updated).
- The rerank still scores those hits, so the relevance floors still apply. If the rerank also fails, `rerank_fallback` is set and the chatbot and the question-generation agent refuse as before.
- A spend limit, too many AI calls at once, or a caller that went away still fails the search.

## Cost

On `gpt-6-luna` ($0.10 per million input tokens, $0.50 per million output tokens), a checked turn sends about 1,000–4,500 input tokens: the instructions, the cited passages and the sentences. It returns about 200–400 visible tokens plus medium-effort reasoning. That is roughly $0.0005–$0.002 per checked turn, and up to about twice that with a re-ask. The reservation holds 1500 × 2.5 output tokens while it runs. On `gpt-6.2-sol` the same turn would cost roughly $0.01–$0.03. The check adds its latency (expected a few seconds, at most 20 s) after `done` (amended above).

## Amendment: the check after `done`

Accepted 2026-11-22 in the next round's plan, which the project owner approved. The check made `done` wait up to 20 s.

- **Order.** The chatbot saves the turn as before, with no `answer_check` for a cited answer, then sends `citations` and `done`. `done` gains `checking: true` when a support check will run (the answer has cited statements whose passages were in the prompt), and `false` otherwise. It then runs the check and saves its record on the saved message (`Store.save_check`, an update by message id: no migration). An unsupported-claim notice is appended to `content` in the same statement, then sent as a new `notice` event `{message_id, text}`, where `text` is the notice with its leading blank line. The stream closes after the check, with or without a notice.
- **Uncited answers.** The uncited check needs no model call, so "This answer does not cite the materials." is still a `token` before `done`, with `checking: false`.
- **Leaving.** The check starts as a detached task (tracked in the same `pending` set as `_save_detached`) before `done` is sent. The stream awaits it through `asyncio.shield`, so a client that leaves after `done` still gets its check and notice saved, and the lifespan waits for it. `SAVE_GRACE` is 25 s, to cover the 20 s check deadline, and the purge consumer is told to stop before that wait so both fit in the container's 45 s stop grace. Leaving before `done` saves the answer as `interrupted`, unchecked, as before; the `streamed` flag this ADR's first version used is gone.
- **Failures.** A failed check logs `answer_check_failed` and saves `{"status": "failed"}` with the content unchanged and no `notice`. If the record cannot be saved, `answer_check_save_failed` is logged and the row stays as saved. If the thread was deleted meanwhile, the update finds no row, `chat_thread_gone` is logged and nothing is sent.
- **Logs.** When a check runs, the `chat_turn` line is written once it ends, with `answer_check`, `unsupported` and `check_ms`. `latency_ms` is still the time to the saved answer.
- **Web.** From `done` with `checking: true` until the stream closes, the answer shows a quiet "Checking the sources…" caption (`role="status"`), and the page's polite live region reads "Answer complete. Checking the sources…". The send lock ends at `done`, so the next question can be typed and sent meanwhile. A `notice` is appended to the answer's text. When the stream closes the caption goes, the live region reads "Sources checked." or "Sources checked: see the note under the answer.", the AI meter is refreshed, and the conversation is re-read, unless another question is being answered (its own re-read then shows the saved notice). Older clients ignore `notice` and see it on reload.
- **Eval.** `run_chat.py` reads the stream to its close, so the saved row it reads has the check. It records `done_ms` (time to `done`, as the client sees it) next to the saved `latency_ms`, fails a turn whose streamed notice is not saved, and has a `support` group of `unsupported_notice: true` cases.
- **Prompt versions.** `student_tutor.v4` and `teacher.v3`: the shared policy now asks, for each listed topic, for a passage found for that topic, preferring one whose subject is that topic over one that only mentions it. `query_rewrite.v4`: a message that describes a practice or problem without naming it keeps its words and adds the one standard term it clearly matches, in parentheses ("... (waterfall model)"), and adds none when the match is unclear. The v3 rule against guessing an unnamed subject stays. `needs_rewrite` no longer lets a first-turn "what is"/"explain" question skip the rewrite when it has `when`, `why`, `how`, `if`, `happens` or `wrong`. The 0.61 relevance floor is unchanged.

## Results

Measured 2026-11-22 with real keys. `run_chat.py` (103 cases, 107 turns): 51 answers checked, 113 statements, 83.2% supported, 16.8% partial, 0 unsupported, 0 uncited, 0 check failures; answer latency p50 3.1 s, max 15.9 s. Two cases failed: `waterfall-weakness` (still under the 0.61 floor, as on 2026-10-20) and `multi-design-testing` (nothing cited from Software Testing); `multi-review-testing` and `multi-persistence-design`, which failed on 2026-10-20, passed. With the OR keyword leg, `run_golden.py --rerank --holdout` gave hit@1 0.951 (was 0.924), MRR 0.974 (was 0.950), recall@10 1.0 and no leaks. No case sets `unsupported_notice` yet, so the checker's catch rate on a genuinely unsupported answer is unmeasured.

After the check moved behind `done` (`student_tutor.v4`, `teacher.v3`, `query_rewrite.v4`; 2026-11-22, 106 cases, 110 turns): time to `done` p50 3.4 s, max 8.1 s, against an answer latency max of 15.9 s when `done` waited for the check. 54 answers checked, 99 statements, 85.9% supported, 14.1% partial, 0 unsupported, 0 uncited, 0 check failures. `waterfall-weakness` and `multi-design-testing` now pass. `multi-estimation-sprints-process` and `multi-persistence-design` failed with nothing cited from one retrieved document (the latter also failed on 2026-10-20), so multi-topic citation is still uneven between runs. The three `support` cases were written to expect a notice, but the model said which specifics the materials lack and cited only what they hold; they now expect no notice, as false-alarm checks. The checker's catch rate on a genuinely unsupported answer remains unmeasured.

## Consequences

- Each cited answer costs one more model call against the user's daily allowance. Since the amendment it no longer delays `done`; leaving after `done` still saves the check.
- The checker is a model's judgment. A wrong `unsupported` shows a notice on a correct answer, and a wrong `supported` shows nothing. The notice says "may not support" for that reason.
- The uncited heuristic can miss an uncited claim inside a reply that also says something is not covered, and can note a long hint with no citation.
- The chatbot's history includes earlier notices, as it already did for the coverage notice.
- The shared `search_content` helper (`learning_transport.agents`) does not yet read `embedding_fallback`, so the agents cannot log it. Content's own `search` log line records it.
- Upgrade: `docker compose up -d --build question-chatbot-agent content`. The chatbot applies migration 007 on start. It needs an ai-core that accepts `model_profile: "check"` (ADR 0023).
