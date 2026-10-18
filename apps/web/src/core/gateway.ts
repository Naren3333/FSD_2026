import { Injectable, inject } from '@angular/core';
import type { StreamEvent } from '../api/models';
import { Auth } from './auth';
import { APP_CONFIG } from './config';
import { SseParser } from './sse';

/** The backends behind Kong, by their path under /api. */
export type Service =
  | 'identity'
  | 'curriculum'
  | 'content'
  | 'assessment'
  | 'grading'
  | 'analytics'
  | 'chatbot'
  | 'question-generation';

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  /** JSON, or a File sent as is with `contentType`. */
  body?: unknown;
  contentType?: string;
  /** Bounds the whole call, upload included. Defaults to the configured request timeout. */
  timeout?: number;
}

/** A failure the API answered: its readable message, its status and, when it named one, its error code. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code = '',
  ) {
    super(message);
  }
}

/** Authenticated calls to the backends through Kong, with their failures turned into readable messages. */
@Injectable({ providedIn: 'root' })
export class Gateway {
  private readonly auth = inject(Auth);
  private readonly config = inject(APP_CONFIG);

  async request<T>(service: Service, path: string, options: RequestOptions = {}): Promise<T> {
    const timeout = AbortSignal.timeout(options.timeout ?? this.config.requestTimeout);
    const response = await this.send(service, path, options, timeout);
    // 204 No Content, such as an archive, has no body to read.
    if (response.status === 204) return undefined as T;
    return response.json() as Promise<T>;
  }

  /** A file the API serves, such as a figure crop, as an object URL the caller revokes. */
  async objectUrl(service: Service, path: string): Promise<string> {
    const response = await this.send(service, path, {}, AbortSignal.timeout(this.config.requestTimeout));
    return URL.createObjectURL(await response.blob());
  }

  /** Posts JSON and reads the server-sent events of the reply as they arrive. */
  async stream(
    service: Service,
    path: string,
    body: unknown,
    signal: AbortSignal,
    onEvent: (event: StreamEvent) => void,
  ): Promise<void> {
    const response = await this.send(service, path, { method: 'POST', body }, signal);
    if (!response.body) throw new Error('The reply could not be read. Retry later.');
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    const parser = new SseParser();
    for (;;) {
      const { done, value } = await reader.read().catch(() => {
        throw new Error(
          signal.aborted ? 'Stopped.' : 'The connection closed before the answer finished. Retry later.',
        );
      });
      if (done) break;
      parser.push(value).forEach(onEvent);
    }
  }

  private async send(
    service: Service,
    path: string,
    options: RequestOptions,
    signal: AbortSignal,
  ): Promise<Response> {
    const token = await this.auth.token();
    const { body } = options;
    const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
    if (body !== undefined) headers['Content-Type'] = options.contentType || 'application/json';
    const response = await fetch(`${this.config.gatewayUrl}/api/${service}${path}`, {
      method: options.method ?? 'GET',
      headers,
      body: body instanceof File ? body : body === undefined ? undefined : JSON.stringify(body),
      signal,
    }).catch(() => {
      throw new Error(unreachable(signal));
    });
    if (response.status === 401) this.auth.expired.set(true);
    if (!response.ok) throw await failure(response);
    return response;
  }
}

function unreachable(signal: AbortSignal): string {
  return signal.aborted && signal.reason?.name !== 'TimeoutError'
    ? 'Stopped.'
    : 'The service could not be reached. Check your connection and retry.';
}

/** The API's own message and code, or a message for the status. */
async function failure(response: Response): Promise<ApiError> {
  const detail: { error?: { code?: string; message?: string } } = await response.json().catch(() => ({}));
  const fail = (message: string) => new ApiError(message, response.status, detail.error?.code ?? '');
  if (detail.error?.message) return fail(detail.error.message);
  if (response.status === 401) return fail('Your session expired. Sign in again.');
  if (response.status === 403) return fail('You do not have access to this resource.');
  if (response.status === 413) return fail('This is too large to send. Shorten it and try again.');
  if (response.status === 504) return fail('The service took too long to answer. Retry later.');
  return fail(`The operation failed (${response.status}). Retry later.`);
}
