# Agentic workflows

The agentic features we could build, in the order we plan to take them, and where each stands. Retrieval (indexing, search, the chatbot's search and document formats) is worked on separately, so each entry says whether it touches that work. Update the status here when a workflow moves, and give each built one its own ADR.

| # | Workflow | Status | Touches retrieval |
|---|---|---|---|
| 1 | Performance interpretation agent | Built (ADR 0014) | No |
| 2 | Marking critic and escalation | Built (ADR 0020) | No |
| 3 | Question generation critic loop | Built (ADR 0015) | Reads the passages generation already has |
| 4 | Adaptive resit agent | Proposed, needs a question bank | Only if it generates new questions |
| 5 | Teacher class-insights copilot | Built (ADR 0021) | No: it does not search materials |
| 6 | Interpretation on finalization | Proposed, after 1 | No |

How the workflows would feed each other once built (thick border: built, dashed: proposed):

```mermaid
flowchart TB
  Submit[assessment.submitted.v4] --> Marker[Grading marker suggests marks]
  Marker --> W2[Workflow 2: Marking critic flags doubtful marks]
  Marker --> Final[Teacher finalizes grades]
  W2 --> Final
  Final -->|teacher asks| W1[Workflow 1: Performance interpretation]
  Final -.->|grading.finalized.v3| W6[Workflow 6: Interpretation on finalization]
  W6 -.-> W1
  W1 -.->|recommended topics| W4[Workflow 4: Adaptive resit]
  W4 -.->|bank short of questions| W3[Workflow 3: Question generation critic loop]
  W3 --> Editor[Teacher adds drafts to an assessment]
  W4 -.->|teacher approves| Editor
  W5[Workflow 5: Class-insights copilot] -->|teacher follows a student link| W1
  W5 -->|tools| Data[Analytics, Grading, Assessment, Curriculum, Identity]
  classDef built stroke-width:3px
  classDef proposed stroke-dasharray:4 3
  class W1,W2,W3,W5 built;
  class W4,W6 proposed;
```

## What makes these agentic

Every earlier AI feature is a fixed pipeline: code chooses the inputs and makes one structured model call. In these workflows the model chooses what to look at next (tools), a later step checks or revises an earlier one (critics), or one agent's output starts another's work (hand-offs). Each one keeps the project's rules: every model call goes through ai-core and is billed to a person, tools read with the user's own token, nothing reaches a student or changes a grade without the teacher, and every claim is checked against the evidence it cites.

ADR 0014 built the pattern the others reuse, inside `student-performance-agent`: a turn loop over ai-core's structured `generate` (no tool-calling API was needed), short references the model cites instead of ids, a turn limit and a deadline that force a report, a step trace for the teacher, and checks that drop anything the gathered evidence does not support. Workflow 5 is its second user, in the same service, so the loop moved into the service's own `loop.py` and not into `learning_transport` (ADR 0021).

A fixed pipeline next to the two agentic shapes built so far:

```mermaid
flowchart LR
  subgraph Critic["Critic loop: workflow 3"]
    direction LR
    C1[Generator] --> C2[Checks] --> C3[Reviewer]
    C3 -->|rejected, with reason| C1
    C3 -->|passed or flagged| C4[Drafts for the teacher]
  end
  subgraph Tools["Tool loop: workflows 1 and 5"]
    direction LR
    T1[Opening evidence] --> T2{Model turn}
    T2 -->|tool calls| T3[Read-only tools] --> T2
    T2 -->|report| T4[Checks against evidence] --> T5[Draft for the teacher]
  end
  subgraph Fixed["Fixed pipeline: marking"]
    direction LR
    F1[Code picks inputs] --> F2[One structured call] --> F3[Checks drop bad output] --> F4[Draft for a person]
  end
```

The rules every workflow keeps, wherever its loop sits:

```mermaid
flowchart TB
  Teacher[Teacher in the browser] -->|own token, through Kong| Agent[Agent service]
  Agent -->|is the caller the classroom's teacher?| Identity
  Agent -->|reads with the teacher's token| APIs[Business service APIs]
  APIs -.-x|never| DB[(Business databases)]
  Agent -->|every call names billed_to| AICore[ai-core]
  AICore --> Spend{Daily allowance left?}
  Spend -->|no| Refuse[429 AI_SPEND_LIMIT]
  Spend -->|yes| Model[Model provider]
  Model --> Agent
  Agent --> Checks[Checks: cited evidence must exist and support the claim]
  Checks -->|unsupported| Dropped[Dropped and counted, never repaired]
  Checks -->|supported| Draft[Draft marked for teacher review]
  Draft --> Teacher
  Draft -.-x|never directly| Student[Student or grade]
```

## 1. Performance interpretation agent

Built. A teacher asks for an interpretation of one student's finalized results. The agent starts from the topic summary and results, then chooses to read the answers behind weak topics, check prerequisites, or compare with the class, and drafts observations, possible misconceptions, next steps and topics to practise. Services: student-performance-agent, Analytics, Grading (new `student_id` filter), Assessment, Curriculum, ai-core. See ADR 0014.

Opening the workspace, before any model call:

```mermaid
sequenceDiagram
  autonumber
  actor T as Teacher
  participant A as student-performance-agent
  participant I as Identity
  participant An as Analytics
  participant G as Grading
  participant As as Assessment
  participant C as Curriculum
  T->>A: POST /v1/performance_interpretation (classroom, student)
  A->>I: teaches the classroom, student enrolled?
  I-->>A: yes (otherwise 403)
  par side by side
    A->>An: released topic summary
  and
    A->>G: finalized evaluations (student_id filter)
  end
  par side by side
    A->>As: batch read of those assessments
  and
    A->>C: topic names
  end
  Note over A: no finalized results: 422 NO_EVIDENCE, no model call<br/>otherwise give results, questions and topics short refs R1, E1, T1
```

The turn loop (defaults: five turns, a two-minute deadline for tool turns, a four-minute budget for the whole request):

```mermaid
flowchart TD
  Start([Workspace opened]) --> Final{"Last turn, past the deadline,<br/>or too little budget for a tool turn?"}
  Final -->|yes| Report["Report-only turn<br/>billed to the teacher"]
  Final -->|no| Turn["Tool turn: structured generate<br/>billed to the teacher"]
  Turn --> Reply{Reply}
  Reply -->|report| Check
  Reply -->|up to 3 tool calls| Tools
  Reply -->|"no calls, unreadable or<br/>recoverable failure"| Force[Next turn must report] --> Report
  subgraph Tools["Read-only tools, side by side"]
    RA["read_answers R#<br/>prompt, key, answer, rubric, feedback"]
    PR["prerequisites T#<br/>Curriculum + student's evidence"]
    CC["class_comparison T#<br/>rest of class from Analytics"]
  end
  Tools -->|"results, refusals and failures<br/>added to the history and the step trace"| Final
  Report --> Check["Checks against gathered evidence:<br/>every observation cites a shown question,<br/>misconceptions cite read answers that lost marks,<br/>topics must be shown ones"]
  Check -->|no observation survives| Rejected[422 INTERPRETATION_REJECTED]
  Check -->|some survive| Done(["Draft: observations, misconceptions,<br/>next steps, topics, cited questions, steps, dropped counts<br/>requires_teacher_review, nothing saved"])
```

`scripts/eval/run_performance.py` checks it against scripted students of known shape (`docs/development/operations.md`, Agent evals). Next steps for it: rate those reports by hand, try it on a few real students, then decide whether to link findings to teaching material once retrieval settles.

## 2. Marking critic and escalation

Built. After `services/grading/marker.go` makes a ready suggestion for a written answer, its job becomes a review job: a second model call marks the same answer blind, under its own instructions and without the first suggestion, and its reply passes the same checks. Code compares the two: different levels, low confidence on either side or a new flag make the suggestion doubtful, which the teacher sees as "Check AI mark", first in Grading review and in both finalize dialogs; anything that fails is not reviewed, never agreed. `MARKING_CRITIC=off` turns it off. Not built: flagging similar answers in one assessment that got different suggested marks, and measuring it against the marks teachers finalized. Services: Grading, ai-core, web review page. See ADR 0020.

```mermaid
flowchart TD
  Job[marking_jobs row, stage suggest] --> Suggest[Marker call: marking.suggestion]
  Suggest --> Check1{Checks: rubric levels, quotes in the answer}
  Check1 -->|fails| Failed[Failed suggestion, no review]
  Check1 -->|ready| Critic{MARKING_CRITIC on?}
  Critic -->|no| Done[Ready suggestion, job deleted]
  Critic -->|yes| Review[Same row, stage review, review pending]
  Review --> Blind["Critic call: marking.review<br/>same task, own instructions,<br/>never the first suggestion"]
  Blind -->|spend limit, busy, not configured| Wait[Waits without using an attempt]
  Wait --> Blind
  Blind --> Check2{Same checks}
  Check2 -->|fails, or the call fails| NotReviewed[not_reviewed, with why]
  Check2 -->|passes| Compare{"compareSuggestions:<br/>levels differ, low confidence,<br/>new flag?"}
  Compare -->|yes| Doubtful[doubtful: Check AI mark, first in the queue]
  Compare -->|no| Agreed[agreed]
  Doubtful --> Teacher[Teacher marks and finalizes]
  Agreed --> Teacher
  NotReviewed --> Teacher
```

## 3. Question generation critic loop

Built. A reviewer model pass checks each generated question against its cited passages only: answerable, key matches what the reviewer worked out, one right answer, plausible wrong options, not a repeat. Rejected drafts go back to the generator with the reason, up to two retries inside one deadline; weak wrong options are flagged for the teacher instead of dropped, and a reviewer failure returns drafts marked not reviewed. Teachers can skip the review per request. Services: question-generation-agent, ai-core, web generator panel. See ADR 0015.

One request, from passages to drafts (defaults: two retries, a 240-second deadline, so at most three writes and three reviews):

```mermaid
flowchart TD
  Req([Teacher asks for questions on a topic]) --> Auth[Identity: classroom's teacher?]
  Auth --> Topic[Curriculum: topic name]
  Topic --> Search["Content search with the teacher's token<br/>passages above the relevance floor"]
  Search --> Gen["Generator call<br/>asks only for each type's shortfall,<br/>with previous attempts and their reasons"]
  Gen --> Checks{"Deterministic checks:<br/>quote in passages, key fits options,<br/>not a duplicate, no 'according to the passage'"}
  Checks -->|duplicate or wrong type| Drop[Dropped, not sent back]
  Checks -->|other failure| Back
  Checks -->|passes| ReviewOn{Review on?}
  ReviewOn -->|"no, review: false"| Unreviewed[Kept, checked against quotes only]
  ReviewOn -->|yes| Reviewer["Reviewer call: sees prompt, options<br/>and only the cited passages, not the key's reasoning"]
  Reviewer -->|call failed or out of time| NotReviewed[Kept as not_reviewed, with why]
  Reviewer --> Verdict{Verdict}
  Verdict -->|passed| LongKey{"Key >= 40 chars and<br/>> 1.8x the other options?"}
  LongKey -->|no| Passed[Passed]
  LongKey -->|yes, LONG_KEY| Back
  Verdict -->|"NOT_ANSWERABLE, WRONG_KEY, AMBIGUOUS,<br/>REPEATS_EXISTING, NEEDS_MATERIALS"| Back
  Verdict -->|WEAK_DISTRACTORS| Back
  Verdict -->|no usable verdict| NotReviewed
  Back[Sent back with the reason] --> More{"A type still short, retries left,<br/>and 1.5x the longest round fits the deadline?"}
  More -->|yes| Gen
  More -->|no| Last["Weak or long-key drafts kept flagged if their type has room<br/>and no rewrite of them was chosen; others dropped"]
  Passed --> Out
  Unreviewed --> Out
  NotReviewed --> Out
  Last --> Out(["Drafts by type, passed first, with each round's<br/>counts and the call count; nothing added until the teacher does"])
```

`scripts/eval/run_generation.py` compares requests with the review on and off, and its `--probe` sends planted faulty drafts straight to the reviewer to measure what it catches. Next steps for it: use the probe's misses to tune the reviewer, and decide from the on/off comparison whether the review earns its extra calls.

## 4. Adaptive resit agent

Takes the topics the interpretation agent recommends, picks questions on them from a question bank (or asks the generator for new ones), and drafts a personal resit for the teacher to approve and publish. Services: Assessment (question bank and resits, not built yet), student-performance-agent, question-generation-agent. Effort: large. It closes the loop from assessing to diagnosing to practice.

## 5. Teacher class-insights copilot

Built. A teacher asks one question about a class ("who is struggling with recursion, and what should I reteach?") and gets one checked answer: who is struggling with which topic, observations, possible misconceptions and what to reteach, each with the answers it rests on. It is a second route in student-performance-agent, `POST /v1/class_insights`, on the interpretation agent's loop (`loop.py`) with its own workspace, tools, prompts and checks. It is stateless: a follow-up resends up to three earlier questions as context, and the evidence is read again every time. The model sees students as `S1` references only; their names are added after the checks, and each cited student links to Learning performance for an interpretation. It does not search Content. Services: student-performance-agent, Analytics, Grading, Assessment, Curriculum, Identity, ai-core. See ADR 0021.

```mermaid
flowchart TD
  Ask([Teacher asks a question]) --> Auth[Identity: classroom's teacher?]
  Auth --> Open["Side by side: Analytics class metrics (first 200),<br/>Identity roster names (kept in the agent),<br/>then Curriculum topic names"]
  Open -->|no released evidence| Empty[422 NO_EVIDENCE, no model call]
  Open --> Loop["Shared turn loop: up to 5 turns,<br/>refs S1, T1, Q1, R1, E1 only"]
  subgraph Tools["Read-only tools, side by side"]
    TD[topic_detail T#]
    TQ["topic_questions T#<br/>one shared class read from Grading"]
    SR["student_results S#"]
    RA["read_answers Q# or R#"]
    PR["prerequisites T#"]
  end
  Loop <--> Tools
  Loop --> Check["Checks: struggling students need enough evidence and a weak result,<br/>statements cite shown topics or answers,<br/>misconceptions cite read answers that lost marks,<br/>percentages match shares shown for what an item cites"]
  Check -->|nothing survives| Rejected[422 INSIGHTS_REJECTED]
  Check -->|nothing reported| Nothing[422 NOTHING_TO_ANSWER]
  Check -->|some survive| Names[Names added for cited students] --> Draft([Draft for the teacher, nothing saved])
```

`scripts/eval/run_class_insights.py` checks it on two scripted classes, repeatably (`docs/development/operations.md`, Agent evals). Not built: saved threads, material search, and running the interpretation agent from inside it.

## 6. Interpretation on finalization

Run workflow 1 automatically when `grading.finalized.v3` arrives for a student whose evidence changed enough, store the draft, and show the newest one on the class dashboard. Needs storage for interpretations, a decision on who pays for runs nobody asked for, and a limit on how often one student is re-read. Services: student-performance-agent (consumer and database), Kafka, web. Effort: medium.
