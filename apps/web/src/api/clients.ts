import { Injectable, inject } from '@angular/core';
import { Gateway } from '../core/gateway';
import { documentMime } from '../shared/files';
import type {
  Account,
  AccountInput,
  AccountPage,
  Assessment,
  AssessmentPage,
  ClassroomPage,
  CreatedAccount,
  DocumentPage,
  TopicPage,
  AssessmentDetails,
  AssessmentProgress,
  AssessmentSummary,
  BankFilter,
  BankQuestion,
  BankQuestionInput,
  BankQuestionPage,
  ChatMessage,
  ClassInsights,
  ClassInsightsRequest,
  ClassPerformance,
  Classroom,
  DocumentRecord,
  Enrollment,
  Evaluation,
  EvaluationPage,
  GenerateRequest,
  GenerateResult,
  GradingCounts,
  BulkFinalized,
  Guidance,
  GuidanceRequest,
  Interpretation,
  Mark,
  NewAssessment,
  OwnResult,
  OwnSubmission,
  Performance,
  PersonRole,
  PracticeRequest,
  PracticeResult,
  Profile,
  ResitRequest,
  SchoolClassroomPage,
  SavedAnswers,
  StreamEvent,
  Student,
  StudentPage,
  Submission,
  Thread,
  Topic,
  Usage,
} from './models';

/* one typed client per backend. */

const id = encodeURIComponent;

/* the most students one page of a classroom's roster holds. */
const ROSTER_PAGE = 500;

/* full metadata reads for pickers/counts still use bounded server pages, `size` at a time; visible lists request a single page. */
async function allPages<T>(
  fetch: (offset: number, limit: number) => Promise<{ items: T[]; total: number }>,
  size = 100,
): Promise<T[]> {
  const items: T[] = [];
  for (;;) {
    const page = await fetch(items.length, size);
    items.push(...page.items);
    if (!page.items.length || items.length >= page.total) return items;
  }
}

@Injectable({ providedIn: 'root' })
export class IdentityApi {
  private readonly gateway = inject(Gateway);

  /* registers the signed-in user (idempotent) and returns their profile. */
  register(): Promise<Profile> {
    return this.gateway.request('identity', '/me', { method: 'POST', body: {} });
  }
  /* deletes the signed-in user's own account for good (adr 0011). */
  deleteAccount(): Promise<void> {
    return this.gateway.request('identity', '/me', { method: 'DELETE' });
  }
  classrooms(): Promise<Classroom[]> {
    return allPages((offset, limit) => this.classroomPage(offset, limit));
  }
  classroomPage(offset: number, limit: number): Promise<ClassroomPage> {
    return this.gateway.request('identity', `/classrooms/page?offset=${offset}&limit=${limit}`);
  }
  /* one page of the classroom's students by name, for its teacher only, and how many are enrolled. */
  rosterPage(classroomId: string, offset: number, limit: number): Promise<StudentPage> {
    const query = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    return this.gateway.request('identity', `/classrooms/${id(classroomId)}/students?${query}`);
  }
  /* every student of the classroom by name, for its teacher only, read a page of 500 at a time. */
  roster(classroomId: string): Promise<Student[]> {
    return allPages(
      (offset, limit) =>
        this.rosterPage(classroomId, offset, limit).then((page) => ({
          items: page.students,
          total: page.total,
        })),
      ROSTER_PAGE,
    );
  }
}

/* the school admin's management of the school's accounts, classrooms and enrollment (adr 0019). */
@Injectable({ providedIn: 'root' })
export class AdminApi {
  private readonly gateway = inject(Gateway);

