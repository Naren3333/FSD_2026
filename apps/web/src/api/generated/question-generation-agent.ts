// generated from contracts/http/question-generation-agent.openapi.json; run npm run contracts to refresh.

export type paths = Record<string, never>;
export type webhooks = Record<string, never>;
export interface components {
  schemas: {
    Criterion: {
      title: string;
      levels: components['schemas']['Level'][];
    };
    DraftQuestion: {
      prompt: string;
      type: 'multiple_choice' | 'short_answer' | 'free_response';
      topic_id: string;
      points: number;
      options: string[];
      answer: string;
      tolerance?: '';
      accepted_answers?: string[];
      case_sensitive?: boolean;
      rubric?: components['schemas']['Rubric'] | null;
      max_length?: number | null;
      marking_scheme?: components['schemas']['Excerpt'][];
      sources: components['schemas']['Excerpt'][];
      origin?: 'generated';
    };
    ErrorBody: {
      code: string;
      message: string;
      capability?: string | null;
    };
    ErrorResponse: {
      error: components['schemas']['ErrorBody'];
    };
    Excerpt: {
      document_id?: string | null;
      chunk_id?: string | null;
      page?: number | null;
      text: string;
    };
    GenerateRequest: {
      classroom_id: string;
      document_ids: string[];
      topic_id: string;
      question_types: ('multiple_choice' | 'short_answer' | 'free_response')[];
      difficulty: 'introductory' | 'intermediate' | 'advanced';
      count: number;
      focus?: string;
      existing_prompts?: string[];
    };
    GenerateResponse: {
      questions: components['schemas']['Generated'][];
      requested: number;
      returned: number;
      dropped: {
        [key: string]: number;
      };
      requires_teacher_review?: true;
    };
    Generated: {
      question: components['schemas']['DraftQuestion'];
      evidence: string;
      explanation: string;
    };
    Guidance: {
      type: 'short_answer' | 'free_response';
      accepted_answers: string[];
      rubric: components['schemas']['Rubric'] | null;
      marking_points: components['schemas']['Excerpt'];
      sources: components['schemas']['Excerpt'][];
      evidence: string;
      requires_teacher_review?: true;
    };
    GuidanceRequest: {
      classroom_id: string;
      document_ids: string[];
      prompt: string;
      type: 'short_answer' | 'free_response';
      points?: number;
    };
    Level: {
      points: number;
      descriptor: string;
    };
    Rubric: {
      criteria: components['schemas']['Criterion'][];
      model_answer: string;
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
