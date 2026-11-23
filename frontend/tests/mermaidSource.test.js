import assert from 'node:assert/strict';
import test from 'node:test';
import DOMPurify from 'dompurify';
import {normalizeMermaidSource,renderMermaidOnce} from '../src/utils/mermaidSource.js';

// Mermaid's browser sanitizer needs a DOM, while these syntax tests run in Node.
// Use a pass-through sanitizer here; production rendering keeps Mermaid strict.
DOMPurify.addHook ??= () => {};
DOMPurify.sanitize ??= text => text;
const {default:mermaid}=await import('mermaid');

test('quotes labels with parentheses and filenames and separates adjacent node declarations',async()=>{
  const source='flowchart TD B_Server[Backend Server (index.js)] B_DB[(PostgreSQL)] B_Server --> B_DB';
  const normalized=normalizeMermaidSource(source);
  assert.match(normalized,/B_Server\["Backend Server \(index\.js\)"\]/);
  assert.match(normalized,/\]\n\s+B_DB\[\("PostgreSQL"\)\]/);
  assert.equal(Boolean(await mermaid.parse(normalized,{suppressErrors:true})),true);
});

test('normalizes fenced diagrams and safely quotes special node labels',async()=>{
  const normalized=normalizeMermaidSource('```mermaid\nflowchart LR\n  Api[POST /api/orders?status=paid]\n  Api --> Db[(Order Model.js)]\n```');
  assert.match(normalized,/Api\["POST \/api\/orders\?status=paid"\]/);
  assert.match(normalized,/Db\[\("Order Model\.js"\)\]/);
  assert.equal(Boolean(await mermaid.parse(normalized,{suppressErrors:true})),true);
});

test('invalid Mermaid syntax is rejected without surfacing parser details',async()=>{
  const normalized=normalizeMermaidSource('flowchart TD\n  A[Valid] -->');
  assert.equal(await mermaid.parse(normalized,{suppressErrors:true}),false);
});

test('the same invalid diagram is parsed only once across render attempts',async()=>{
  let parses=0,renders=0;
  const parser={
    initialize(){},
    async parse(){parses++;return false},
    async render(){renders++;return {svg:'<svg></svg>'}},
  };
  const [first,second]=await Promise.all([
    renderMermaidOnce('flowchart TD\n  A[broken --> B',parser),
    renderMermaidOnce('flowchart TD\n  A[broken --> B',parser),
  ]);
  assert.deepEqual(first,{error:true});
  assert.deepEqual(second,{error:true});
  assert.equal(parses,1);
  assert.equal(renders,0);
});

test('valid diagram rendering remains cached and renders once',async()=>{
  let parses=0,renders=0;
  const parser={
    initialize(){},
    async parse(){parses++;return {diagramType:'flowchart-v2'}},
    async render(){renders++;return {svg:'<svg id="valid"></svg>'}},
  };
  const first=await renderMermaidOnce('flowchart TD\n  A[Start] --> B[Done]',parser);
  const second=await renderMermaidOnce('flowchart TD\n  A[Start] --> B[Done]',parser);
  assert.deepEqual(first,{svg:'<svg id="valid"></svg>'});
  assert.equal(second,first);
  assert.equal(parses,1);
  assert.equal(renders,1);
});