  /* one page of the school's teacher or student accounts by name, matching the search, and how many match. */
  accounts(role: PersonRole, search: string, offset: number, limit: number): Promise<AccountPage> {
    const query = new URLSearchParams({ role, search, offset: String(offset), limit: String(limit) });
    return this.gateway.request('identity', `/admin/accounts?${query}`);
  }
  /* every teacher of the school, for choosing a classroom's teacher: the first page gives the total, then the rest are read together, 50 at a time. */
  async teachers(): Promise<Account[]> {
    const size = 50;
    const first = await this.accounts('teacher', '', 0, size);
    const offsets: number[] = [];
    for (let offset = first.accounts.length ? size : first.total; offset < first.total; offset += size) {
      offsets.push(offset);
    }
    const rest = await Promise.all(offsets.map((offset) => this.accounts('teacher', '', offset, size)));
    // an account created meanwhile can push one onto two pages.
    const byId = new Map<string, Account>();
    for (const page of [first, ...rest]) page.accounts.forEach((a) => byId.set(a.id, a));
    return [...byId.values()];
  }
  /* creates a sign-in account in the school; its temporary password comes back this once. */
  createAccount(account: AccountInput): Promise<CreatedAccount> {
    return this.gateway.request('identity', '/admin/accounts', { method: 'POST', body: account });
  }
  /* gives a teacher or student of the school a new temporary password, which comes back this once; the old one stops working. */
  resetPassword(accountId: string): Promise<CreatedAccount> {
    return this.gateway.request('identity', `/admin/accounts/${id(accountId)}/password`, { method: 'POST' });
  }
  classrooms(offset: number, limit: number): Promise<SchoolClassroomPage> {
    return this.gateway.request('identity', `/admin/classrooms?offset=${offset}&limit=${limit}`);
  }
  createClassroom(name: string, teacherId: string): Promise<Classroom> {
    return this.gateway.request('identity', '/admin/classrooms', {
      method: 'POST',
      body: { name, teacher_id: teacherId },
    });
  }
  /* renames the classroom and gives it its teacher, who then has the classroom's teacher access. */
  updateClassroom(classroomId: string, name: string, teacherId: string): Promise<{ classroom_id: string }> {
    return this.gateway.request('identity', `/admin/classrooms/${id(classroomId)}`, {
      method: 'PATCH',
      body: { name, teacher_id: teacherId },
    });
  }
  deleteClassroom(classroomId: string): Promise<{ classroom_id: string }> {
    return this.gateway.request('identity', `/admin/classrooms/${id(classroomId)}`, { method: 'DELETE' });
  }
  rosterPage(classroomId: string, offset: number, limit: number): Promise<StudentPage> {
    const query = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    return this.gateway.request('identity', `/admin/classrooms/${id(classroomId)}/students?${query}`);
  }
  /* the ids of every student enrolled in the classroom. */
  rosterIds(classroomId: string): Promise<string[]> {
    return this.gateway.request('identity', `/admin/classrooms/${id(classroomId)}/student-ids`);
  }
  enroll(classroomId: string, studentId: string): Promise<Enrollment> {
    return this.gateway.request('identity', `/admin/classrooms/${id(classroomId)}/enrollments`, {
      method: 'POST',
      body: { student_id: studentId },
    });
  }
  unenroll(classroomId: string, studentId: string): Promise<Enrollment> {
    return this.gateway.request(
      'identity',
      `/admin/classrooms/${id(classroomId)}/enrollments/${id(studentId)}`,
      { method: 'DELETE' },
    );
  }
}

@Injectable({ providedIn: 'root' })
export class CurriculumApi {
  private readonly gateway = inject(Gateway);

  /* all active topics for the teacher's native pickers. */
  topics(): Promise<Topic[]> {
    return allPages((offset, limit) => this.topicPage(offset, limit));
  }
  topicPage(offset: number, limit: number, archived = false): Promise<TopicPage> {
    return this.gateway.request(
      'curriculum',
      `/topics/page?offset=${offset}&limit=${limit}&archived=${archived}`,
    );
  }
  topicNames(ids: string[]): Promise<Topic[]> {
    return this.gateway.request('curriculum', '/topics/batch', { method: 'POST', body: { ids } });
  }
  createTopic(topic: { name: string }): Promise<Topic> {
    return this.gateway.request('curriculum', '/topics', { method: 'POST', body: topic });
  }
  /* archived topics for curriculum management and picker validation. */
  archivedTopics(): Promise<Topic[]> {
    return allPages((offset, limit) => this.topicPage(offset, limit, true));
  }
  /* hides the topic from every list and from new questions; nothing is deleted. */
  archiveTopic(topicId: string): Promise<void> {
    return this.gateway.request('curriculum', `/topics/${id(topicId)}`, { method: 'DELETE' });
  }
  /* makes an archived topic active again. */
  restoreTopic(topicId: string): Promise<Topic> {
    return this.gateway.request('curriculum', `/topics/${id(topicId)}/restore`, { method: 'POST' });
  }
}

/* long enough for a 50 MiB pdf at about 1.5 mbit/s; other calls keep the default timeout. */
const UPLOAD_TIMEOUT = 5 * 60 * 1000;

