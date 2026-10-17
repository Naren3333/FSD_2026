/** The shapes of the backend APIs, as the web app reads and sends them. */

export interface Profile {
  id: string;
  org_id: string;
  role: 'teacher' | 'student';
  /** Empty when the sign-in token carries no username. */
  username: string;
  display_name: string;
}
/** An enrolled student as the classroom's roster shows them; names are empty until they sign in again. */
export interface Student {
  id: string;
  username: string;
  display_name: string;
}
export interface Classroom {
  id: string;
  name: string;
  teacher_id: string;
}
/** Curriculum levels, top down; each node's parent is the level above it. */
export type NodeKind = 'subject' | 'course' | 'unit' | 'topic' | 'skill' | 'objective';
export interface Node {
  id: string;
  kind: NodeKind;
  name: string;
  parent_id: string | null;
}
export type DocType = 'teaching_material' | 'rubric' | 'marking_scheme';
export type DocumentStatus = 'extracted' | 'extraction_unsupported' | 'indexing' | 'indexed' | 'failed';
/** Why indexing a PDF failed. */
export type FailureReason =
  | 'NO_TEXT_LAYER'
  | 'ENCRYPTED'
  | 'CORRUPT'
  | 'TOO_LARGE'
  | 'AI_UNAVAILABLE'
  | 'INTERNAL';
export interface DocumentRecord {
  id: string;
  classroom_id: string;
  title: string;
  mime_type: 'text/plain' | 'application/pdf';
  status: DocumentStatus;
  revision: number;
  doc_type: DocType;
  page_count?: number;
  chunk_count?: number;
  indexed_at?: string;
  failure_reason?: FailureReason;
}
/** One passage Content's search found, with the document it is from. */
export interface SearchHit {
  chunk_id: string;
  document_id: string;
  title: string;
  doc_type: DocType;
  section_path: string | null;
  page_start: number;
  page_end: number;
  content: string;
}
export interface SearchRequest {
  classroom_id: string;
  query: string;
  top_k?: number;
  document_ids?: string[];
  doc_types?: DocType[];
}
export interface Thread {
  id: string;
  classroom_id: string;
  role: Profile['role'];
  title: string | null;
  created_at: string;
  last_message_at: string;
}
export interface Citation {
  chunk_id: string;
  document_id: string;
  title: string;
  page_start: number;
  page_end: number;
  kind: 'text' | 'table' | 'figure';
  source: 'pdf' | 'vision';
  content: string;
  image: string | null;
}
export type Outcome = 'answered' | 'no_results' | 'error' | 'interrupted';
export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  outcome: Outcome | null;
  citations: Citation[] | null;
  created_at: string;
}
export interface StreamEvent {
  event: string;
  data: Record<string, unknown>;
}
/** The fields a new node needs: its level, name and the node one level above it. */
export type NodeInput = Omit<Node, 'id'>;
export type QuestionType = 'numerical' | 'multiple_choice' | 'short_answer' | 'free_response';
/** One level of a rubric criterion: its points and what work at that level looks like. */
export interface Level {
  points: number;
  descriptor: string;
}
export interface Criterion {
  /** Assigned by Assessment (c1, c2, …). */
  id?: string;
  title: string;
  levels: Level[];
}
/** How a free response is marked; for the classroom's teacher only. */
export interface Rubric {
  criteria: Criterion[];
  model_answer?: string;
}
/** A passage of the classroom's marking scheme the teacher attached to a question, for AI marking suggestions only.
 * Picked from search it names its document, chunk and page; pasted by the teacher it may name none. */
export interface Excerpt {
  document_id?: string;
  chunk_id?: string;
  page?: number;
  text: string;
}
/** A question. Answer keys, accepted answers, rubrics and marking-scheme passages come only to the classroom's
 * teacher. */
