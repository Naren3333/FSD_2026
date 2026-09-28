import { Injectable, signal } from '@angular/core';
import Keycloak from 'keycloak-js';

export interface Profile { id: string; org_id: string; role: 'teacher' | 'student' }
export interface Classroom { id: string; name: string; teacher_id: string }
export interface Node { id: string; kind: string; name: string; parent_id: string | null }
export interface DocumentRecord { id: string; title: string; mime_type: string; status: string; revision: number }
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
  async request<T>(service: string, path: string, method='GET', body?: unknown, contentType?: string): Promise<T> {
    if (!this.auth.authenticated) throw new Error('Sign in to continue.');
    try { await this.auth.updateToken(30); } catch { throw new Error('Your session expired. Sign in again; your unsaved work is still here.'); }
    const response = await fetch(`http://localhost:8000/api/${service}${path}`, {method, headers:{Authorization:`Bearer ${this.auth.token}`, ...(body === undefined ? {} : {'Content-Type':contentType || 'application/json'})}, body: body instanceof File ? body : body === undefined ? undefined : JSON.stringify(body), signal:AbortSignal.timeout(20000)}).catch(() => {throw new Error('The service could not be reached. Check your connection and retry.');});
    if (!response.ok) {
      const detail: {error?:{message?:string}} = await response.json().catch(() => ({}));
      throw new Error(detail.error?.message || (response.status===401?'Your session expired. Sign in again.':response.status===403?'You do not have access to this resource.':`The operation failed (${response.status}). Retry later.`));
    }
    return response.json() as Promise<T>;
  }
  classrooms(): Promise<Classroom[]> {return this.request('identity','/classrooms');}
  nodes(): Promise<Node[]> {return this.request('curriculum','/nodes');}
  documents(id: string): Promise<DocumentRecord[]> {return this.request('content',`/documents?classroom_id=${encodeURIComponent(id)}`);}
  assessments(id: string): Promise<Pick<Assessment,'id'|'title'>[]> {return this.request('assessment',`/assessments?classroom_id=${encodeURIComponent(id)}`);}
  evaluations(id: string): Promise<Evaluation[]> {return this.request('grading',`/evaluations?classroom_id=${encodeURIComponent(id)}`);}
}

