import { Component, EventEmitter, Input, Output, inject } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { Api, Classroom } from './api';
import { ActionState, FieldComponent, StatusComponent } from './shared';

@Component({selector:'app-classrooms',standalone:true,imports:[ReactiveFormsModule,FieldComponent,StatusComponent],template:`
<div class="section-heading"><div><p class="eyebrow">Your teaching space</p><h2>Classrooms</h2></div><button class="secondary" (click)="refresh.emit()">Refresh classrooms</button></div>
<p>Keep teaching materials, assessments and evidence together in one classroom.</p>
<div class="split"><section class="panel"><h3>Your classrooms</h3><p class="caption">Up to 100 most recent classrooms</p>
@for(item of classrooms;track item.id){<article class="record"><strong>{{item.name}}</strong><span class="identifier">{{item.id}}</span></article>}@empty{<p class="empty">No classrooms yet. Teachers can create one; students need to be enrolled by their teacher.</p>}
</section>@if(api.profile()?.role==='teacher'){<section class="panel"><h3>Create a classroom</h3><form novalidate [formGroup]="form" (ngSubmit)="create()">
<app-field controlId="class-name" label="Classroom name" [invalid]="form.controls.name.touched && form.controls.name.invalid" error="Enter a name of 1–120 characters."><input id="class-name" formControlName="name" maxlength="120" [attr.aria-invalid]="form.controls.name.touched && form.controls.name.invalid" aria-describedby="class-name-help"></app-field>
<button [disabled]="state.busy()">Create classroom</button><button type="reset" class="secondary clear-form" [disabled]="state.busy()">Clear form</button></form>
<h3 class="separated">Enroll a student</h3><p>Select a classroom above. The student must sign in once before enrollment.</p><form novalidate [formGroup]="enrollment" (ngSubmit)="enroll()"><app-field controlId="enroll-student" label="Student ID" hint="The student can find their ID in the workspace header." [invalid]="enrollment.controls.student_id.touched && enrollment.controls.student_id.invalid"><input id="enroll-student" formControlName="student_id" aria-describedby="enroll-student-help" [attr.aria-invalid]="enrollment.controls.student_id.touched && enrollment.controls.student_id.invalid"></app-field><button [disabled]="state.busy() || !classroomId">Enroll student</button><button type="reset" class="secondary clear-form" [disabled]="state.busy()">Clear form</button></form><app-status [state]="state"/>
</section>}</div>`})
export class ClassroomsComponent {
  readonly api=inject(Api); readonly state=new ActionState();
  @Input() classrooms: Classroom[]=[]; @Input() classroomId=''; @Output() refresh=new EventEmitter<void>();
  readonly form=new FormGroup({name:new FormControl('',{nonNullable:true,validators:[Validators.required,Validators.maxLength(120),Validators.pattern(/\S/)]})});
  readonly enrollment=new FormGroup({student_id:new FormControl('',{nonNullable:true,validators:[Validators.required]})});
  create():void {if(!this.state.validate(this.form))return;void this.state.run(async()=>{await this.api.request('identity','/classrooms','POST',this.form.getRawValue());this.form.reset();this.refresh.emit();},'Classroom created.');}
  enroll():void {if(!this.state.validate(this.enrollment))return;void this.state.run(async()=>{await this.api.request('identity',`/classrooms/${this.classroomId}/enrollments`,'POST',this.enrollment.getRawValue());this.enrollment.reset();},'Student enrolled.');}
}


