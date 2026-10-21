// generated from contracts/http/grading.openapi.json; run npm run contracts to refresh.

export type paths = Record<string, never>;
export type webhooks = Record<string, never>;
export interface components {
  schemas: {
    CriterionScore: {
      criterion_id: string;
      points: number;
    };
    Error: {
      error: {
        code: string;
        message: string;
      };
    };
    Evaluation: {
      id: string;
      assessment_id: string;
      classroom_id: string;
      student_id: string;
      status: 'draft' | 'finalized';
      evidence: components['schemas']['EvaluationEvidence'][];
      answers: {
        [key: string]: string;
      };
      late: boolean;
      submitted_at: string | null;
      release_policy: 'on_finalize' | 'manual';
      finalized_at: string | null;
      released_at: string | null;
    };
    EvaluationEvidence: {
      question_id: string;
      topic_id: string;
      correct: boolean;
      awarded: number;
      possible: number;
      needs_marking?: boolean;
      marked_by?: 'auto' | 'teacher';
      criteria?: components['schemas']['CriterionScore'][];
      feedback?: string;
      suggestion?: components['schemas']['Suggestion'];
    };
    EvaluationPage: {
      evaluations: components['schemas']['Evaluation'][];
      total: number;
    };
    FinalizeRequest: {
      reason: string;
      overrides?: {
        [key: string]: number;
      };
      marks?: {
        [key: string]: components['schemas']['Mark'];
      };
    };
    GradingCounts: {
      drafts: number;
      finalized: number;
      unreleased: number;
    };
    Mark: {
      criteria?: {
        [key: string]: number;
      };
      awarded?: number;
      feedback?: string;
    };
    MarksRequest: {
      marks: {
        [key: string]: components['schemas']['Mark'];
      };
    };
    OwnEvidence: {
      question_id: string;
      topic_id: string;
      correct: boolean;
      awarded: number;
      possible: number;
      feedback?: string;
    };
    OwnResult: {
      id: string;
      assessment_id: string;
      classroom_id: string;
      student_id: string;
      status: 'finalized';
      evidence: components['schemas']['OwnEvidence'][];
      answers: {
        [key: string]: string;
      };
      late: boolean;
      finalized_at: string;
      released_at: string;
    };
    Release: {
      assessment_id: string;
      released: number;
    };
    Suggestion: {
      status: 'pending' | 'ready' | 'failed';
      criteria?: {
        criterion_id: string;
        points: number;
        evidence_quotes: string[];
        rationale: string;
      }[];
      awarded: number;
      feedback?: string;
      confidence?: 'high' | 'medium' | 'low';
      flags?: string[];
      model?: string;
      reason?: string;
      scheme_passages?: number;
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
