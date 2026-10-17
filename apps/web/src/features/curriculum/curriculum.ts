import { Component, OnInit, inject } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { CurriculumApi } from '../../api/clients';
import type { Node, NodeKind } from '../../api/models';
import { CurriculumNames } from '../../core/curriculum-names';
import { Session } from '../../core/session';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { requiredText } from '../../shared/validators';

/** Every curriculum level, keyed so the compiler notices a missing one. */
const LEVELS: Record<NodeKind, true> = {
  subject: true,
  course: true,
  unit: true,
  topic: true,
  skill: true,
  objective: true,
};
/** Curriculum levels, top down (the keys keep their written order); each node's parent is the level above it. */
export const NODE_KINDS = Object.keys(LEVELS) as NodeKind[];

@Component({
  selector: 'app-curriculum',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    FieldComponent,
    FieldControlDirective,
    StatusComponent,
    SectionStateComponent,
  ],
  templateUrl: './curriculum.html',
})
export class CurriculumComponent implements OnInit {
  readonly session = inject(Session);
  private readonly curriculum = inject(CurriculumApi);
  /** Shares what this page reads and adds with every page that names skills. */
  private readonly names = inject(CurriculumNames);
  readonly state = new ActionState();
  readonly nodes = new Section<Node[]>();
  readonly kinds = NODE_KINDS;
  readonly form = new FormGroup({
    kind: new FormControl<NodeKind>('skill', { nonNullable: true }),
    name: new FormControl('', { nonNullable: true, validators: requiredText(160) }),
    parent_id: new FormControl('', { nonNullable: true }),
  });

  constructor() {
    inject(UnsavedChanges).track(() => this.form.dirty);
  }

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    void this.nodes.load(() => this.names.refresh());
  }

  create(): void {
    if (!this.state.validate(this.form, 'node-form')) return;
    void this.state.run(async () => {
      const value = this.form.getRawValue();
      await this.curriculum.createNode({ ...value, parent_id: value.parent_id || null });
      this.form.reset({ kind: 'skill', name: '', parent_id: '' });
      this.nodes.set(await this.names.refresh());
    }, 'Curriculum added.');
  }
}
