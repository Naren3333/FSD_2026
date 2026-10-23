// generated from contracts/http/student-performance-agent.openapi.json; run npm run contracts to refresh.

export type paths = Record<string, never>;
export type webhooks = Record<string, never>;
export interface components {
  schemas: {
    ErrorBody: {
      code: string;
      message: string;
      capability?: string | null;
    };
    ErrorResponse: {
      error: components['schemas']['ErrorBody'];
    };
    Evidence: {
      question_id: string;
      evaluation_id: string;
      assessment_id: string;
      assessment_title: string;
      topic_id: string;
      prompt: string;
      awarded: number;
      possible: number;
    };
    Observation: {
      text: string;
      evidence_ids: string[];
    };
    RequestContract: {
      classroom_id: string;
      student_id: string;
    };
    ResponseContract: {
      classroom_id: string;
      student_id: string;
      observations: components['schemas']['Observation'][];
      possible_misconceptions: components['schemas']['Observation'][];
      suggested_interventions: string[];
      recommended_topic_ids: string[];
      evidence: components['schemas']['Evidence'][];
      steps: components['schemas']['Step'][];
      model_turns: number;
      dropped: {
        [key: string]: number;
      };
      requires_teacher_review?: true;
    };
    Step: {
      tool: string;
      reason: string;
      target: string;
      outcome: 'ok' | 'refused' | 'failed';
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
