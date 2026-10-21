// generated from contracts/http/question-chatbot-agent.openapi.json; run npm run contracts to refresh.

export type paths = Record<string, never>;
export type webhooks = Record<string, never>;
export interface components {
  schemas: {
    Citation: {
      chunk_id: string;
      document_id: string;
      title: string;
      section_path?: string | null;
      page_start: number;
      page_end: number;
      kind: 'text' | 'table' | 'figure';
      source: 'pdf' | 'vision' | 'markdown' | 'pptx' | 'docx';
      content: string;
      image: string | null;
      removed?: boolean;
    };
    ErrorBody: {
      code: string;
      message: string;
      capability?: string | null;
    };
    ErrorResponse: {
      error: components['schemas']['ErrorBody'];
    };
    Message: {
      id: string;
      role: 'user' | 'assistant';
      content: string;
      outcome: ('answered' | 'no_results' | 'error' | 'interrupted') | null;
      citations: components['schemas']['Citation'][] | null;
      created_at: string;
    };
    MessageList: {
      messages: components['schemas']['Message'][];
    };
    NewMessage: {
      content: string;
    };
    NewThread: {
      classroom_id: string;
    };
    Thread: {
      id: string;
      classroom_id: string;
      role: 'student' | 'teacher';
      title: string | null;
      created_at: string;
      last_message_at: string;
    };
    ThreadList: {
      threads: components['schemas']['Thread'][];
    };
    Usage: {
      spent_usd: number;
      limit_usd: number;
      resets_at: string;
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
