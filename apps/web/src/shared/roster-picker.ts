import {
  Component,
  EventEmitter,
  Input,
  OnChanges,
  Output,
  SimpleChanges,
  inject,
  signal,
} from '@angular/core';
import { IdentityApi } from '../api/clients';
import type { Student } from '../api/models';
import { studentName } from './names';
import { PagedSection } from './paged-section';
import { PagerComponent } from './pager';
import { SectionStateComponent } from './section-state';

/* reads one page of the classroom's roster, twenty students at a time, into `roster`. */
export function readRosterPage(
  identity: Pick<IdentityApi, 'rosterPage'>,
  roster: PagedSection<Student>,
  classroomId: string,
): Promise<void> {
  return roster.load(() =>
    roster.readPage(async (offset, limit) => {
      const page = await identity.rosterPage(classroomId, offset, limit);
      return { items: page.students, total: page.total };
    }),
  );
}

/* a checkbox list of a classroom's students, a page at a time, whose choices (ids to names) are kept across pages and removable from a chosen list; locked students stay chosen. */
@Component({
  selector: 'app-roster-picker',
  standalone: true,
  imports: [PagerComponent, SectionStateComponent],
  templateUrl: './roster-picker.html',
})
export class RosterPickerComponent implements OnChanges {
  private readonly identity = inject(IdentityApi);
  @Input({ required: true }) classroomId = '';
  /* the chosen students' ids and names, in the order chosen; '' while a name is unknown. */
  @Input() chosen: ReadonlyMap<string, string> = new Map();
  @Input() max = 100;
  /* students to mark with `suggestedLabel`, such as those who lost marks. */
  @Input() suggested: ReadonlySet<string> = new Set();
  @Input() suggestedLabel = 'Lost marks';
  /* a short note beside a student, such as their marks. */
  @Input() notes: ReadonlyMap<string, string> = new Map();
  /* chosen students who cannot be removed, and why. */
  @Input() locked: ReadonlySet<string> = new Set();
  @Input() lockedReason = '';
  /* the locked students' badge. */
  @Input() lockedLabel = 'Submitted';
  /* names for chosen students on no page read yet, such as those who left the class. */
  @Input() names: ReadonlyMap<string, string> = new Map();
  @Input() disabled = false;
  /* a roster the page reads itself (to suggest from the page on show); without one the picker reads its own. */
  @Input() roster: PagedSection<Student> | null = null;
  /* prefixes element ids, so two pickers never share one. */
  @Input() pickerId = 'roster';
  @Output() readonly chosenChange = new EventEmitter<Map<string, string>>();
  /* a page of the roster was read. */
  @Output() readonly rosterRead = new EventEmitter<Student[]>();

  private readonly own = new PagedSection<Student>();
  /* names from every page read, so a student chosen on another page keeps theirs. */
  private readonly known = signal<ReadonlyMap<string, string>>(new Map());

  get full(): boolean {
    return this.chosen.size >= this.max;
  }

  get section(): PagedSection<Student> {
    return this.roster ?? this.own;
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (!this.roster && changes['classroomId']) {
      this.own.clear();
      this.own.page.set(0);
      this.known.set(new Map());
      if (this.classroomId) this.load();
    }
  }

  /* reads the page on show again, or the next one the pager moved to. */
  load(): void {
    const section = this.section;
    void readRosterPage(this.identity, section, this.classroomId).then(() => {
      const people = section.value();
      if (!people) return;
      const known = new Map(this.known());
      for (const s of people) if (studentName(s)) known.set(s.id, studentName(s));
      this.known.set(known);
      this.rosterRead.emit(people);
    });
  }

  isChosen(id: string): boolean {
    return this.chosen.has(id);
  }

  isLocked(id: string): boolean {
    return this.locked.has(id) && this.chosen.has(id);
  }

  /* a student's name: from the chosen list, a page read, the page's fallback, else their bare id. */
  label(id: string): string {
    return this.chosen.get(id) || this.known().get(id) || this.names.get(id) || id;
  }

  /* whether only the bare id is known. */
  unnamed(id: string): boolean {
    return this.label(id) === id;
  }

  rowName(student: Student): string {
    return studentName(student) || student.id;
  }

  chosenList(): string[] {
    return [...this.chosen.keys()];
  }

  toggle(student: Student, checked: boolean): void {
    this.set(student.id, studentName(student), checked);
  }

  remove(id: string): void {
    this.set(id, '', false);
  }

  private set(id: string, name: string, checked: boolean): void {
    if (this.disabled || this.isLocked(id)) return;
    if (checked === this.chosen.has(id)) return;
    if (checked && this.chosen.size >= this.max) return;
    const next = new Map(this.chosen);
    if (checked) next.set(id, name);
    else next.delete(id);
    this.chosenChange.emit(next);
  }
}
