import '@angular/compiler';
import { describe, it, expect } from 'vitest';
import type { Citation, DocumentRecord } from './api';
import { SseParser, pages, segments } from './sse';
import { describe as describeDocument } from './materials';

const A='11111111-1111-4111-8111-111111111111',B='22222222-2222-4222-8222-222222222222';
const cite=(id:string):Citation=>({chunk_id:id,document_id:id,title:'Notes',page_start:2,page_end:3,kind:'text',source:'pdf',content:'x',image:null});
const doc=(change:Partial<DocumentRecord>):DocumentRecord=>({id:A,title:'T',mime_type:'application/pdf',status:'indexing',revision:1,doc_type:'teaching_material',...change});

describe('SseParser',()=>{
  it('joins frames split across reads and skips malformed ones',()=>{
    const parser=new SseParser();
    expect(parser.push('event: token\ndata: {"te')).toEqual([]);
    expect(parser.push('xt":"Hi"}\n\nevent: token\r\ndata: nope\r\n\r\nevent: done\ndata: {"outcome":"answered"}\n\n')).toEqual([{event:'token',data:{text:'Hi'}},{event:'done',data:{outcome:'answered'}}]);
  });
});

describe('segments',()=>{
  it('numbers citations in citation order and drops tags without a citation',()=>{
    expect(segments(`One [doc:${B}] two [doc:${A}][doc:${'3'.repeat(8)}-3333-4333-8333-333333333333].`,[cite(A),cite(B)])).toEqual([
      {text:'One ',ref:0},{text:'',ref:2},{text:' two ',ref:0},{text:'',ref:1},{text:'.',ref:0}]);
  });
  it('hides a tag that is still streaming',()=>{
    expect(segments('Light [doc:1111',[])).toEqual([{text:'Light ',ref:0}]);
  });
  it('describes page ranges',()=>{expect(pages({page_start:4,page_end:4})).toBe('page 4');expect(pages(cite(A))).toBe('pages 2–3');});
});

describe('document status',()=>{
  it('never presents extraction as indexing',()=>{
    expect(describeDocument(doc({status:'extracted',mime_type:'text/plain'})).detail).toMatch(/not indexed/);
    expect(describeDocument(doc({status:'extraction_unsupported'})).label).toBe('Not indexed');
    expect(describeDocument(doc({status:'indexed',page_count:12,chunk_count:20})).detail).toBe('12 pages, 20 passages. Available to the assistant.');expect(describeDocument(doc({status:'indexed',page_count:1,chunk_count:1})).detail).toBe('1 page, 1 passage. Available to the assistant.');
    expect(describeDocument(doc({status:'failed',failure_reason:'NO_TEXT_LAYER'})).detail).toMatch(/no readable text layer/);
  });
});
