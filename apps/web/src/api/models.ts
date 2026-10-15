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
export interface Node {
  id: string;
  kind: string;
  name: string;
  parent_id: string | null;
}
export type DocType = 'teaching_material' | 'rubric' | 'marking_scheme';
export type DocumentStatus = 'extracted' | 'extraction_unsupported' | 'indexing' | 'indexed' | 'failed';
export interface DocumentRecord {
  id: string;
  title: string;
  mime_type: string;
  status: DocumentStatus;
  revision: number;
  doc_type: DocType;
  page_count?: number;
  chunk_count?: number;
  indexed_at?: string;
  failure_reason?: string;
}
export interface Thread {
  id: string;
  classroom_id: string;
  role: 'student' | 'teacher';
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
export interface NodeInput {
  kind: string;
  name: string;
  parent_id: string | null;
}
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
  evidence_status: string;
  /** Five or more attempts under 60% correct. */
  weakness_indicator: boolean;
}
export interface Performance {
  evidence_status: string;
  interpretation: string;
  skills: SkillMetric[];
}
/** One page of a classroom's enrolled students, in roster order, each with their finalized skill evidence. */
export interface ClassPerformance {
  total: number;
  offset: number;
  students: { student_id: string; skills: SkillMetric[]; evidence_status: string }[];
  interpretation: string;
}
/** How many students have submitted an assessment. */
export interface AssessmentProgress {
  id: string;
  title: string;
  submissions: number;
}
/** How many of a classroom's grades wait for its teacher, and how many are final. */
export interface GradingCounts {
  drafts: number;
  finalized: number;
}