@Injectable({ providedIn: 'root' })
export class ContentApi {
  private readonly gateway = inject(Gateway);

  documents(classroomId: string): Promise<DocumentRecord[]> {
    return allPages((offset, limit) => this.documentPage(classroomId, offset, limit));
  }
  documentPage(classroomId: string, offset: number, limit: number): Promise<DocumentPage> {
    return this.gateway.request(
      'content',
      `/documents/page?classroom_id=${id(classroomId)}&offset=${offset}&limit=${limit}`,
    );
  }
  /* uploads a teaching material, the only kind of document teachers add (adr 0009). */
  upload(classroomId: string, title: string, file: File): Promise<DocumentRecord> {
    const query = `classroom_id=${id(classroomId)}&title=${id(title)}`;
    return this.gateway.request('content', `/documents?${query}`, {
      method: 'POST',
      body: file,
      contentType: documentMime(file) || 'application/octet-stream',
      timeout: UPLOAD_TIMEOUT,
    });
  }
  reindex(documentId: string): Promise<DocumentRecord> {
    return this.gateway.request('content', `/documents/${id(documentId)}/reindex`, {
      method: 'POST',
      body: {},
    });
  }
  /* deletes the document for good, with its passages and stored files, for the classroom's teacher. */
  remove(documentId: string): Promise<void> {
    return this.gateway.request('content', `/documents/${id(documentId)}`, { method: 'DELETE' });
  }
  /* object urls the caller revokes. */
  figure(documentId: string, image: string): Promise<string> {
    return this.gateway.objectUrl('content', `/documents/${id(documentId)}/figures/${id(image)}`);
  }
  original(documentId: string): Promise<string> {
    return this.gateway.objectUrl('content', `/documents/${id(documentId)}/original`);
  }
}

@Injectable({ providedIn: 'root' })
export class AssessmentApi {
  private readonly gateway = inject(Gateway);

  /* the classroom's 100 newest assessments, each saying whether the signed-in user has submitted it. */
  list(classroomId: string): Promise<AssessmentSummary[]> {
    return allPages((offset, limit) => this.assessmentPage(classroomId, offset, limit));
  }
  assessmentPage(classroomId: string, offset: number, limit: number): Promise<AssessmentPage> {
    return this.gateway.request(
      'assessment',
      `/assessments/page?classroom_id=${id(classroomId)}&offset=${offset}&limit=${limit}`,
    );
  }
  /* for the classroom's teacher: complete progress metadata read through bounded pages. */
  progress(classroomId: string): Promise<AssessmentProgress[]> {
    return allPages((offset, limit) =>
      this.gateway.request<{ items: AssessmentProgress[]; total: number }>(
        'assessment',
        `/assessments/progress/page?classroom_id=${id(classroomId)}&offset=${offset}&limit=${limit}`,
      ),
    );
  }
  /* with answer keys for the classroom's teacher only. */
  private readonly queued = new Map<
    string,
    { promise: Promise<Assessment>; resolve: (value: Assessment) => void; reject: (error: unknown) => void }
  >();

  get(assessmentId: string): Promise<Assessment> {
    const existing = this.queued.get(assessmentId);
    if (existing) return existing.promise;
    let resolve!: (value: Assessment) => void;
    let reject!: (error: unknown) => void;
    const promise = new Promise<Assessment>((yes, no) => {
      resolve = yes;
      reject = no;
    });
    const pending = { promise, resolve, reject };
    if (!this.queued.size) queueMicrotask(() => void this.flushReads());
    this.queued.set(assessmentId, pending);
    return pending.promise;
  }

