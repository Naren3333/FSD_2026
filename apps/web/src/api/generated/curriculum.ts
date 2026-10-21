// generated from contracts/http/curriculum.openapi.json; run npm run contracts to refresh.

export type paths = Record<string, never>;
export type webhooks = Record<string, never>;
export interface components {
  schemas: {
    Error: {
      error: {
        code: string;
        message: string;
      };
    };
    Prerequisite: {
      topic_id: string;
      prerequisite_id: string;
    };
    Topic: {
      id: string;
      name: string;
    };
    TopicInput: {
      name: string;
    };
    TopicPage: {
      items: components['schemas']['Topic'][];
      total: number;
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
