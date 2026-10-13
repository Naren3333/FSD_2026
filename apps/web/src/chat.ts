import { Component, Input, OnChanges, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { Api, ChatMessage, Citation, StreamEvent, Thread } from './api';
import { ActionState, FieldComponent, StatusComponent } from './shared';
import { pages, segments } from './sse';

export const MAX_QUESTION = 1000;
const OUTCOME_NOTES: Record<string, string> = {
  no_results: 'No matching passage was found in this classroom’s indexed materials.',
  error: 'This answer failed before it finished. Ask again to retry.',
  interrupted: 'Stopped before the answer finished.',
};

@Component({selector:'app-chat',standalone:true,imports:[ReactiveFormsModule,FieldComponent,StatusComponent],template:`
<div class="section-heading"><div><p class="eyebrow">Grounded in your classroom’s documents</p><h2>Ask the materials</h2></div><button class="secondary" (click)="loadThreads()" [disabled]="state.busy() || streaming() || !classroomId">Refresh conversations</button></div>
<p>Answers come only from this classroom’s indexed PDFs and cite their sources by number. Check anything important against the original document.</p>
<div class="chat-layout"><section class="panel thread-panel" aria-labelledby="threads-heading"><h3 id="threads-heading">Your conversations</h3><p class="caption">Up to 100 most recent, visible only to you</p><button class="secondary new-thread" (click)="newThread()" [disabled]="streaming() || !classroomId">New conversation</button>
@if(threadsLoading()){<p class="empty" aria-busy="true">Loading conversations…</p>}@else{<ul class="thread-list">@for(thread of threads();track thread.id){<li><button class="thread" [class.active]="thread.id===active()?.id" [attr.aria-current]="thread.id===active()?.id?'true':null" (click)="open(thread)" [disabled]="streaming()"><span>{{thread.title || 'Untitled conversation'}}</span><small>{{when(thread.last_message_at)}}</small></button></li>}@empty{<li class="empty">{{classroomId?'No conversations yet. Ask a question to start one.':'Select a classroom to ask about its materials.'}}</li>}</ul>}</section>
<section class="panel conversation-panel" aria-labelledby="conversation-heading"><h3 id="conversation-heading">{{active()?.title || 'New conversation'}}</h3>
@if(messagesLoading()){<p class="empty" aria-busy="true">Loading messages…</p>}@else{<ol class="conversation" [attr.aria-busy]="streaming()">@for(message of messages();track message.id){<li class="turn" [class.answer]="message.role==='assistant'"><span class="speaker">{{message.role==='user'?'You':'Assistant'}}</span>
@if(message.role==='user'){<p class="turn-text">{{message.content}}</p>}@else{@if(message.content || message.id===streamingId){<p class="turn-text">@for(part of parts(message);track $index){@if(part.ref){<a class="cite" [href]="'#'+sourceId(message,part.ref)" (click)="jump($event,message,part.ref)" [attr.aria-label]="'Source '+part.ref">[{{part.ref}}]</a>}@else{<span>{{part.text}}</span>}}@if(message.id===streamingId){<span class="cursor" aria-hidden="true">▍</span>}</p>}
@if(message.outcome && notes[message.outcome]){<p class="outcome" [class.error]="message.outcome==='error'"><span class="badge">{{message.outcome==='no_results'?'No matching material':message.outcome==='error'?'Failed':'Stopped'}}</span> {{notes[message.outcome]}}</p>}
@if(message.citations?.length){<ol class="sources" [attr.aria-label]="'Sources for this answer'">@for(source of message.citations;track source.chunk_id;let n=$index){<li [id]="sourceId(message,n+1)" tabindex="-1"><details (toggle)="reveal($event,source)"><summary><span class="source-number">{{n+1}}</span><span><strong>{{source.title}}</strong> <small>{{pageText(source)}}@if(source.kind!=='text'){<span> · {{source.kind==='table'?'Table':'Figure'}}</span>}</small></span></summary>@if(source.source==='vision'){<p class="caption">This passage was written by a model from the page image.</p>}<p class="passage">{{source.content}}</p>@if(source.image){@if(figures()[source.chunk_id];as url){<img [src]="url" [alt]="'Figure from '+source.title+', '+pageText(source)">}@else{<p class="caption">{{figureErrors()[source.chunk_id] || 'Loading figure…'}}</p>}}</details></li>}</ol>}}
</li>}@empty{<li class="empty">{{classroomId?'Ask about the teaching materials in this classroom, for example a definition or a worked step.':'Select a classroom first.'}}</li>}</ol>}
<form novalidate [formGroup]="form" (ngSubmit)="send()"><app-field controlId="chat-question" label="Your question" [hint]="'Up to '+max+' characters. Press Ctrl+Enter to send. '+form.controls.question.value.length+'/'+max" error="Enter a question of 1 to 1000 characters." [invalid]="tried() && form.controls.question.invalid"><textarea id="chat-question" formControlName="question" rows="3" [attr.maxlength]="max" aria-describedby="chat-question-help" [attr.aria-invalid]="tried() && form.controls.question.invalid" (keydown)="shortcut($event)"></textarea></app-field>
<div class="chat-actions"><button [disabled]="state.busy() || streaming() || !classroomId">Send question</button>@if(streaming()){<button type="button" class="secondary" (click)="stop()">Stop answer</button>}</div></form>
<div class="feedback"><p role="status" aria-live="polite">{{announcement()}}</p></div></section></div><app-status [state]="state"/>`})
export class ChatComponent implements OnChanges {
  readonly api=inject(Api);readonly state=new ActionState();readonly max=MAX_QUESTION;readonly notes=OUTCOME_NOTES;
  @Input() classroomId='';
  readonly threads=signal<Thread[]>([]);readonly active=signal<Thread|null>(null);readonly messages=signal<ChatMessage[]>([]);
  readonly threadsLoading=signal(false);readonly messagesLoading=signal(false);readonly streaming=signal(false);readonly announcement=signal('');readonly tried=signal(false);
  readonly figures=signal<Record<string,string>>({});readonly figureErrors=signal<Record<string,string>>({});
  streamingId='';private controller:AbortController|null=null;private threadRevision=0;private messageRevision=0;
  readonly form=new FormGroup({question:new FormControl('',{nonNullable:true,validators:[Validators.required,Validators.maxLength(MAX_QUESTION),Validators.pattern(/\S/)]})});

  ngOnChanges():void{this.controller?.abort();this.threads.set([]);this.reset();this.loadThreads();}
  private reset():void{this.messageRevision++;this.messagesLoading.set(false);this.active.set(null);this.messages.set([]);Object.values(this.figures()).forEach(url=>URL.revokeObjectURL(url));this.figures.set({});this.figureErrors.set({});}
  loadThreads():void{const id=this.classroomId,revision=++this.threadRevision;if(!id)return;this.threadsLoading.set(true);void this.state.run(async()=>{try{const {threads}=await this.api.threads(id);if(revision===this.threadRevision)this.threads.set(threads);}finally{if(revision===this.threadRevision)this.threadsLoading.set(false);}});}
  newThread():void{this.reset();this.announcement.set('');setTimeout(()=>document.getElementById('chat-question')?.focus());}
  open(thread:Thread):void{this.reset();this.active.set(thread);const revision=++this.messageRevision;this.messagesLoading.set(true);void this.state.run(async()=>{try{const {messages}=await this.api.messages(thread.id);if(revision===this.messageRevision)this.messages.set(messages);}finally{if(revision===this.messageRevision)this.messagesLoading.set(false);}});}

  shortcut(event:KeyboardEvent):void{if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)){event.preventDefault();this.send();}}
  send():void{
    if(this.streaming())return;this.tried.set(true);if(!this.state.validate(this.form))return;
    const question=this.form.controls.question.value.trim(),classroom=this.classroomId;
    void this.state.run(async()=>{
      let thread=this.active();
      if(!thread){thread=await this.api.createThread(classroom);this.active.set(thread);this.threads.update(list=>[thread as Thread,...list]);}
      const answer:ChatMessage={id:'streaming',role:'assistant',content:'',outcome:null,citations:null,created_at:new Date().toISOString()};
      this.messages.update(list=>[...list,{id:'asked',role:'user',content:question,outcome:null,citations:null,created_at:answer.created_at},answer]);
      this.streamingId=answer.id;this.streaming.set(true);this.announcement.set('Answering…');this.controller=new AbortController();
      let started=false,failure='';
      const update=(change:Partial<ChatMessage>)=>this.messages.update(list=>list.map(m=>m.id===answer.id?{...m,...change}:m));
      try{
        await this.api.stream('chatbot',`/threads/${thread.id}/messages`,{content:question},this.controller.signal,(event:StreamEvent)=>{
          if(!started){started=true;this.form.reset();this.tried.set(false);}
          const current=this.messages().find(m=>m.id===answer.id);
          if(event.event==='token')update({content:(current?.content||'')+String(event.data['text']??'')});
          else if(event.event==='citations')update({citations:(event.data['citations'] as Citation[])||[]});
          else if(event.event==='done')update({outcome:event.data['outcome'] as ChatMessage['outcome']});
          else if(event.event==='error'){update({outcome:'error'});failure=String(event.data['message']||'The assistant could not answer. Retry later.');}
        });
      }catch(error){
        if(!started){this.messages.update(list=>list.filter(m=>m.id!==answer.id&&m.id!=='asked'));this.announcement.set('');throw error;}
        if(!this.controller?.signal.aborted)failure=error instanceof Error?error.message:'The answer stopped. Retry later.';
        update({outcome:this.controller?.signal.aborted?'interrupted':'error'});
      }finally{this.streaming.set(false);this.streamingId='';this.controller=null;}
      const done=this.messages().find(m=>m.id===answer.id);
      this.announcement.set(done?.outcome==='answered'?'Answer complete.':done?.outcome==='interrupted'?'Answer stopped.':done?.outcome==='no_results'?'No matching material found.':'');
      await this.sync(thread);
      if(failure)throw new Error(failure);
    });
  }
  stop():void{this.controller?.abort();}
  /** Replaces the local copy with what the chatbot saved, and moves the conversation to the top. */
  private async sync(thread:Thread):Promise<void>{
    try{const [{messages},{threads}]=await Promise.all([this.api.messages(thread.id),this.api.threads(thread.classroom_id)]);if(this.active()?.id!==thread.id)return;this.messages.set(messages);this.threads.set(threads);this.active.set(threads.find(t=>t.id===thread.id)||thread);}catch{/* keep the streamed copy; Refresh conversations retries */}
  }

  parts(message:ChatMessage){return segments(message.content,message.citations||[]);}
  pageText(source:Citation):string{return pages(source);}
  sourceId(message:ChatMessage,n:number):string{return `source-${message.id}-${n}`;}
  jump(event:Event,message:ChatMessage,n:number):void{event.preventDefault();const item=document.getElementById(this.sourceId(message,n));if(!item)return;item.querySelector('details')?.setAttribute('open','');item.focus();}
  reveal(event:Event,source:Citation):void{
    if(!(event.target as HTMLDetailsElement).open||!source.image||this.figures()[source.chunk_id])return;
    this.figureErrors.update(e=>({...e,[source.chunk_id]:''}));
    this.api.objectUrl('content',`/documents/${encodeURIComponent(source.document_id)}/figures/${encodeURIComponent(source.image)}`)
      .then(url=>this.figures.update(f=>({...f,[source.chunk_id]:url})))
      .catch((error:unknown)=>this.figureErrors.update(e=>({...e,[source.chunk_id]:error instanceof Error?`The figure could not be loaded. ${error.message}`:'The figure could not be loaded.'})));
  }
  when(iso:string):string{return new Date(iso).toLocaleString('en-SG',{dateStyle:'medium',timeStyle:'short'});}
}
