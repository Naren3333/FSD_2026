import { Component, Input, OnChanges, OnDestroy, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { Api, DocType, DocumentRecord } from './api';
import { ActionState, FieldComponent, StatusComponent } from './shared';

const MIB = 1024 * 1024;
export const LIMITS = {pdf: 50 * MIB, txt: 5 * MIB};
// Long enough for a 50 MiB PDF at about 1.5 Mbit/s; other calls keep the 20 s default.
const UPLOAD_TIMEOUT = 5 * 60 * 1000;
const FAILURES: Record<string, string> = {
  NO_TEXT_LAYER: 'the PDF has no readable text layer',
  ENCRYPTED: 'the PDF is password protected',
  CORRUPT: 'the PDF could not be read',
  AI_UNAVAILABLE: 'the AI service was unavailable after several attempts',
  INTERNAL: 'an internal error occurred',
};
export const DOC_TYPES: {value: DocType; label: string}[] = [{value:'teaching_material',label:'Teaching material'},{value:'rubric',label:'Rubric'},{value:'marking_scheme',label:'Marking scheme'}];

const count=(n: number | undefined, noun: string)=>`${n ?? 0} ${noun}${n === 1 ? '' : 's'}`;

/** What a document's status means for the reader; never implies text extraction is search indexing. */
export function describe(doc: DocumentRecord): {label: string; detail: string} {
  switch (doc.status) {
  case 'extracted': return {label:'Text extracted', detail:'Text files are not indexed for the assistant.'};
  case 'indexing': return {label:'Indexing', detail:'Being prepared for the assistant. This can take a few minutes for a long PDF.'};
  case 'indexed': return {label:'Indexed', detail:`${count(doc.page_count, 'page')}, ${count(doc.chunk_count, 'passage')}. Available to the assistant.`};
  case 'failed': return {label:'Indexing failed', detail:`Not available to the assistant: ${FAILURES[doc.failure_reason || ''] || 'an unknown error occurred'}.`};
  default: return {label:'Not indexed', detail:'Uploaded before indexing was available. Index it to use it with the assistant.'};
  }
}

@Component({selector:'app-materials',standalone:true,imports:[ReactiveFormsModule,FieldComponent,StatusComponent],template:`
<div class="section-heading"><div><p class="eyebrow">A shared source of learning</p><h2>Teaching materials</h2></div><button class="secondary" (click)="load()" [disabled]="state.busy() || !classroomId">Refresh materials</button></div><p>Upload original teaching documents. PDFs are indexed so the assistant can answer from them with sources; text files are kept as extracted text only.</p>
<div class="split"><section class="panel"><h3>Classroom library</h3><p class="caption">Up to 100 most recent documents</p>@for(doc of documents();track doc.id){<article class="record"><strong>{{doc.title}}</strong><span class="doc-tags"><span class="badge" [class.badge-danger]="doc.status==='failed'">{{status(doc).label}}</span>@if(api.profile()?.role==='teacher'){<span class="badge badge-plain">{{typeLabel(doc.doc_type)}}</span>}</span><small>{{status(doc).detail}}</small><small>Revision {{doc.revision}} · {{doc.mime_type}}</small>@if(api.profile()?.role==='teacher' && (doc.status==='failed' || doc.status==='extraction_unsupported')){<button class="secondary" (click)="reindex(doc)" [disabled]="state.busy()">{{doc.status==='failed'?'Retry indexing':'Index document'}}<span class="visually-hidden"> {{doc.title}}</span></button>}</article>}@empty{<p class="empty">{{classroomId?'No documents in this classroom. Upload a teaching resource to begin.':'Select a classroom to view its materials.'}}</p>}@if(polling()){<p class="caption" aria-live="off">Indexing status refreshes automatically.</p>}</section>
@if(api.profile()?.role==='teacher'){<section class="panel"><h3>Upload a document</h3><form novalidate [formGroup]="form" (ngSubmit)="upload()"><app-field controlId="document-title" label="Document title" [invalid]="form.controls.title.touched && form.controls.title.invalid"><input id="document-title" formControlName="title" maxlength="160" aria-describedby="document-title-help" [attr.aria-invalid]="form.controls.title.touched && form.controls.title.invalid"></app-field><app-field controlId="document-type" label="Document type" hint="Students never see rubrics or marking schemes, and the assistant does not use them in their answers."><select id="document-type" formControlName="doc_type" aria-describedby="document-type-help">@for(type of types;track type.value){<option [value]="type.value">{{type.label}}</option>}</select></app-field><app-field controlId="document-file" label="Educational file" hint="PDF up to 50 MiB, or UTF-8 .txt up to 5 MiB."><input id="document-file" type="file" accept=".txt,.pdf" (change)="selectFile($event)" aria-describedby="document-file-help"></app-field>@if(file()){<p>{{file()?.name}}</p>}<button [disabled]="state.busy() || !classroomId">Upload document</button><button type="reset" class="secondary clear-form" (click)="file.set(null)" [disabled]="state.busy()">Clear form</button></form></section>}</div><app-status [state]="state"/>`})
export class MaterialsComponent implements OnChanges, OnDestroy {
  readonly api=inject(Api);readonly state=new ActionState();readonly documents=signal<DocumentRecord[]>([]);readonly file=signal<File|null>(null);readonly polling=signal(false);readonly types=DOC_TYPES;@Input() classroomId='';private revision=0;private timer:ReturnType<typeof setTimeout>|undefined;
  readonly form=new FormGroup({title:new FormControl('',{nonNullable:true,validators:[Validators.required,Validators.maxLength(160),Validators.pattern(/\S/)]}),doc_type:new FormControl<DocType>('teaching_material',{nonNullable:true})});
  ngOnChanges():void{this.documents.set([]);this.load();}
  ngOnDestroy():void{clearTimeout(this.timer);}
  load():void{const id=this.classroomId,revision=++this.revision;this.schedule();if(!id)return;void this.state.run(async()=>{const docs=await this.api.documents(id);if(revision===this.revision)this.show(docs);});}
  /** While a PDF is indexing, re-reads the list quietly; a failed read stops until the next manual refresh. */
  private schedule():void{clearTimeout(this.timer);this.polling.set(this.documents().some(d=>d.status==='indexing'));if(!this.polling())return;const id=this.classroomId,revision=this.revision;
    this.timer=setTimeout(()=>{if(revision!==this.revision||this.state.busy())return this.schedule();this.api.documents(id).then(docs=>{if(revision===this.revision)this.show(docs);},()=>this.polling.set(false));},5000);}
  private show(docs:DocumentRecord[]):void{this.documents.set(docs);this.schedule();}
  status(doc:DocumentRecord){return describe(doc);}
  typeLabel(type:DocType):string{return DOC_TYPES.find(t=>t.value===type)?.label||type;}
  selectFile(event:Event):void{const file=(event.target as HTMLInputElement).files?.[0]||null;const pdf=!!file&&/\.pdf$/i.test(file.name);if(file&&(file.size===0||!(/\.(txt|pdf)$/i).test(file.name)||file.size>(pdf?LIMITS.pdf:LIMITS.txt))){this.state.error.set('Choose a nonempty PDF up to 50 MiB or .txt file up to 5 MiB.');this.file.set(null);return;}this.file.set(file);this.form.markAsDirty();}
  upload():void{if(!this.state.validate(this.form))return;const file=this.file();if(!file){this.state.error.set('Choose a file to upload.');return;}const pdf=file.name.toLowerCase().endsWith('.pdf');const {title,doc_type}=this.form.getRawValue();
    void this.state.run(async()=>{await this.api.request('content',`/documents?classroom_id=${encodeURIComponent(this.classroomId)}&title=${encodeURIComponent(title)}&doc_type=${doc_type}`,'POST',file,pdf?'application/pdf':'text/plain',UPLOAD_TIMEOUT);this.form.reset();this.file.set(null);const input=document.getElementById('document-file') as HTMLInputElement|null;if(input)input.value='';this.show(await this.api.documents(this.classroomId));},pdf?'Document uploaded. Indexing has started.':'Document uploaded. Text files are not indexed for the assistant.');}
  reindex(doc:DocumentRecord):void{void this.state.run(async()=>{await this.api.reindex(doc.id);this.show(await this.api.documents(this.classroomId));},'Indexing has started.');}
}
