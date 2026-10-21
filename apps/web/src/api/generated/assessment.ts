// generated from contracts/http/assessment.openapi.json; run npm run contracts to refresh.

export type paths = Record<string, never>;
export type webhooks = Record<string, never>;
export interface components {
  schemas: {
    Assessment: {
      id: string;
      classroom_id: string;
      title: string;
      questions: components['schemas']['Question'][];
      status: 'draft' | 'published';
      opens_at: string | null;
      due_at: string | null;
      late_policy: 'accept_flagged' | 'reject';
      release_policy: 'on_finalize' | 'manual';
      ai_marking: boolean;
      published_at: string | null;
    };
    AssessmentDetails: {
      title?: string;
      due_at?: string | null;
      late_policy?: 'accept_flagged' | 'reject';
    };
    AssessmentInput: {
      classroom_id: string;
      title: string;
      questions: components['schemas']['QuestionInput'][];
      status?: 'draft' | 'published';
      opens_at?: string | null;
      due_at?: string | null;
      late_policy?: 'accept_flagged' | 'reject';
      release_policy?: 'on_finalize' | 'manual';
      ai_marking?: boolean;
    };
    AssessmentPage: {
      items: components['schemas']['AssessmentSummary'][];
      total: number;
    };
    AssessmentProgress: {
      id: string;
      title: string;
      submissions: number;
      status: 'draft' | 'published';
      due_at: string | null;
    };
    AssessmentSummary: {
      id: string;
      title: string;
      submitted: boolean;
      status: 'draft' | 'published';
      opens_at: string | null;
      due_at: string | null;
      late_policy: 'accept_flagged' | 'reject';
      question_count: number;
      points: number;
      submissions?: number;
      has_submissions?: boolean;
    };
    Criterion: {
      id: string;
      title: string;
      levels: components['schemas']['Level'][];
    };
    Error: {
      error: {
        code: string;
        message: string;
      };
    };
    Excerpt: {
      document_id?: string;
      chunk_id?: string;
      page?: number;
      text: string;
    };
    Level: {
      points: number;
      descriptor: string;
    };
    OwnSubmission: {
      answers: {
        [key: string]: string;
      };
      submitted_at: string;
      late: boolean;
    };
    Question: {
      id: string;
      prompt: string;
      type: 'multiple_choice' | 'numerical' | 'short_answer' | 'free_response';
      options: string[] | null;
      answer: string;
      tolerance: string;
      accepted_answers?: string[];
      case_sensitive?: boolean;
      rubric?: components['schemas']['Rubric'];
      max_length?: number;
      points: number;
      topic_id: string;
      marking_scheme?: components['schemas']['Excerpt'][];
      sources?: components['schemas']['Excerpt'][];
      origin?: 'generated';
    };
    QuestionInput: {
      id?: string;
      prompt: string;
      type: 'multiple_choice' | 'numerical' | 'short_answer' | 'free_response';
      options?: string[] | null;
      answer?: string;
      tolerance?: string;
      accepted_answers?: string[];
      case_sensitive?: boolean;
      rubric?: components['schemas']['RubricInput'];
      max_length?: number;
      points?: number;
      topic_id: string;
      marking_scheme?: components['schemas']['Excerpt'][];
      sources?: components['schemas']['Excerpt'][];
      origin?: 'generated';
    };
    Rubric: {
      criteria: components['schemas']['Criterion'][];
      model_answer?: string;
    };
    RubricInput: {
      criteria: {
        id?: string;
        title: string;
        levels: components['schemas']['Level'][];
      }[];
      model_answer?: string;
    };
    SavedAnswers: {
      answers: {
        [key: string]: string;
      };
      saved_at: string | null;
    };
    StudentAssessment: {
      id: string;
      classroom_id: string;
      title: string;
      questions: components['schemas']['StudentQuestion'][];
      opens_at: string | null;
      due_at: string | null;
      late_policy: 'accept_flagged' | 'reject';
    };
    StudentQuestion: {
      id: string;
      prompt: string;
      type: 'multiple_choice' | 'numerical' | 'short_answer' | 'free_response';
      options: string[] | null;
      max_length?: number;
      points: number;
      topic_id: string;
    };
    SubmissionReceipt: {
      id: string;
      status: 'submitted';
      late: boolean;
    };
  };
  responses: never;
  parameters: never;
  requestBodies: never;
  headers: never;
  pathItems: never;
}
export type $defs = Record<string, never>;
export type operations = Record<string, never>;
