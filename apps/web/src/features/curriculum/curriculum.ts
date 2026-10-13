import { Component, OnInit, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { CurriculumApi } from '../../api/clients';
import type { Node } from '../../api/models';
import { Session } from '../../core/session';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { FieldComponent } from '../../shared/field';
import { StatusComponent } from '../../shared/status';

/** Curriculum levels, top down; each node's parent is the level above it. */
export const NODE_KINDS = ['subject', 'course', 'unit', 'topic', 'skill', 'objective'];

@Component({
  selector: 'app-curriculum',
  standalone: true,
  imports: [ReactiveFormsModule, FieldComponent, StatusComponent],
  templateUrl: './curriculum.html',
})
export class CurriculumComponent implements OnInit {
  readonly session = inject(Session);
  private readonly curriculum = inject(CurriculumApi);
  readonly state = new ActionState();
  readonly nodes = signal<Node[]>([]);
  readonly kinds = NODE_KINDS;
  readonly form = new FormGroup({
    kind: new FormControl('skill', { nonNullable: true }),
    name: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(160), Validators.pattern(/\S/)],
    }),
    parent_id: new FormControl('', { nonNullable: true }),
  });

  constructor() {
    inject(UnsavedChanges).track(() => this.form.dirty);
  }

  ngOnInit(): void {
    this.load();
  }

  load(): void {
    void this.state.run(async () => this.nodes.set(await this.curriculum.nodes()));
  }

  create(): void {
    if (!this.state.validate(this.form)) return;
    void this.state.run(async () => {
      const value = this.form.getRawValue();
      await this.curriculum.createNode({ ...value, parent_id: value.parent_id || null });
      this.form.reset({ kind: 'skill', name: '', parent_id: '' });
      this.nodes.set(await this.curriculum.nodes());
    }, 'Curriculum added.');
  }
}
