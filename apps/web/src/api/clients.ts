import { Injectable, inject } from '@angular/core';
import { Gateway } from '../core/gateway';
import { isPdf } from '../shared/files';
import type {
  Assessment,
  AssessmentProgress,
  AssessmentSummary,
  ChatMessage,
  ClassPerformance,
  Classroom,
  DocType,
  DocumentRecord,
  Enrollment,
  Evaluation,
  GradingCounts,
  Node,
  Mark,
  NewAssessment,
  NodeInput,
  Performance,
  Profile,
  SavedAnswers,
  StreamEvent,
  Student,
  Submission,
  Thread,
  Usage,
} from './models';

/** One typed client per backend. Components call these, never the gateway with hand-built paths. */

const id = encodeURIComponent;

@Injectable({ providedIn: 'root' })
export class IdentityApi {
  private readonly gateway = inject(Gateway);

  /** Registers the signed-in user (idempotent) and returns their profile. */
  register(): Promise<Profile> {
    return this.gateway.request('identity', '/me', { method: 'POST', body: {} });
  }
  classrooms(): Promise<Classroom[]> {
    return this.gateway.request('identity', '/classrooms');
  }
  createClassroom(name: string): Promise<Classroom> {
    return this.gateway.request('identity', '/classrooms', { method: 'POST', body: { name } });
  }
  /** Deletes the classroom for its teacher. Its students lose access; its work is kept but no longer reachable. */
  deleteClassroom(classroomId: string): Promise<{ classroom_id: string }> {
    return this.gateway.request('identity', `/classrooms/${id(classroomId)}`, { method: 'DELETE' });
  }
  /** Enrolls one of the organization's students by account id. */
  enroll(classroomId: string, studentId: string): Promise<Enrollment> {
    return this.gateway.request('identity', `/classrooms/${id(classroomId)}/enrollments`, {
      method: 'POST',
      body: { student_id: studentId },
    });
  }
  unenroll(classroomId: string, studentId: string): Promise<Enrollment> {
    return this.gateway.request('identity', `/classrooms/${id(classroomId)}/enrollments/${id(studentId)}`, {
      method: 'DELETE',
    });
  }
  /** One page of the students in the teacher's organization who have signed in, by name, and how many match. */
  students(search: string, offset: number, limit: number): Promise<{ students: Student[]; total: number }> {
    const query = new URLSearchParams({ search, offset: String(offset), limit: String(limit) });
    return this.gateway.request('identity', `/students?${query}`);
  }
  /** The classroom's students, for its teacher only; up to 500. */
  roster(classroomId: string): Promise<Student[]> {
    return this.gateway.request('identity', `/classrooms/${id(classroomId)}/students`);
  }
}

@Injectable({ providedIn: 'root' })
export class CurriculumApi {
  private readonly gateway = inject(Gateway);

  nodes(): Promise<Node[]> {
    return this.gateway.request('curriculum', '/nodes');
  }
  createNode(node: NodeInput): Promise<Node> {
    return this.gateway.request('curriculum', '/nodes', { method: 'POST', body: node });
  }
}

/** Long enough for a 50 MiB PDF at about 1.5 Mbit/s; other calls keep the default timeout. */
const UPLOAD_TIMEOUT = 5 * 60 * 1000;

@Injectable({ providedIn: 'root' })
export class ContentApi {
  private readonly gateway = inject(Gateway);

  documents(classroomId: string): Promise<DocumentRecord[]> {
    return this.gateway.request('content', `/documents?classroom_id=${id(classroomId)}`);
  }
  upload(classroomId: string, title: string, docType: DocType, file: File): Promise<DocumentRecord> {
    const query = `classroom_id=${id(classroomId)}&title=${id(title)}&doc_type=${id(docType)}`;
    return this.gateway.request('content', `/documents?${query}`, {
      method: 'POST',
      body: file,
      contentType: isPdf(file) ? 'application/pdf' : 'text/plain',
      timeout: UPLOAD_TIMEOUT,
    });
  }
  reindex(documentId: string): Promise<DocumentRecord> {
    return this.gateway.request('content', `/documents/${id(documentId)}/reindex`, {
      method: 'POST',
      body: {},
    });
  }
  /** Object URLs the caller revokes. */
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

