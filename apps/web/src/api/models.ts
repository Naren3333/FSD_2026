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
export type FailureReason = 'NO_TEXT_LAYER' | 'ENCRYPTED' | 'CORRUPT' | 'AI_UNAVAILABLE' | 'INTERNAL';
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
export interface Question {
  id?: string;
  prompt: string;
  type: 'numerical' | 'multiple_choice';
  options: string[];
  answer?: string;
  tolerance?: string;
  skill_id: string;
}
export interface Assessment {
  id: string;
  classroom_id: string;
  title: string;
  questions: Question[];
}
/** What a teacher sends to create an assessment; Assessment assigns the ids. */
export interface NewAssessment {
  classroom_id: string;
  title: string;
  questions: Omit<Question, 'id'>[];
}
/** A recorded submission; its grade starts as a draft for the teacher. */
export interface Submission {
  id: string;
  status: 'submitted';
}
export interface Evidence {
  question_id: string;
  skill_id: string;
  correct: boolean;
  awarded: number;
  possible: number;
}
export interface Evaluation {
  id: string;
  assessment_id: string;
  classroom_id: string;
  student_id: string;
  status: 'draft' | 'finalized';
  evidence: Evidence[];
  /** On a student's own results only. */
  finalized_at?: string;
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
  correct: number;
  observed_accuracy: number;
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
}
/** How many students have submitted an assessment. */
export interface AssessmentProgress {
  id: string;
  title: string;
  submissions: number;
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
}
