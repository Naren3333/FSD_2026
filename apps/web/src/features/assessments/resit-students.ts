import {
  Component,
  ElementRef,
  EventEmitter,
  Output,
  ViewChild,
  computed,
  inject,
  signal,
} from '@angular/core';
import { AssessmentApi, GradingApi, IdentityApi } from '../../api/clients';
import type { Assessment } from '../../api/models';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { focusLater } from '../../shared/focus';
import { plural } from '../../shared/format';
import { studentNames } from '../../shared/names';
import { RosterPickerComponent } from '../../shared/roster-picker';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { ASSIGNEE_REFUSALS, RESIT_STUDENTS, assigneeRefusal, needsReload, submittedStudents } from './resit';

/* a resit's students as the dialog read them. */
export interface ResitStudents {
  assessment: Assessment;
  /* students with a grade on it, who have submitted and so stay. */
  submitted: ReadonlySet<string>;
  /* assigned students no longer on the roster, shown by their id. */
  departed: string[];
  /* each assigned student's roster name. */
  names: ReadonlyMap<string, string>;
}

/* changes who a resit (draft or published) is assigned to (adr 0017): students who submitted stay, and taking someone off a published resit is confirmed, since their saved answers are deleted. */
@Component({
  selector: 'app-resit-students',
  standalone: true,
  imports: [ConfirmDialogComponent, RosterPickerComponent, SectionStateComponent, StatusComponent],
  templateUrl: './resit-students.html',
})
export class ResitStudentsComponent {
  private readonly assessmentApi = inject(AssessmentApi);
  private readonly gradingApi = inject(GradingApi);
  private readonly identityApi = inject(IdentityApi);
  /* the resit with its new students. */
  @Output() readonly saved = new EventEmitter<Assessment>();
  @ViewChild('dialog', { static: true }) private dialog!: ElementRef<HTMLDialogElement>;
  @ViewChild('removeDialog', { static: true }) removeDialog!: ConfirmDialogComponent;

  readonly maxStudents = RESIT_STUDENTS;
  readonly read = new Section<ResitStudents>();
  readonly state = new ActionState();
  readonly removeState = new ActionState();
  readonly chosen = signal<ReadonlyMap<string, string>>(new Map());
  /* whether the last refusal means reading the students again. */
  readonly stale = signal(false);
  private assessmentId = '';
  /* the button that opened the dialog, which gets focus back. */
  private openerId = '';

  readonly title = computed(() => this.read.value()?.assessment.title ?? '');
  readonly published = computed(() => this.read.value()?.assessment.status === 'published');
  readonly locked = computed(() => this.read.value()?.submitted ?? new Set<string>());
  /* students on the resit now whom the new choice takes off. */
  readonly removed = computed(() => {
    const chosen = this.chosen();
    return (this.read.value()?.assessment.assignees ?? []).filter((id) => !chosen.has(id));
  });
  readonly added = computed(() => {
    const current = new Set(this.read.value()?.assessment.assignees ?? []);
    return [...this.chosen().keys()].filter((id) => !current.has(id));
  });
  readonly plural = plural;

  /* opens the dialog on the resit's students as they are now. */
  open(assessmentId: string, openerId = ''): void {
    this.assessmentId = assessmentId;
    this.openerId = openerId;
    this.state.error.set('');
    this.state.message.set('');
    this.read.clear();
    this.chosen.set(new Map());
    if (!this.dialog.nativeElement.open) this.dialog.nativeElement.showModal();
    focusLater('resit-students-title');
    this.load();
  }

  close(): void {
    this.dialog.nativeElement.close();
  }

  /* reads the resit, who has submitted it and its students' names. */
  load(): void {
    const assessmentId = this.assessmentId;
    this.stale.set(false);
    this.state.error.set('');
    void this.read
      .load(async () => {
        const assessment = await this.assessmentApi.editable(assessmentId);
        if (!assessment.assignees) throw new Error(ASSIGNEE_REFUSALS['NOT_ASSIGNED']);
        const classroomId = assessment.classroom_id;
        const [submitted, roster] = await Promise.all([
          submittedStudents((offset, limit) =>
            this.gradingApi.evaluations(classroomId, assessmentId, offset, limit),
          ),
          this.identityApi.roster(classroomId),
        ]);
        const enrolled = new Set(roster.map((s) => s.id));
        const names = studentNames(roster);
        return {
          assessment,
          submitted,
          names,
          departed: assessment.assignees.filter((id) => !enrolled.has(id)),
        };
      })
      .then(() => {
        const read = this.read.value();
        if (read && assessmentId === this.assessmentId)
          this.chosen.set(new Map(read.assessment.assignees?.map((id) => [id, read.names.get(id) ?? ''])));
      });
  }

  /* escape waits while a save runs. */
  onCancel(event: Event): void {
    if (this.state.busy()) event.preventDefault();
  }

  onClose(): void {
    if (this.removeDialog.isOpen()) this.removeDialog.close();
    if (this.openerId) focusLater(this.openerId);
  }

  /* saves the choice, confirming first when it takes anyone off a published resit. */
  save(): void {
    const read = this.read.value();
    if (!read || this.state.busy()) return;
    this.state.message.set('');
    if (!this.chosen().size) {
      this.state.error.set('Choose at least one student for the resit.');
      return;
    }
    if (!this.removed().length && !this.added().length) {
      this.close();
      return;
    }
    // students only see a published resit, so only then can someone being taken off have saved answers.
    if (this.published() && this.removed().length) {
      this.removeDialog.show();
      return;
    }
    void this.write(this.state);
  }

  /* the confirmation's own action, reported inside it. */
  /* a refusal closes the confirmation and shows in the dialog, beside what fixes it. */
  confirmRemove(): void {
    void this.write(this.removeState).then(() => {
      const error = this.removeState.error();
      this.removeDialog.close();
      if (error) this.state.error.set(error);
    });
  }

  removedNames(): string {
    const read = this.read.value();
    return this.removed()
      .map((id) => read?.names.get(id) || id)
      .join(', ');
  }

  private write(state: ActionState): Promise<void> {
    const assessmentId = this.assessmentId;
    const ids = [...this.chosen().keys()];
    return state.run(async () => {
      try {
        const saved = await this.assessmentApi.setAssignees(assessmentId, ids);
        this.saved.emit(saved);
        this.close();
      } catch (error) {
        this.stale.set(needsReload(error));
        throw new Error(assigneeRefusal(error));
      }
    });
  }
}
