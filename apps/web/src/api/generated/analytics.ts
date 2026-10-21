// generated from contracts/http/analytics.openapi.json; run npm run contracts to refresh.

export type paths = Record<string, never>;
export type webhooks = Record<string, never>;
export interface components {
  schemas: {
    ClassPerformanceResponse: {
      classroom_id: string;
      total: number;
      offset: number;
      students: components['schemas']['StudentPerformance'][];
      interpretation: string;
    };
    ErrorBody: {
      code: string;
      message: string;
      capability?: string | null;
    };
    ErrorResponse: {
      error: components['schemas']['ErrorBody'];
    };
    PerformanceResponse: {
      student_id: string;
      classroom_id: string;
      topics: components['schemas']['TopicMetric'][];
      evidence_status: 'no_finalized_evidence' | 'available';
      interpretation: string;
    };
    StudentPerformance: {
      student_id: string;
      topics: components['schemas']['TopicMetric'][];
      evidence_status: 'no_finalized_evidence' | 'available';
    };
    TopicMetric: {
      topic_id: string;
      attempted: number;
      correct: number;
      observed_accuracy: number;
      marks_awarded: number;
      marks_possible: number;
      evidence_status: 'insufficient_evidence' | 'observed';
      weakness_indicator: boolean;
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