  private async flushReads(): Promise<void> {
    const entries = [...this.queued];
    this.queued.clear();
    // bound request count as well as each batch's size; no unbounded Promise.all fan-out.
    for (let start = 0; start < entries.length; start += 50) {
      const batch = entries.slice(start, start + 50);
      try {
        const items =
          batch.length === 1
            ? [await this.gateway.request<Assessment>('assessment', `/assessments/${id(batch[0][0])}`)]
            : await this.gateway.request<Assessment[]>('assessment', '/assessments/batch', {
                method: 'POST',
                body: { ids: batch.map(([key]) => key) },
              });
        const byId = new Map(items.map((item) => [item.id, item]));
        for (const [key, pending] of batch) {
          const item = byId.get(key);
          if (item) pending.resolve(item);
          else pending.reject(new Error('The assessment could not be loaded.'));
        }
      } catch (error) {
        for (const [, pending] of batch) pending.reject(error);
      }
    }
  }
  /* creates a draft, or publishes at once when its status says so. */
  create(assessment: NewAssessment): Promise<Assessment> {
    return this.gateway.request('assessment', '/assessments', { method: 'POST', body: assessment });
  }
  /* replaces an assessment until a student submits it; a published one stays published. */
  update(assessmentId: string, assessment: NewAssessment): Promise<Assessment> {
    return this.gateway.request('assessment', `/assessments/${id(assessmentId)}`, {
      method: 'PUT',
      body: assessment,
    });
  }
  publish(assessmentId: string): Promise<Assessment> {
    return this.gateway.request('assessment', `/assessments/${id(assessmentId)}/publish`, {
      method: 'POST',
      body: {},
    });
  }
  /* changes the title, due date and late policy, also after students have submitted. */
  details(assessmentId: string, details: AssessmentDetails): Promise<Assessment> {
    return this.gateway.request('assessment', `/assessments/${id(assessmentId)}`, {
      method: 'PATCH',
      body: details,
    });
  }
  /* deletes an assessment. */
  remove(assessmentId: string, withSubmissions = false): Promise<{ id: string }> {
    const query = withSubmissions ? '?with_submissions=true' : '';
    return this.gateway.request('assessment', `/assessments/${id(assessmentId)}${query}`, {
      method: 'DELETE',
    });
  }
  /* the signed-in student's own submitted answers; a 404 until they submit. */
  submission(assessmentId: string): Promise<OwnSubmission> {
    return this.gateway.request('assessment', `/assessments/${id(assessmentId)}/submission`);
  }
  /* the signed-in student's unsubmitted work. */
  saved(assessmentId: string): Promise<SavedAnswers> {
    return this.gateway.request('assessment', `/assessments/${id(assessmentId)}/saved-answers`);
  }
  /* replaces the signed-in student's unsubmitted work and returns what is now saved. */
  save(assessmentId: string, answers: Record<string, string>): Promise<SavedAnswers> {
    return this.gateway.request('assessment', `/assessments/${id(assessmentId)}/saved-answers`, {
      method: 'PUT',
      body: { answers },
    });
  }
  submit(assessmentId: string, answers: Record<string, string>): Promise<Submission> {
    return this.gateway.request('assessment', `/assessments/${id(assessmentId)}/submissions`, {
      method: 'POST',
      body: { answers },
    });
  }

  /* the teacher's own copy, read on its own (never batched), so each question carries its bank link. */
  editable(assessmentId: string): Promise<Assessment> {
    return this.gateway.request('assessment', `/assessments/${id(assessmentId)}`);
  }
  /* a draft resit of a published assessment for chosen students (adr 0017). */
  createResit(assessmentId: string, request: ResitRequest): Promise<Assessment> {
    return this.gateway.request('assessment', `/assessments/${id(assessmentId)}/resits`, {
      method: 'POST',
      body: request,
    });
  }
  /* replaces a resit's students; one who has submitted stays. */
  setAssignees(assessmentId: string, studentIds: string[]): Promise<Assessment> {
    return this.gateway.request('assessment', `/assessments/${id(assessmentId)}/assignees`, {
      method: 'PUT',
      body: { student_ids: studentIds },
    });
  }

