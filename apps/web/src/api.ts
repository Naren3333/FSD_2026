import { Injectable, signal } from '@angular/core';
import Keycloak from 'keycloak-js';
import { SseParser } from './sse';

export interface Profile { id: string; org_id: string; role: 'teacher' | 'student' }
export interface Classroom { id: string; name: string; teacher_id: string }
export interface Node { id: string; kind: string; name: string; parent_id: string | null }
export type DocType = 'teaching_material' | 'rubric' | 'marking_scheme';
export type DocumentStatus = 'extracted' | 'extraction_unsupported' | 'indexing' | 'indexed' | 'failed';
export interface DocumentRecord { id: string; title: string; mime_type: string; status: DocumentStatus; revision: number; doc_type: DocType; page_count?: number; chunk_count?: number; indexed_at?: string; failure_reason?: string }
export interface Thread { id: string; classroom_id: string; role: 'student' | 'teacher'; title: string | null; created_at: string; last_message_at: string }
export interface Citation { chunk_id: string; document_id: string; title: string; page_start: number; page_end: number; kind: 'text' | 'table' | 'figure'; source: 'pdf' | 'vision'; content: string; image: string | null }
export type Outcome = 'answered' | 'no_results' | 'error' | 'interrupted';
export interface ChatMessage { id: string; role: 'user' | 'assistant'; content: string; outcome: Outcome | null; citations: Citation[] | null; created_at: string }
export interface StreamEvent { event: string; data: Record<string, unknown> }
export interface Question { id?: string; prompt: string; type: 'numerical' | 'multiple_choice'; options: string[]; answer?: string; tolerance?: string; skill_id: string }
export interface Assessment { id: string; classroom_id: string; title: string; questions: Question[] }
export interface Evidence { question_id: string; skill_id: string; correct: boolean; awarded: number; possible: number }
export interface Evaluation { id: string; student_id: string; status: 'draft' | 'finalized'; evidence: Evidence[] }
export interface Performance { evidence_status: string; interpretation: string; skills: {skill_id: string; attempted: number; correct: number; observed_accuracy: number; evidence_status: string}[] }

@Injectable({providedIn:'root'})
export class Api {
  readonly profile = signal<Profile | null>(null);
  readonly auth = new Keycloak({url:'http://localhost:8081', realm:'learning', clientId:'learning-web'});
  async initialize(): Promise<void> {
    await this.auth.init({checkLoginIframe:false, pkceMethod:'S256', responseMode:'query'});
    if (this.auth.authenticated) this.profile.set(await this.request<Profile>('identity','/me','POST',{}));
  }
  login(): Promise<void> { return this.auth.login({redirectUri:location.origin+'/'}); }
  logout(): Promise<void> { return this.auth.logout({redirectUri:location.origin+'/'}); }
  /** `timeout` bounds the whole call, upload included: a 50 MiB PDF needs far longer than the default on a slow link. */
  async request<T>(service: string, path: string, method='GET', body?: unknown, contentType?: string, timeout=20000): Promise<T> {
    const response = await this.send(service, path, method, body, contentType, AbortSignal.timeout(timeout));
    return response.json() as Promise<T>;
  }
  /** A file the API serves, such as a figure crop, as an object URL the caller revokes. */
  async objectUrl(service: string, path: string): Promise<string> {
    const response = await this.send(service, path, 'GET', undefined, undefined, AbortSignal.timeout(20000));
    return URL.createObjectURL(await response.blob());
  }
  /** Posts JSON and reads the server-sent events of the reply as they arrive. */
  async stream(service: string, path: string, body: unknown, signal: AbortSignal, onEvent: (event: StreamEvent) => void): Promise<void> {
    const response = await this.send(service, path, 'POST', body, undefined, signal);
    if (!response.body) throw new Error('The reply could not be read. Retry later.');
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    const parser = new SseParser();
    for (;;) {
      const {done, value} = await reader.read().catch(() => { throw new Error(signal.aborted ? 'Stopped.' : 'The connection closed before the answer finished. Retry later.'); });
      if (done) break;
      parser.push(value).forEach(onEvent);
    }
  }
  private async send(service: string, path: string, method: string, body: unknown, contentType: string | undefined, signal: AbortSignal): Promise<Response> {
    if (!this.auth.authenticated) throw new Error('Sign in to continue.');
    try { await this.auth.updateToken(30); } catch { throw new Error('Your session expired. Sign in again; your unsaved work is still here.'); }
    const response = await fetch(`http://localhost:8000/api/${service}${path}`, {method, headers:{Authorization:`Bearer ${this.auth.token}`, ...(body === undefined ? {} : {'Content-Type':contentType || 'application/json'})}, body: body instanceof File ? body : body === undefined ? undefined : JSON.stringify(body), signal}).catch(() => {throw new Error(signal.aborted && signal.reason?.name !== 'TimeoutError' ? 'Stopped.' : 'The service could not be reached. Check your connection and retry.');});
    if (!response.ok) {
      const detail: {error?:{message?:string}} = await response.json().catch(() => ({}));
      throw new Error(detail.error?.message || (response.status===401?'Your session expired. Sign in again.':response.status===403?'You do not have access to this resource.':`The operation failed (${response.status}). Retry later.`));
    }
    return response;
  }
  classrooms(): Promise<Classroom[]> {return this.request('identity','/classrooms');}
  nodes(): Promise<Node[]> {return this.request('curriculum','/nodes');}
  documents(id: string): Promise<DocumentRecord[]> {return this.request('content',`/documents?classroom_id=${encodeURIComponent(id)}`);}
  assessments(id: string): Promise<Pick<Assessment,'id'|'title'>[]> {return this.request('assessment',`/assessments?classroom_id=${encodeURIComponent(id)}`);}
  reindex(id: string): Promise<DocumentRecord> {return this.request('content',`/documents/${encodeURIComponent(id)}/reindex`,'POST',{});}
  threads(id: string): Promise<{threads: Thread[]}> {return this.request('chatbot',`/threads?classroom_id=${encodeURIComponent(id)}`);}
  createThread(id: string): Promise<Thread> {return this.request('chatbot','/threads','POST',{classroom_id:id});}
  messages(id: string): Promise<{messages: ChatMessage[]}> {return this.request('chatbot',`/threads/${encodeURIComponent(id)}/messages`);}
  evaluations(id: string): Promise<Evaluation[]> {return this.request('grading',`/evaluations?classroom_id=${encodeURIComponent(id)}`);}
}

