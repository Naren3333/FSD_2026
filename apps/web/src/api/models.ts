/* the shapes of the backend apis, as the web app reads and sends them. */
import type { components as AnalyticsApi } from './generated/analytics';
import type { components as AssessmentApi } from './generated/assessment';
import type { components as ContentApi } from './generated/content';
import type { components as CurriculumApi } from './generated/curriculum';
import type { components as GradingApi } from './generated/grading';
import type { components as IdentityApi } from './generated/identity';
import type { components as ChatbotApi } from './generated/question-chatbot-agent';
import type { components as GenerationApi } from './generated/question-generation-agent';

type Identity = IdentityApi['schemas'];
type Curriculum = CurriculumApi['schemas'];
type Content = ContentApi['schemas'];
type Chatbot = ChatbotApi['schemas'];
type Assessments = AssessmentApi['schemas'];
type Grading = GradingApi['schemas'];
type Analytics = AnalyticsApi['schemas'];
type Generation = GenerationApi['schemas'];

export type Profile = Identity['Profile'];
/* an enrolled student as the classroom's roster shows them; names are empty until they sign in again. */
export type Student = Identity['Student'];
/* one page of students, and how many there are across every page. */
export type StudentPage = Identity['StudentPage'];
export type Classroom = Identity['Classroom'];
export type ClassroomPage = Identity['ClassroomPage'];
export type TopicPage = Curriculum['TopicPage'];
export type DocumentPage = Content['DocumentPage'];
export type AssessmentPage = Assessments['AssessmentPage'];
/* a classroom enrollment, as identity confirms adding or removing it. */
export type Enrollment = Identity['Enrollment'];

/* a curriculum topic: the single level questions and evidence are tagged with. */
export type Topic = Curriculum['Topic'];

export type DocumentRecord = Content['Document'];
/* teachers upload teaching material only; rubrics and marking schemes uploaded before adr 0009 keep their type and stay teacher-only. */
export type DocType = DocumentRecord['doc_type'];
export type DocumentStatus = DocumentRecord['status'];
/* why indexing a pdf failed. */
export type FailureReason = NonNullable<DocumentRecord['failure_reason']>;

/* one of the user's conversations with the classroom assistant. */
export type Thread = Chatbot['Thread'];
export type Citation = Chatbot['Citation'];
export type ChatMessage = Chatbot['Message'];
export type Outcome = NonNullable<ChatMessage['outcome']>;
/* one server-sent event of a streamed answer. */
export interface StreamEvent {
  event: string;
  data: Record<string, unknown>;
}
/* the signed-in user's ai spend today against their daily allowance (adr 0003). */
export type Usage = Chatbot['Usage'];

export type QuestionType = Assessments['Question']['type'];
/* one level of a rubric criterion: its points and what work at that level looks like. */
export type Level = Assessments['Level'];
/* how a free response is marked; for the classroom's teacher only. */
export type Rubric = Assessments['RubricInput'];
export type Criterion = Rubric['criteria'][number];
/* a passage on a question: a marking point for ai marking suggestions only, or a source of a generated question. */
export type Excerpt = Assessments['Excerpt'];
/* a question as a student gets it, with what only the classroom's teacher gets (answer keys, accepted answers, rubrics, marking points and generated questions' sources) when the teacher reads it. */
export type Question = Assessments['StudentQuestion'] &
  Partial<Omit<Assessments['Question'], keyof Assessments['StudentQuestion'] | 'rubric'>> & {
    rubric?: Rubric;
  };
export type AssessmentStatus = Assessments['Assessment']['status'];
/* late work is accepted and flagged for the teacher, or refused after the due date. */
export type LatePolicy = Assessments['Assessment']['late_policy'];
/* students see a finalized grade at once, or when the teacher releases the assessment's results. */
export type ReleasePolicy = Assessments['Assessment']['release_policy'];
/* an assessment's dates and policies, as its teacher sets them. */
export type AssessmentSettings = Pick<
  Assessments['Assessment'],
  'status' | 'opens_at' | 'due_at' | 'late_policy' | 'release_policy' | 'ai_marking'
>;
/* an assessment as a student gets it (questions, dates and late policy), with the rest of its settings when its teacher reads it. */
export type Assessment = Omit<Assessments['StudentAssessment'], 'questions'> & {
  questions: Question[];
} & Partial<Pick<Assessments['Assessment'], 'status' | 'release_policy' | 'ai_marking' | 'published_at'>>;
/* what a teacher sends to create or replace an assessment; assessment assigns the ids. */
export type NewAssessment = Omit<Assessments['AssessmentInput'], 'questions' | keyof AssessmentSettings> &
  AssessmentSettings & { questions: Omit<Question, 'id'>[] };
/* what a teacher may still change once students have submitted: nothing their grades depend on. */
export type AssessmentDetails = Required<Assessments['AssessmentDetails']>;
/* a recorded submission; its grade starts as a draft for the teacher. */
export type Submission = Assessments['SubmissionReceipt'];
/* a student's own submitted answers, as they read them again; never the answer keys. */
export type OwnSubmission = Assessments['OwnSubmission'];
/* a student's unsubmitted work on an assessment. */
export type SavedAnswers = Assessments['SavedAnswers'];
/* an assessment in a classroom's list, with whether the signed-in user has submitted it. */
export type AssessmentSummary = Assessments['AssessmentSummary'];
/* how many students have submitted an assessment. */
export type AssessmentProgress = Assessments['AssessmentProgress'];

export type CriterionScore = Grading['CriterionScore'];
/* an ai-suggested mark for a question the teacher marks. */
export type Suggestion = Grading['Suggestion'];
export type Evidence = Grading['EvaluationEvidence'];
export type Evaluation = Grading['Evaluation'];
/* a student's own released grade: marks and feedback, nothing the teacher still works on. */
export type OwnResult = Grading['OwnResult'];
/* the teacher's marking of one question: a level for each rubric criterion, or the points awarded. */
export type Mark = Grading['Mark'];
/* one page of a teacher's grades, and how many there are across every page. */
export type EvaluationPage = Grading['EvaluationPage'];
/* how many of a classroom's grades wait for its teacher, how many are final, and how many of those students cannot see until the teacher releases them. */
export type GradingCounts = Grading['GradingCounts'];

export type TopicMetric = Analytics['TopicMetric'];
/* whether a student has any finalized evidence in the classroom yet. */
export type PerformanceStatus = Analytics['StudentPerformance']['evidence_status'];
export type Performance = Analytics['PerformanceResponse'];
/* one page of a classroom's enrolled students, in roster order, each with their finalized topic evidence. */
export type ClassPerformance = Analytics['ClassPerformanceResponse'];

/* what a teacher asks the question-generation agent for (adr 0006). */
export type GenerateRequest = Generation['GenerateRequest'];
/* the question types the agent generates. */
export type GeneratableType = GenerateRequest['question_types'][number];
export type Difficulty = GenerateRequest['difficulty'];
/* a drafted question, with the quote from its sources that supports its key. */
export type GeneratedQuestion = Generation['Generated'];
/* a drafted question as the agent sends it, with null for what it leaves out; see draftquestion. */
export type DraftQuestion = Generation['DraftQuestion'];
/* a passage as the agent sends it, with null for a document, chunk or page it doesn't name; see fromagent. */
export type AgentExcerpt = Generation['Excerpt'];
export type GenerateResult = Generation['GenerateResponse'];
/* marking guidance for a short answer or free response the teacher wrote. */
export type GuidanceRequest = Generation['GuidanceRequest'];
export type Guidance = Generation['Guidance'];