  /* one page of the teacher's bank, newest change first. */
  bankPage(filter: BankFilter, offset: number, limit: number): Promise<BankQuestionPage> {
    const query = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    if (filter.topicId) query.set('topic_id', filter.topicId);
    if (filter.type) query.set('type', filter.type);
    if (filter.text?.trim()) query.set('q', filter.text.trim());
    if (filter.classroomId && filter.studentId) {
      query.set('classroom_id', filter.classroomId);
      query.set('student_id', filter.studentId);
      if (filter.excludeAnswered) query.set('exclude_answered', 'true');
    }
    return this.gateway.request('assessment', `/bank/questions?${query.toString()}`);
  }
  /* one of the teacher's bank questions. */
  bankQuestion(bankQuestionId: string): Promise<BankQuestion> {
    return this.gateway.request('assessment', `/bank/questions/${id(bankQuestionId)}`);
  }
  /* saves an assessment's question to the teacher's bank and links the two. */
  saveToBank(assessmentId: string, questionId: string): Promise<BankQuestion> {
    return this.gateway.request('assessment', '/bank/questions', {
      method: 'POST',
      body: { assessment_id: assessmentId, question_id: questionId },
    });
  }
  /* keeps a question the teacher wrote, or a copy of one, in their bank. */
  addToBank(question: BankQuestionInput): Promise<BankQuestion> {
    return this.gateway.request('assessment', '/bank/questions', { method: 'POST', body: { question } });
  }
  updateBankQuestion(bankQuestionId: string, question: BankQuestionInput): Promise<BankQuestion> {
    return this.gateway.request('assessment', `/bank/questions/${id(bankQuestionId)}`, {
      method: 'PUT',
      body: question,
    });
  }
  removeBankQuestion(bankQuestionId: string): Promise<void> {
    return this.gateway.request('assessment', `/bank/questions/${id(bankQuestionId)}`, { method: 'DELETE' });
  }
}

@Injectable({ providedIn: 'root' })
export class GradingApi {
  private readonly gateway = inject(Gateway);

  /* one page (at most 100) of the classroom's evaluations with students' answers, newest first, or of one assessment's, oldest first, and how many there are. */
  evaluations(
    classroomId: string,
    assessmentId: string,
    offset: number,
    limit: number,
  ): Promise<EvaluationPage> {
    const query = new URLSearchParams({
      classroom_id: classroomId,
      offset: String(offset),
      limit: String(limit),
    });
    if (assessmentId) query.set('assessment_id', assessmentId);
    return this.gateway.request('grading', `/evaluations?${query}`);
  }
  /* one evaluation with its student's answers, for the classroom's teacher. */
  evaluation(evaluationId: string): Promise<Evaluation> {
    return this.gateway.request('grading', `/evaluations/${id(evaluationId)}`);
  }
  /* for the classroom's teacher: how many grades are drafts waiting for review, how many are final and how many of those are not released, in the classroom or in one assessment. */
  counts(classroomId: string, assessmentId = ''): Promise<GradingCounts> {
    const query = new URLSearchParams({ classroom_id: classroomId });
    if (assessmentId) query.set('assessment_id', assessmentId);
    return this.gateway.request('grading', `/evaluations/counts?${query}`);
  }
  /* the signed-in student's released results in the classroom; drafts are never returned. */
  mine(classroomId: string): Promise<OwnResult[]> {
    return this.gateway.request('grading', `/evaluations/mine?classroom_id=${id(classroomId)}`);
  }
  /* one page of the signed-in student's released results, newest first, with how many there are. */
  minePage(
    classroomId: string,
    offset: number,
    limit: number,
  ): Promise<{ items: OwnResult[]; total: number }> {
    return this.gateway.request(
      'grading',
      `/evaluations/mine/page?classroom_id=${id(classroomId)}&offset=${offset}&limit=${limit}`,
    );
  }
  /* saves the teacher's marks on a draft without finalizing it. */
  mark(evaluationId: string, marks: Record<string, Mark>): Promise<Evaluation> {
    return this.gateway.request('grading', `/evaluations/${id(evaluationId)}/marks`, {
      method: 'POST',
      body: { marks },
    });
  }
  /* finalizes a draft, with any last marks; refused while a question still needs marking. */
  finalize(evaluationId: string, reason: string, marks: Record<string, Mark> = {}): Promise<Evaluation> {
    return this.gateway.request('grading', `/evaluations/${id(evaluationId)}/finalize`, {
      method: 'POST',
      body: { reason, marks },
    });
  }
  /* finalizes every enrolled student's draft of an assessment with nothing left to mark. */
  finalizeAll(assessmentId: string): Promise<BulkFinalized> {
    return this.gateway.request('grading', '/evaluations/finalize', {
      method: 'POST',
      body: { assessment_id: assessmentId },
    });
  }
  /* turns a finalized grade back into a draft to correct it; its student no longer sees it until it is finalized again. */
  reopen(evaluationId: string, reason: string): Promise<Evaluation> {
    return this.gateway.request('grading', `/evaluations/${id(evaluationId)}/reopen`, {
      method: 'POST',
      body: { reason },
    });
  }
  /* shows an assessment's finalized grades to its enrolled students; a removed student's stay hidden. */
  release(assessmentId: string): Promise<{ assessment_id: string; released: number }> {
    return this.gateway.request('grading', '/evaluations/release', {
      method: 'POST',
      body: { assessment_id: assessmentId },
    });
  }
}