  /** The classroom's 100 newest assessments, each saying whether the signed-in user has submitted it. */
  list(classroomId: string): Promise<AssessmentSummary[]> {
    return this.gateway.request('assessment', `/assessments?classroom_id=${id(classroomId)}`);
  }
  /** For the classroom's teacher: its 100 newest assessments with how many students submitted each. */
  progress(classroomId: string): Promise<AssessmentProgress[]> {
    return this.gateway.request('assessment', `/assessments/progress?classroom_id=${id(classroomId)}`);
  }
  /** With answer keys for the classroom's teacher only. */
  get(assessmentId: string): Promise<Assessment> {
    return this.gateway.request('assessment', `/assessments/${id(assessmentId)}`);
  }
  /** Creates a draft, or publishes at once when its status says so. */
  create(assessment: NewAssessment): Promise<Assessment> {
    return this.gateway.request('assessment', '/assessments', { method: 'POST', body: assessment });
  }
  /** Replaces a draft; a published assessment cannot change. */
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
  /** Deletes a draft; published assessments are kept. */
  remove(assessmentId: string): Promise<{ id: string }> {
    return this.gateway.request('assessment', `/assessments/${id(assessmentId)}`, { method: 'DELETE' });
  }
  /** The signed-in student's unsubmitted work. */
  saved(assessmentId: string): Promise<SavedAnswers> {
    return this.gateway.request('assessment', `/assessments/${id(assessmentId)}/saved-answers`);
  }
  save(assessmentId: string, answers: Record<string, string>): Promise<{ saved_at: string }> {
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
}

@Injectable({ providedIn: 'root' })
export class GradingApi {
  private readonly gateway = inject(Gateway);

  /** The classroom's 100 newest evaluations, or all (up to 500) of one assessment's, with students' answers. */
  evaluations(classroomId: string, assessmentId = ''): Promise<Evaluation[]> {
    const query = new URLSearchParams({ classroom_id: classroomId });
    if (assessmentId) query.set('assessment_id', assessmentId);
    return this.gateway.request('grading', `/evaluations?${query}`);
  }
  /** For the classroom's teacher: how many grades are drafts waiting for review, and how many are final. */
  counts(classroomId: string): Promise<GradingCounts> {
    return this.gateway.request('grading', `/evaluations/counts?classroom_id=${id(classroomId)}`);
  }
  /** The signed-in student's released results in the classroom; drafts are never returned. */
  mine(classroomId: string): Promise<Evaluation[]> {
    return this.gateway.request('grading', `/evaluations/mine?classroom_id=${id(classroomId)}`);
  }
  /** Saves the teacher's marks on a draft without finalizing it. */
  mark(evaluationId: string, marks: Record<string, Mark>): Promise<Evaluation> {
    return this.gateway.request('grading', `/evaluations/${id(evaluationId)}/marks`, {
      method: 'POST',
      body: { marks },
    });
  }
  /** Finalizes a draft, with any last marks; refused while a question still needs marking. */
  finalize(evaluationId: string, reason: string, marks: Record<string, Mark> = {}): Promise<Evaluation> {
    return this.gateway.request('grading', `/evaluations/${id(evaluationId)}/finalize`, {
      method: 'POST',
      body: { reason, marks },
    });
  }
  /** Shows an assessment's finalized grades to its students. */
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
  /** For the classroom's teacher: one page of its enrolled students with their finalized skill evidence. */
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
  /** Sends a question and passes each event of the streamed answer to `onEvent`. */
  ask(
    threadId: string,
    content: string,
    signal: AbortSignal,
    onEvent: (event: StreamEvent) => void,
  ): Promise<void> {
    return this.gateway.stream('chatbot', `/threads/${id(threadId)}/messages`, { content }, signal, onEvent);
  }
  /** The signed-in user's AI spend today. */
  usage(): Promise<Usage> {
    return this.gateway.request('chatbot', '/usage');
  }
}
