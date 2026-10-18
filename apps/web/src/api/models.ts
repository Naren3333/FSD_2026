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
/** A curriculum topic: the single level questions and evidence are tagged with. */
export interface Topic {
  id: string;
  name: string;
}
/** Teachers upload teaching material only; rubrics and marking schemes uploaded before ADR 0009 keep their type and
 * stay teacher-only. */
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
/** One of the user's conversations with the classroom assistant. */
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
  /** The material was deleted after this answer: the passage and figure are gone and only the title is kept. */
  removed?: boolean;
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
/** A passage on a question: a marking point for AI marking suggestions only, or a source of a generated question.
 * A source names its document, chunk and page; a marking point written by the teacher or AI names none (one copied
 * from an uploaded marking scheme before ADR 0009 may still name its document). */
export interface Excerpt {
  document_id?: string;
  chunk_id?: string;
  page?: number;
  text: string;
}
/** A question. Answer keys, accepted answers, rubrics and marking points come only to the classroom's
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
  /** Short answers and free responses only: up to five marking points. */
  marking_scheme?: Excerpt[];
  /** A free response's character limit. */
  max_length?: number;
  /** For a free response, the rubric's total. */
  points: number;
  topic_id: string;
  /** Generated questions only: the classroom passages the question was drawn from (ADR 0006). Teacher only. */
  sources?: Excerpt[];
  origin?: 'generated';
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
/** What a teacher sends to create or replace an assessment; Assessment assigns the ids. */
export interface NewAssessment extends AssessmentSettings {
  classroom_id: string;
  title: string;
  questions: Omit<Question, 'id'>[];
}
/** What a teacher may still change once students have submitted: nothing their grades depend on. */
export interface AssessmentDetails {
  title: string;
  due_at: string | null;
  late_policy: LatePolicy;
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
  /** How many of the question's marking points the AI was given. */
  scheme_passages?: number;
}
export interface Evidence {
  question_id: string;
  topic_id: string;
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
export interface TopicMetric {
  topic_id: string;
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
  topics: TopicMetric[];
}
/** One page of a classroom's enrolled students, in roster order, each with their finalized topic evidence. */
export interface ClassPerformance {
  classroom_id: string;
  total: number;
  offset: number;
  students: { student_id: string; topics: TopicMetric[]; evidence_status: PerformanceStatus }[];
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
  /** How many students still enrolled have submitted it; for the classroom's teacher only. */
  submissions?: number;
  /** Whether anyone has submitted it, students since removed from the classroom included, so its questions are fixed
   * and deleting it takes their grades; for the classroom's teacher only. */
  has_submissions?: boolean;
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
/** The question types the agent generates (ADR 0006). */
export type GeneratableType = 'multiple_choice' | 'short_answer' | 'free_response';
export type Difficulty = 'introductory' | 'intermediate' | 'advanced';
/** What a teacher asks the question-generation agent for. */
export interface GenerateRequest {
  classroom_id: string;
  document_ids: string[];
  topic_id: string;
  question_types: GeneratableType[];
  difficulty: Difficulty;
  count: number;
  focus?: string;
  /** The prompts the editor already holds, so near-duplicates are dropped. */
  existing_prompts?: string[];
}
/** A drafted question, with the quote from its sources that supports its key. Never saved until the teacher keeps it. */
export interface GeneratedQuestion {
  question: Omit<Question, 'id'>;
  evidence: string;
  explanation: string;
}
export interface GenerateResult {
  questions: GeneratedQuestion[];
  requested: number;
  returned: number;
  /** Drafts the checks refused, by reason. */
  dropped: Record<string, number>;
  requires_teacher_review: true;
}
/** Marking guidance for a short answer or free response the teacher wrote. */
export interface GuidanceRequest {
  classroom_id: string;
  document_ids: string[];
  prompt: string;
  type: 'short_answer' | 'free_response';
  points?: number;
}
export interface Guidance {
  type: 'short_answer' | 'free_response';
  accepted_answers: string[];
  rubric: Rubric | null;
  /** What earns each mark, as a marking point (no document). */
  marking_points: Excerpt;
  sources: Excerpt[];
  evidence: string;
  requires_teacher_review: true;
}
