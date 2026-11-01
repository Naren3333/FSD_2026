import { Component, EventEmitter, Input, Output, inject } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { AssessmentApi } from '../../api/clients';
import type { BankQuestion, Question, QuestionType } from '../../api/models';
import { CurriculumNames } from '../../core/curriculum-names';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { IconComponent } from '../../shared/icon';
import { PagedSection } from '../../shared/paged-section';
import { PagerComponent } from '../../shared/pager';
import { SectionStateComponent } from '../../shared/section-state';
import { fromBank, marksText } from './editor';

export const BANK_TYPES: { value: QuestionType; label: string }[] = [
  { value: 'multiple_choice', label: 'Multiple choice' },
  { value: 'numerical', label: 'Numerical' },
  { value: 'short_answer', label: 'Short answer' },
  { value: 'free_response', label: 'Written response' },
];

/* the editor's "add from your bank" panel: the teacher's bank, searched and paged, each question added as a copy linked to it (adr 0017). */
@Component({
  selector: 'app-bank-picker',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    FieldComponent,
    FieldControlDirective,
    IconComponent,
    PagerComponent,
    SectionStateComponent,
  ],
  templateUrl: './bank-picker.html',
})
export class BankPickerComponent {
  private readonly api = inject(AssessmentApi);
  private readonly curriculum = inject(CurriculumNames);
  /* whether the assessment can take another question. */
  @Input() room = true;
  @Input() disabled = false;
  @Output() readonly add = new EventEmitter<Omit<Question, 'id'>>();

  readonly topics = this.curriculum.topics;
  readonly types = BANK_TYPES;
  readonly marksText = marksText;
  readonly text = new FormControl('', { nonNullable: true });
  readonly topic = new FormControl('', { nonNullable: true });
  readonly type = new FormControl<'' | QuestionType>('', { nonNullable: true });
  /* the search form's group, so submitting it searches rather than reloading the page. */
  readonly filters = new FormGroup({ text: this.text, topic: this.topic, type: this.type });
  readonly questions = new PagedSection<BankQuestion>();
  private read = false;

  /* reads the bank the first time the panel opens; after that, only when the teacher asks. */
  opened(): void {
    if (!this.read) this.search();
  }

  search(): void {
    this.read = true;
    this.questions.page.set(0);
    this.load();
  }

  load(): void {
    const filter = { topicId: this.topic.value, type: this.type.value || undefined, text: this.text.value };
    void this.questions.load(() =>
      this.questions.readPage((offset, limit) => this.api.bankPage(filter, offset, limit)),
    );
  }

  topicName(topicId: string): string {
    return this.curriculum.topicName(topicId);
  }

  /* a question on an archived topic stays in the bank but cannot go into an assessment. */
  archived(question: BankQuestion): boolean {
    return this.curriculum.archived().some((t) => t.id === question.topic_id);
  }

  typeName(type: QuestionType): string {
    return BANK_TYPES.find((t) => t.value === type)?.label ?? type;
  }

  choose(question: BankQuestion): void {
    if (!this.room || this.archived(question)) return;
    this.add.emit(fromBank(question));
  }
}