@Injectable({ providedIn: 'root' })
export class AnalyticsApi {
  private readonly gateway = inject(Gateway);

  performance(studentId: string, classroomId: string): Promise<Performance> {
    return this.gateway.request('analytics', `/performance/${id(studentId)}?classroom_id=${id(classroomId)}`);
  }
  /* for the classroom's teacher: one page of its enrolled students with their finalized topic evidence. */
  classPerformance(classroomId: string, offset: number, limit: number): Promise<ClassPerformance> {
    const query = new URLSearchParams({
      classroom_id: classroomId,
      offset: String(offset),
      limit: String(limit),
    });
    return this.gateway.request('analytics', `/performance?${query}`);
  }
}

@Injectable({ providedIn: 'root' })
export class ChatApi {
  private readonly gateway = inject(Gateway);

  threads(classroomId: string): Promise<{ threads: Thread[] }> {
    return this.gateway.request('chatbot', `/threads?classroom_id=${id(classroomId)}`);
  }
  createThread(classroomId: string): Promise<Thread> {
    return this.gateway.request('chatbot', '/threads', {
      method: 'POST',
      body: { classroom_id: classroomId },
    });
  }
  messages(threadId: string): Promise<{ messages: ChatMessage[] }> {
    return this.gateway.request('chatbot', `/threads/${id(threadId)}/messages`);
  }
  /* sends a question and passes each event of the streamed answer to `onEvent`. */
  ask(
    threadId: string,
    content: string,
    signal: AbortSignal,
    onEvent: (event: StreamEvent) => void,
  ): Promise<void> {
    return this.gateway.stream('chatbot', `/threads/${id(threadId)}/messages`, { content }, signal, onEvent);
  }
  /* the signed-in user's ai spend today. */
  usage(): Promise<Usage> {
    return this.gateway.request('chatbot', '/usage');
  }
}

/* generation reads the documents, waits for the model and its reviewer, and retries; kong allows it five minutes. */
const GENERATION_TIMEOUT = 5 * 60 * 1000 + 10 * 1000;

/* drafts from the classroom's documents for its teacher (adr 0006), and practice for its students (adr 0022). */
@Injectable({ providedIn: 'root' })
export class QuestionGenerationApi {
  private readonly gateway = inject(Gateway);

  generate(request: GenerateRequest): Promise<GenerateResult> {
    return this.gateway.request('question-generation', '/questions', {
      method: 'POST',
      body: request,
      timeout: GENERATION_TIMEOUT,
    });
  }
  /* accepted answers or a rubric, and marking points, for a question the teacher wrote. */
  guidance(request: GuidanceRequest): Promise<Guidance> {
    return this.gateway.request('question-generation', '/marking-guidance', {
      method: 'POST',
      body: request,
      timeout: GENERATION_TIMEOUT,
    });
  }
  /* reviewed practice questions with their keys, for the signed-in student's own use; nothing is saved. */
  practice(request: PracticeRequest): Promise<PracticeResult> {
    return this.gateway.request('question-generation', '/practice', {
      method: 'POST',
      body: request,
      timeout: GENERATION_TIMEOUT,
    });
  }
}

/* the agent may take several model turns; kong allows it five minutes. */
const INTERPRETATION_TIMEOUT = 5 * 60 * 1000 + 10 * 1000;

/* the performance agent's drafts for the classroom's teacher: one student's interpretation (adr 0014) and class insights (adr 0021). */
@Injectable({ providedIn: 'root' })
export class PerformanceAgentApi {
  private readonly gateway = inject(Gateway);

  interpret(classroomId: string, studentId: string): Promise<Interpretation> {
    return this.gateway.request('performance-agent', '/performance_interpretation', {
      method: 'POST',
      body: { classroom_id: classroomId, student_id: studentId },
      timeout: INTERPRETATION_TIMEOUT,
    });
  }

  /* a checked answer to the teacher's question about their class (adr 0021); the same loop and time limit. */
  classInsights(request: ClassInsightsRequest): Promise<ClassInsights> {
    return this.gateway.request('performance-agent', '/class_insights', {
      method: 'POST',
      body: request,
      timeout: INTERPRETATION_TIMEOUT,
    });
  }
}
