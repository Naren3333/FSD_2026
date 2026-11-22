import { Component, Input, OnChanges, computed, inject } from '@angular/core';
import { RouterLink } from '@angular/router';
import { PerformanceAgentApi } from '../../api/clients';
import type { Interpretation, InterpretationEvidence, InterpretationStep } from '../../api/models';
import { AiUsage } from '../../core/ai-usage';
import { CurriculumNames } from '../../core/curriculum-names';
import { plural } from '../../shared/format';
import { Section } from '../../shared/section';
import { IconComponent } from '../../shared/icon';
import { SectionStateComponent } from '../../shared/section-state';

/* the longest question prompt an evidence line shows. */
const PROMPT_CHARS = 120;

/* the performance agent's tools, for both interpretation and class insights. */
const TOOLS: Record<string, string> = {
  read_answers: 'Read answers',
  prerequisites: 'Checked prerequisites',
  class_comparison: 'Compared with the class',
  topic_detail: 'Read the topic’s results',
  topic_questions: 'Read the topic’s questions',
  student_results: 'Read a student’s results',
};

const OUTCOMES: Record<InterpretationStep['outcome'], string> = {
  ok: 'done',
  refused: 'not allowed',
  failed: 'could not be read',
};

/* "Read answers: Loops quiz (done). Why: …" for one of the agent's steps. */
export function stepText(step: InterpretationStep): string {
  const tool = TOOLS[step.tool] ?? step.tool;
  return `${tool}: ${step.target} (${OUTCOMES[step.outcome]}). Why: ${step.reason}`;
}

/* "Loops quiz · What does range(3) produce? · 0 of 2 marks" for a cited question. */
export function evidenceText(e: InterpretationEvidence): string {
  const prompt = e.prompt.length > PROMPT_CHARS ? e.prompt.slice(0, PROMPT_CHARS - 1) + '…' : e.prompt;
  return `${e.assessment_title} · ${prompt || 'Question no longer available'} · ${e.awarded} of ${e.possible} marks`;
}

/* "2 statements were left out because they did not rest on this student's results.", or '' when none were. */
export function droppedText(dropped: Record<string, number>, whose = "this student's"): string {
  const total = Object.values(dropped).reduce((sum, n) => sum + n, 0);
  if (!total) return '';
  return `${total} ${total === 1 ? 'statement was' : 'statements were'} left out because they did not rest on ${whose} results.`;
}

/* each assessment the statements cite, once, in the order first cited. */
export function citedAssessments(
  draft: Pick<Interpretation, 'observations' | 'possible_misconceptions' | 'evidence'>,
): { id: string; title: string }[] {
  const byQuestion = new Map(draft.evidence.map((e) => [e.question_id, e] as const));
  const found = new Map<string, string>();
  for (const item of [...draft.observations, ...draft.possible_misconceptions]) {
    for (const id of item.evidence_ids) {
      const e = byQuestion.get(id);
      if (e && !found.has(e.assessment_id)) found.set(e.assessment_id, e.assessment_title);
    }
  }
  return [...found].map(([id, title]) => ({ id, title }));
}

/* asks the performance agent to interpret one student's finalized results, for their teacher to review (adr 0014). */
@Component({
  selector: 'app-interpretation',
  standalone: true,
  imports: [RouterLink, SectionStateComponent, IconComponent],
  templateUrl: './interpretation.html',
})
export class InterpretationComponent implements OnChanges {
  private readonly api = inject(PerformanceAgentApi);
  private readonly usage = inject(AiUsage);
  private readonly curriculum = inject(CurriculumNames);
  @Input({ required: true }) classroomId = '';
  @Input({ required: true }) studentId = '';
  readonly result = new Section<Interpretation>();
  readonly evidence = computed(
    () => new Map((this.result.value()?.evidence ?? []).map((e) => [e.question_id, e] as const)),
  );
  /* the assessments a resit could be made of for this student. */
  readonly resits = computed(() => {
    const draft = this.result.value();
    return draft ? citedAssessments(draft) : [];
  });
  readonly dropped = computed(() => droppedText(this.result.value()?.dropped ?? {}));
  readonly stepText = stepText;
  readonly evidenceText = evidenceText;
  readonly plural = plural;

  ngOnChanges(): void {
    // another student's evidence is on show: an earlier interpretation no longer applies.
    this.result.clear();
  }

  topic(topicId: string): string {
    return this.curriculum.topicName(topicId) || 'A topic no longer in the curriculum';
  }

  cited(ids: readonly string[]): InterpretationEvidence[] {
    const found = this.evidence();
    return ids.flatMap((id) => found.get(id) ?? []);
  }

  interpret(): void {
    const classroomId = this.classroomId;
    const studentId = this.studentId;
    if (!classroomId || !studentId) return;
    void this.result
      .load(async () => {
        const interpretation = await this.api.interpret(classroomId, studentId);
        // without the curriculum, recommended topics show a generic name.
        await this.curriculum.need(interpretation.recommended_topic_ids).catch(() => undefined);
        return interpretation;
      })
      .then(() => this.usage.refresh());
  }
}
