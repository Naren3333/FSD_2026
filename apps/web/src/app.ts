import { Component, HostListener, OnInit, ViewChild, inject, signal } from '@angular/core';
import { Api, Classroom } from './api';
import { ActionState, StatusComponent } from './shared';
import { ClassroomsComponent } from './classrooms';
import { CurriculumComponent } from './curriculum';
import { MaterialsComponent } from './materials';
import { AssessmentsComponent } from './assessments';
import { ReviewComponent } from './review';
import { PerformanceComponent } from './performance';
import { ChatComponent } from './chat';
import { UsageComponent } from './usage';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [
    StatusComponent,
    ClassroomsComponent,
    CurriculumComponent,
    MaterialsComponent,
    AssessmentsComponent,
    ReviewComponent,
    PerformanceComponent,
    ChatComponent,
    UsageComponent,
  ],
  templateUrl: './app.html',
})
export class AppComponent implements OnInit {
  @ViewChild(ClassroomsComponent) classroomForms?: ClassroomsComponent;
  @ViewChild(CurriculumComponent) curriculumForm?: CurriculumComponent;
  @ViewChild(MaterialsComponent) materialsForm?: MaterialsComponent;
  @ViewChild(AssessmentsComponent) assessmentForms?: AssessmentsComponent;
  @ViewChild(ReviewComponent) reviewForm?: ReviewComponent;
  @ViewChild(ChatComponent) chat?: ChatComponent;
  @ViewChild(UsageComponent) usage?: UsageComponent;
  readonly api = inject(Api);
  readonly state = new ActionState();
  readonly classrooms = signal<Classroom[]>([]);
  readonly classroomId = signal('');
  readonly section = signal('classrooms');
  readonly sections = [
    { key: 'classrooms', label: 'Classrooms' },
    { key: 'curriculum', label: 'Curriculum' },
    { key: 'materials', label: 'Teaching materials' },
    { key: 'chat', label: 'Ask the materials' },
    { key: 'assessments', label: 'Assessments' },
    { key: 'review', label: 'Grading review' },
    { key: 'performance', label: 'Learning performance' },
  ];
  ngOnInit(): void {
    void this.state.run(async () => {
      await this.api.initialize();
      if (this.api.profile()) await this.refresh();
    });
  }
  private async refresh(): Promise<void> {
    const rooms = await this.api.classrooms();
    this.classrooms.set(rooms);
    if (!rooms.some((r) => r.id === this.classroomId())) this.classroomId.set(rooms[0]?.id || '');
  }
  load(): void {
    void this.state.run(() => this.refresh());
  }
  login(): void {
    void this.state.run(() => this.api.login());
  }
  logout(): void {
    if (this.hasUnsaved()) {
      this.state.error.set('Finish or clear your unsaved forms before signing out.');
      return;
    }
    void this.state.run(() => this.api.logout());
  }
  /** Opens a section; the AI use meter refreshes too, since indexing may have spent since it last loaded. */
  show(key: string): void {
    this.section.set(key);
    this.usage?.refresh();
  }
  selectClassroom(event: Event): void {
    this.classroomId.set((event.target as HTMLSelectElement).value);
  }
  hasUnsaved(): boolean {
    return (
      [
        this.classroomForms?.form,
        this.classroomForms?.enrollment,
        this.curriculumForm?.form,
        this.materialsForm?.form,
        this.assessmentForms?.form,
        this.assessmentForms?.answers,
        this.reviewForm?.form,
        this.chat?.form,
      ].some((form) => form?.dirty) || !!this.chat?.streaming()
    );
  }
  @HostListener('window:beforeunload', ['$event']) beforeUnload(event: BeforeUnloadEvent): void {
    if (this.hasUnsaved()) {
      event.preventDefault();
      event.returnValue = '';
    }
  }
}
