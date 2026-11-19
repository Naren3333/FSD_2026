// generated from contracts/http/monitoring.openapi.json; run npm run contracts to refresh.

export type paths = Record<string, never>;
export type webhooks = Record<string, never>;
export interface components {
  schemas: {
    Ceiling: {
      limit_usd: string;
      remaining_usd: string;
    };
    DailySpend: {
      recorded_usd: string;
      estimated_usd: string;
      calls: number;
      day: string;
    };
    OperationSpend: {
      recorded_usd: string;
      estimated_usd: string;
      calls: number;
      operation: string;
    };
    Reservations: {
      count: number;
      reserved_usd: string;
      expired_count: number;
      expired_usd: string;
    };
    SpendSummary: {
      as_of: string;
      timezone: string;
      today: components['schemas']['DailySpend'];
      days: components['schemas']['DailySpend'][];
      operations: components['schemas']['OperationSpend'][];
      reservations: components['schemas']['Reservations'];
      ceiling: components['schemas']['Ceiling'] | null;
    };
    UserSpend: {
      recorded_usd: string;
      estimated_usd: string;
      calls: number;
      org_id: string;
      user_id: string;
    };
    UserSpendPage: {
      as_of: string;
      timezone: string;
      total: number;
      items: components['schemas']['UserSpend'][];
    };
    MonitoringStatus: {
      services: {
        name: string;
        state: 'checking' | 'ready' | 'degraded' | 'not_ready' | 'unreachable' | 'discovery_failed';
        checked_at: string | null;
        ready_replicas: number;
        replicas: {
          replica: number;
          state: 'ready' | 'not_ready' | 'unreachable';
          ready_probe_ms: number | null;
        }[];
        last_ready_at?: string;
        latency: {
          state: 'checking' | 'available' | 'partial' | 'unavailable';
          requests: number;
          p95_ms: number | null;
          above_max_bucket: boolean;
          covered_replicas: number;
        };
      }[];
      dead_letters: {
        name: string;
        state: 'checking' | 'available' | 'unavailable' | 'stale';
        estimated_retained: number | null;
        checked_at: string | null;
        last_success_at: string | null;
      }[];
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

export const operatorPolicy = {
  required_role: 'admin',
  excluded_roles: ['teacher', 'student', 'monitoring-reader', 'ai-client'],
};
