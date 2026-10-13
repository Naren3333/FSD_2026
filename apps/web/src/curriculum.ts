import { Component, inject, signal, OnInit } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { Api, Node } from './api';
import { ActionState, FieldComponent, StatusComponent } from './shared';
@Component({
  selector: 'app-curriculum',
  standalone: true,
  imports: [ReactiveFormsModule, FieldComponent, StatusComponent],
  templateUrl: './curriculum.html',
})
export class CurriculumComponent implements OnInit {
  readonly api = inject(Api);
  readonly state = new ActionState();
  readonly nodes = signal<Node[]>([]);
  readonly kinds = ['subject', 'course', 'unit', 'topic', 'skill', 'objective'];
  readonly form = new FormGroup({
    kind: new FormControl('skill', { nonNullable: true }),
    name: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(160), Validators.pattern(/\S/)],
    }),
    parent_id: new FormControl('', { nonNullable: true }),
  });
  ngOnInit(): void {
    this.load();
  }
  load(): void {
    void this.state.run(async () => this.nodes.set(await this.api.nodes()));
  }
  create(): void {
    if (!this.state.validate(this.form)) return;
    void this.state.run(async () => {
      const value = this.form.getRawValue();
      await this.api.request('curriculum', '/nodes', 'POST', {
        ...value,
        parent_id: value.parent_id || null,
      });
      this.form.reset({ kind: 'skill', name: '', parent_id: '' });
      this.nodes.set(await this.api.nodes());
    }, 'Curriculum added.');
  }
}
