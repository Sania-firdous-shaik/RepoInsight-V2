import assert from 'node:assert/strict';
import test from 'node:test';
import DOMPurify from 'dompurify';
import {architectureEvidence,architectureSummary,buildArchitectureMermaid,downloadArchitectureMermaid,
  downloadArchitectureSvg,downloadArchitecturePng} from '../src/utils/architectureMermaid.js';
import {normalizeMermaidSource} from '../src/utils/mermaidSource.js';

DOMPurify.addHook ??= () => {};
DOMPurify.sanitize ??= text => text;
const {default:mermaid}=await import('mermaid');

function graphFixture(){
  const sources=[
    {file_path:'web/src/App.jsx',start_line:1,end_line:8},
    {file_path:'api/app.py',start_line:1,end_line:18},
    {file_path:'api/models.py',start_line:3,end_line:10},
    {file_path:'api/train.py',start_line:1,end_line:12},
  ];
  return {version:1,repository:{id:4,name:'owner/sample-app'},metadata:{represented_source_files:4,ai_architecture_status:'interpreted'},sources,nodes:[
    {id:'client:browser',label:'Client / Browser',type:'client',citations:[sources[0]]},
    {id:'layer:frontend',label:'React Frontend',type:'layer',citations:[sources[0]],technologies:['React','Vite']},
    {id:'layer:backend',label:'Flask API',type:'layer',citations:[sources[1]]},
    {id:'file:api/predict.py',label:'predict.py',type:'route',parent_id:'layer:backend',path:'api/predict.py',citations:[sources[1]]},
    {id:'database:postgres',label:'PostgreSQL',type:'database',citations:[sources[2]]},
    {id:'external:stripe',label:'Stripe',type:'external',citations:[sources[1]]},
    {id:'artifact:models/model.pkl',label:'Prediction Model',type:'artifact',architecture_component:true,
      architecture_order:2,responsibility:'Returns a probability for the transformed request.',artifact_path:'models/model.pkl',citations:[sources[1]]},
    {id:'technology:joblib',label:'joblib',type:'technology',parent_id:'layer:backend',citations:[sources[1]]},
  ],edges:[
    {id:'e1',source:'client:browser',target:'layer:frontend',type:'renders_in',verified:true,citations:[sources[0]]},
    {id:'e2',source:'layer:frontend',target:'file:api/predict.py',type:'api_call',label:'HTTP POST /predict',verified:true,citations:[sources[0],sources[1]]},
    {id:'e3',source:'file:api/predict.py',target:'database:postgres',type:'connects_to',label:'Result',verified:true,citations:[sources[1],sources[2]]},
    {id:'e4',source:'file:api/predict.py',target:'artifact:models/model.pkl',type:'uses_model',label:'Model inference',verified:true,citations:[sources[1]]},
    {id:'e5',source:'file:api/predict.py',target:'external:stripe',type:'calls_external',label:'Payment processing',verified:true,citations:[sources[1]]},
  ]};
}

test('turns source-cited architecture evidence into valid, deterministic Mermaid flowchart',async()=>{
  const graph=graphFixture(),first=buildArchitectureMermaid(graph),second=buildArchitectureMermaid(graph);
  assert.equal(first.source,second.source);
  assert.match(first.source,/flowchart TD/);
  assert.match(first.source,/PostgreSQL/);
  assert.match(first.source,/Payment processing/);
  assert.match(first.source,/HTTP \/predict/);
  assert.doesNotMatch(first.source,/joblib/,'ordinary library technology is not promoted to a major component');
  assert.doesNotMatch(first.source,/\|Result\|/,'generic labels are omitted without deleting their relation');
  assert.equal(Boolean(await mermaid.parse(normalizeMermaidSource(first.source),{suppressErrors:true})),true);
  assert.equal(first.edges.some(edge=>edge.source==='layer:frontend'&&edge.target==='layer:backend'),true);
  assert.equal(first.edges.some(edge=>edge.source==='layer:backend'&&edge.target==='database:postgres'),true);
  assert.equal(first.edges.some(edge=>edge.source==='layer:backend'&&edge.target==='database:postgres'&&edge.label===''),true,
    'the generic visible label is omitted while the verified direction remains');
  assert.equal(graph.edges.find(edge=>edge.id==='e3').label,'Result','the source graph and relationship metadata are preserved');
});

test('keeps model pipeline stages and complete source citations available to the explanation',()=>{
  const graph=graphFixture();
  graph.nodes.push({id:'process:preprocess',label:'Data Preprocessor',type:'service',architecture_component:true,architecture_order:1,
    responsibility:'Transforms request features before inference.',citations:[graph.sources[3]]});
  graph.edges.push({id:'e6',source:'process:preprocess',target:'artifact:models/model.pkl',type:'uses_model',label:'Transformed features',verified:true,citations:[graph.sources[3]]});
  const diagram=buildArchitectureMermaid(graph),evidence=architectureEvidence(graph,diagram);
  assert.ok(diagram.nodes.some(node=>node.id==='process:preprocess'));
  assert.match(diagram.source,/Transformed features/);
  assert.ok(evidence.some(item=>item.file_path==='api/train.py'));
  assert.match(architectureSummary(graph,diagram),/source-grounded/i);
});

test('exports the same rendered SVG and Mermaid source without empty downloads',async()=>{
  const downloads=[];
  const browser={document:{body:{appendChild(){}},createElement(){return {style:{},click(){downloads.push(this.download)},remove(){}}}},
    URL:{createObjectURL(){return 'blob:preview'},revokeObjectURL(){}},setTimeout(callback){callback();}};
  assert.equal(downloadArchitectureMermaid('', 'repo',browser),false);
  assert.equal(downloadArchitectureSvg('', 'repo',browser),false);
  assert.equal(downloadArchitectureMermaid('flowchart TD\n A-->B','owner/sample-app',browser),true);
  assert.equal(downloadArchitectureSvg('<svg></svg>','owner/sample-app',browser),true);
  assert.deepEqual(downloads,['sample-app_Architecture.mmd','sample-app_Architecture.svg']);

  let clicked=false;
  class ImageMock{set src(_value){queueMicrotask(()=>this.onload())}naturalWidth=320;naturalHeight=240;}
  const pngBrowser={...browser,Image:ImageMock,document:{...browser.document,createElement(tag){
    if(tag==='canvas')return {width:0,height:0,getContext(){return {fillRect(){},scale(){},drawImage(){}}},toBlob(callback){callback(new Blob(['png']))}};
    return browser.document.createElement(tag);
  }},URL:{createObjectURL(){return 'blob:preview'},revokeObjectURL(){}},setTimeout(callback){callback();}};
  const originalCreate=pngBrowser.document.createElement;
  pngBrowser.document.createElement=function(tag){if(tag==='a')return {style:{},click(){clicked=true;downloads.push(this.download)},remove(){}};return originalCreate(tag)};
  assert.equal(await downloadArchitecturePng('<svg width="320" height="240"></svg>','sample-app',pngBrowser),true);
  assert.equal(clicked,true);
  assert.equal(downloads.at(-1),'sample-app_Architecture.png');
});
