import { Component, HostListener, OnInit, ViewChild, inject, signal } from '@angular/core';
import { Api, Classroom } from './api';
import { ActionState, StatusComponent } from './shared';
import { ClassroomsComponent } from './classrooms';
import { CurriculumComponent } from './curriculum';
import { MaterialsComponent } from './materials';
import { AssessmentsComponent } from './assessments';
import { ReviewComponent } from './review';
import { PerformanceComponent } from './performance';

@Component({selector:'app-root',standalone:true,imports:[StatusComponent,ClassroomsComponent,CurriculumComponent,MaterialsComponent,AssessmentsComponent,ReviewComponent,PerformanceComponent],template:`
<a class="skip-link" href="#workspace">Skip to workspace</a><div class="app-shell"><aside class="sidebar"><a href="/" class="brand"><span class="brand-symbol" aria-hidden="true">L<span>↗</span></span><span>Learning<br><strong>Ledger</strong></span></a><p class="sidebar-label">Teaching & learning</p><nav aria-label="Workspace">@for(item of sections;track item.key){@if(item.key!=='review' || api.profile()?.role==='teacher'){<button [class.active]="section()===item.key" [attr.aria-current]="section()===item.key?'page':null" (click)="section.set(item.key)">{{item.label}}</button>}}</nav><div class="sidebar-footer"><span class="status-dot"></span> Foundation milestone<p>Real learning evidence.<br>AI capabilities coming later.</p></div></aside>
<main id="workspace"><header class="topbar"><span class="workspace-label">LEARNING WORKSPACE</span>@if(api.profile();as profile){<div class="account"><span>{{profile.role==='teacher'?'Teacher':'Student'}} · {{profile.org_id}}</span><button class="secondary" (click)="logout()">Sign out</button></div>}@else{<button (click)="login()" [disabled]="state.busy()">Sign in</button>}</header>
@if(api.profile();as profile){<div class="context-bar"><div><label for="active-classroom">Current classroom</label><select id="active-classroom" [value]="classroomId()" (change)="selectClassroom($event)" [disabled]="hasUnsaved()"><option value="">Choose a classroom</option>@for(room of classrooms();track room.id){<option [value]="room.id" [selected]="room.id===classroomId()">{{room.name}}</option>}</select></div><div class="identity-note"><span>Your ID</span><code>{{profile.id}}</code><small>Share this ID with your teacher for enrollment.</small></div></div><p class="draft-note">{{hasUnsaved()?'Finish or clear your current form before changing classrooms.':''}}</p>
<app-status [state]="state"/><div [hidden]="section()!=='classrooms'"><app-classrooms [classrooms]="classrooms()" [classroomId]="classroomId()" (refresh)="load()"/></div><div [hidden]="section()!=='curriculum'"><app-curriculum/></div><div [hidden]="section()!=='materials'"><app-materials [classroomId]="classroomId()"/></div><div [hidden]="section()!=='assessments'"><app-assessments [classroomId]="classroomId()"/></div>@if(profile.role==='teacher'){<div [hidden]="section()!=='review'"><app-review [classroomId]="classroomId()"/></div>}<div [hidden]="section()!=='performance'"><app-performance [classroomId]="classroomId()"/></div>
} @else {<section class="welcome"><p class="eyebrow">A clearer picture of learning</p><h1>Every next step starts<br>with <em>real evidence.</em></h1><p>Bring your classroom, teaching materials and assessments into one focused workspace. Review the work. Understand the progress.</p><button (click)="login()" [disabled]="state.busy()">Sign in to your workspace <span aria-hidden="true">→</span></button><div class="learning-rail"><span>Classroom</span><span>Materials</span><span>Assessment</span><span>Evidence</span></div><aside class="ai-notice"><span class="badge">Foundation milestone</span><p>Classroom tools and evidence-based grading are available. AI questions, feedback and document chat are not implemented yet.</p></aside></section><app-status [state]="state"/>}
<footer>Learning Ledger <span>Human review. Traceable evidence.</span></footer></main></div>`})
export class AppComponent implements OnInit {
  @ViewChild(ClassroomsComponent) classroomForms?: ClassroomsComponent;
  @ViewChild(CurriculumComponent) curriculumForm?: CurriculumComponent;
  @ViewChild(MaterialsComponent) materialsForm?: MaterialsComponent;
  @ViewChild(AssessmentsComponent) assessmentForms?: AssessmentsComponent;
  @ViewChild(ReviewComponent) reviewForm?: ReviewComponent;
  readonly api=inject(Api);readonly state=new ActionState();readonly classrooms=signal<Classroom[]>([]);readonly classroomId=signal('');readonly section=signal('classrooms');readonly sections=[{key:'classrooms',label:'Classrooms'},{key:'curriculum',label:'Curriculum'},{key:'materials',label:'Teaching materials'},{key:'assessments',label:'Assessments'},{key:'review',label:'Grading review'},{key:'performance',label:'Learning performance'}];
  ngOnInit():void{void this.state.run(async()=>{await this.api.initialize();if(this.api.profile())await this.refresh();});}
  private async refresh():Promise<void>{const rooms=await this.api.classrooms();this.classrooms.set(rooms);if(!rooms.some(r=>r.id===this.classroomId()))this.classroomId.set(rooms[0]?.id||'');}
  load():void{void this.state.run(()=>this.refresh());}
  login():void{void this.state.run(()=>this.api.login());}
  logout():void{if(this.hasUnsaved()){this.state.error.set('Finish or clear your unsaved forms before signing out.');return;}void this.state.run(()=>this.api.logout());}
  selectClassroom(event:Event):void{this.classroomId.set((event.target as HTMLSelectElement).value);}
  hasUnsaved():boolean{return [this.classroomForms?.form,this.classroomForms?.enrollment,this.curriculumForm?.form,this.materialsForm?.form,this.assessmentForms?.form,this.assessmentForms?.answers,this.reviewForm?.form].some(form=>form?.dirty);}
  @HostListener('window:beforeunload',['$event']) beforeUnload(event:BeforeUnloadEvent):void{if(this.hasUnsaved()){event.preventDefault();event.returnValue='';}}
}

