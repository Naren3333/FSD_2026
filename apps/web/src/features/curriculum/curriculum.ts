import { Component, OnInit, ViewChild, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { CurriculumApi } from '../../api/clients';
import type { Topic } from '../../api/models';
import { CurriculumNames } from '../../core/curriculum-names';
import { Session } from '../../core/session';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { requiredText } from '../../shared/validators';

@Component({
  selector: 'app-curriculum',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    FieldComponent,
    FieldControlDirective,
    StatusComponent,
    SectionStateComponent,
    ConfirmDialogComponent,
  ],
  templateUrl: './curriculum.html',
})
export class CurriculumComponent implements OnInit {
  readonly session = inject(Session);
  private readonly curriculum = inject(CurriculumApi);
  /** Shares what this page reads and adds with every page that names topics. */
  private readonly names = inject(CurriculumNames);
  readonly state = new ActionState();
  /** Archiving a topic, in its confirmation dialog. */
  readonly archiveState = new ActionState();
  /** The topic waiting for its archiving to be confirmed. */
  readonly archiving = signal<Topic | null>(null);
  @ViewChild('archiveDialog') archiveDialog?: ConfirmDialogComponent;
  readonly topics = new Section<Topic[]>();
  readonly form = new FormGroup({
    name: new FormControl('', { nonNullable: true, validators: requiredText(160) }),
  });

  constructor() {
    inject(UnsavedChanges).track(() => this.form.dirty);
  }

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    void this.topics.load(() => this.names.refresh());
  }

  create(): void {
    if (!this.state.validate(this.form, 'topic-form')) return;
    void this.state.run(async () => {
      const { name } = this.form.getRawValue();
      await this.curriculum.createTopic({ name });
      this.form.reset({ name: '' });
      this.topics.set(await this.names.refresh());
    }, 'Topic added.');
  }

  confirmArchive(topic: Topic): void {
    this.archiving.set(topic);
    this.archiveDialog?.show();
  }

  closeArchive(): void {
    this.archiving.set(null);
    this.archiveDialog?.close();
  }

  archive(): void {
    const topic = this.archiving();
    if (!topic) return;
    void this.archiveState.run(async () => {
      await this.curriculum.archiveTopic(topic.id);
      this.closeArchive();
      // The dialog has closed, so refreshing the list reports to the page, where a failure stays visible.
      void this.state.run(async () => {
        this.topics.set(await this.names.refresh());
      }, `${topic.name} archived.`);
    });
  }
}