export interface Question {
  /** Assigned by Assessment. */
  id: string;
  prompt: string;
  type: QuestionType;
  /** Multiple choice only; null or empty for the other types. */
  options: string[] | null;
  answer?: string;
  tolerance?: string;
  accepted_answers?: string[];
  case_sensitive?: boolean;
  rubric?: Rubric;
  /** Short answers and free responses only: up to five passages. */
  marking_scheme?: Excerpt[];
  /** A free response's character limit. */
  max_length?: number;
  /** For a free response, the rubric's total. */
  points: number;
  skill_id: string;
}
export type AssessmentStatus = 'draft' | 'published';
/** Late work is accepted and flagged for the teacher, or refused after the due date. */
export type LatePolicy = 'accept_flagged' | 'reject';
/** Students see a finalized grade at once, or when the teacher releases the assessment's results. */
export type ReleasePolicy = 'on_finalize' | 'manual';
/** An assessment's dates and policies, as its teacher sets them. */
export interface AssessmentSettings {
  status: AssessmentStatus;
  opens_at: string | null;
  due_at: string | null;
  late_policy: LatePolicy;
  release_policy: ReleasePolicy;
  ai_marking: boolean;
}
/** An assessment. Students get only the questions, dates and late policy. */
export interface Assessment extends Partial<AssessmentSettings> {
  id: string;
  classroom_id: string;
  title: string;
  questions: Question[];
  published_at?: string | null;
}
/** What a teacher sends to create or replace a draft; Assessment assigns the ids. */
export interface NewAssessment extends AssessmentSettings {
  classroom_id: string;
  title: string;
  questions: Omit<Question, 'id'>[];
}
/** A recorded submission; its grade starts as a draft for the teacher. */
export interface Submission {
  id: string;
  status: 'submitted';
  /** Submitted after the due date. */
  late: boolean;
}
/** A student's unsubmitted work on an assessment. */
export interface SavedAnswers {
  answers: Record<string, string>;
  saved_at: string | null;
}
export interface CriterionScore {
  criterion_id: string;
  points: number;
}
/** An AI-suggested mark for a question the teacher marks. Never a grade: the teacher accepts, edits or ignores it. */
export interface Suggestion {
  status: 'pending' | 'ready' | 'failed';
  criteria?: {
    criterion_id: string;
    points: number;
    evidence_quotes: string[];
    rationale: string;
  }[];
  awarded: number;
  feedback?: string;
  confidence?: 'low' | 'medium' | 'high';
  flags?: string[];
  model?: string;
  /** Why it failed or still waits, such as the teacher's daily AI allowance. */
  reason?: string;
  /** How many of the question's marking-scheme passages the AI was given. */
  scheme_passages?: number;
}
export interface Evidence {
  question_id: string;
  skill_id: string;
  correct: boolean;
  awarded: number;
  possible: number;
  /** The teacher still has to mark this question. */
  needs_marking?: boolean;
  marked_by?: 'auto' | 'teacher';
  criteria?: CriterionScore[];
  feedback?: string;
  suggestion?: Suggestion;
}
export interface Evaluation {
  id: string;
  assessment_id: string;
  classroom_id: string;
  student_id: string;
  status: 'draft' | 'finalized';
  evidence: Evidence[];
  answers: Record<string, string>;
  late: boolean;
  submitted_at?: string | null;
  release_policy?: ReleasePolicy;
  finalized_at?: string | null;
  released_at?: string | null;
}
/** The teacher's marking of one question: a level for each rubric criterion, or the points awarded. */
export interface Mark {
  criteria?: Record<string, number>;
  awarded?: number;
  feedback?: string;
}
/** The signed-in user's AI spend today against their daily allowance (ADR 0003). */
export interface Usage {
  spent_usd: number;
  limit_usd: number;
  resets_at: string;
}
export interface SkillMetric {
  skill_id: string;
  attempted: number;
  /** Questions answered with full marks. */
  correct: number;
  observed_accuracy: number;
  /** Marks earned out of marks possible, which shows part marks. */
  marks_awarded?: number;
  marks_possible?: number;
  /** Fewer than five attempts is too few to judge. */
  evidence_status: 'insufficient_evidence' | 'observed';
  /** Five or more attempts under 60% correct. */
  weakness_indicator: boolean;
}
/** Whether a student has any finalized evidence in the classroom yet. */
export type PerformanceStatus = 'no_finalized_evidence' | 'available';
export interface Performance {
  student_id: string;
  classroom_id: string;
  evidence_status: PerformanceStatus;
  interpretation: string;
  skills: SkillMetric[];
}
/** One page of a classroom's enrolled students, in roster order, each with their finalized skill evidence. */
export interface ClassPerformance {
  classroom_id: string;
  total: number;
  offset: number;
  students: { student_id: string; skills: SkillMetric[]; evidence_status: PerformanceStatus }[];
  interpretation: string;
}
/** An assessment in a classroom's list, with whether the signed-in user has submitted it. */
export interface AssessmentSummary {
  id: string;
  title: string;
  submitted: boolean;
  status: AssessmentStatus;
  opens_at: string | null;
  due_at: string | null;
  late_policy: LatePolicy;
  question_count: number;
  points: number;
}
/** How many students have submitted an assessment. */
export interface AssessmentProgress {
  id: string;
  title: string;
  submissions: number;
  status: AssessmentStatus;
  due_at: string | null;
}
/** A classroom enrollment, as Identity confirms adding or removing it. */
export interface Enrollment {
  student_id: string;
  classroom_id: string;
}
/** How many of a classroom's grades wait for its teacher, and how many are final. */
export interface GradingCounts {
  drafts: number;
  finalized: number;
  /** Finalized grades students cannot see until the teacher releases them. */
  unreleased: number;
}
