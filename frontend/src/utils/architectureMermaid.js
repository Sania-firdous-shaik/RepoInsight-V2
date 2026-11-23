import {normalizeMermaidSource} from './mermaidSource.js';
import {validateArchitectureGraph} from './architectureValidation.js';

const HIGH_LEVEL_TYPES=new Set(['layer','client','database','external']);
const GENERIC_LABELS=new Set(['features','data','input','output','process','result','uses','connects to','depends on']);
const RELATION_LABELS={api_call:'HTTP requests',connects_to:'Database access',calls_external:'External service',renders_in:'',
  uses_service:'',uses_model:'',references_model:'',uses_middleware:'',imports:'',implemented_with:''};

function safeLabel(value){
  return String(value||'').replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;').replaceAll('>','&gt;')
    .replaceAll('|','&#124;').replace(/[\r\n]+/g,' ').trim();
}

function relationLabel(edge){
  let label=String(edge.label||RELATION_LABELS[edge.type]||'').trim();
  label=label.replace(/^HTTP\s+(GET|POST|PUT|PATCH|DELETE)\s+/i,'HTTP ');
  const concise={'Create & verify payment':'Payment processing','Browser checkout':'Browser checkout',
    'JSON response':'JSON response','HTTP API requests':'HTTP requests','prediction probability':'Prediction probability',
    'engineered features':'','transformed features':'Transformed features','engineers features':'Feature engineering',
    'risk result':'Risk result','preprocesses input':'Preprocesses input','runs inference':'Model inference',
    'email delivery':'Email delivery','ai requests':'AI requests'};
  label=concise[label.toLowerCase()]??label;
  return GENERIC_LABELS.has(label.toLowerCase())?'':label;
}

function architectureNodes(graph){
  const all=graph.nodes;
  const selected=all.filter(node=>HIGH_LEVEL_TYPES.has(node.type)||node.architecture_component===true);
  const selectedIds=new Set(selected.map(node=>node.id));
  const byId=new Map(all.map(node=>[node.id,node]));
  const nodes=selected.slice().sort((a,b)=>{
    const order=node=>Number.isFinite(node.architecture_order)?node.architecture_order:
      node.type==='client'?-3:node.type==='layer'&&/front|client|ui/i.test(node.label)?-2:
      node.type==='layer'?-1:node.type==='database'||node.type==='external'?100:50;
    return order(a)-order(b)||a.label.localeCompare(b.label)||a.id.localeCompare(b.id);
  });
  const ids=new Map(nodes.map((node,index)=>[node.id,`C${index+1}`]));
  const resolve=id=>{
    let current=byId.get(id),seen=new Set();
    while(current&&!seen.has(current.id)){
      if(selectedIds.has(current.id))return current.id;
      seen.add(current.id);current=byId.get(current.parent_id);
    }
    return null;
  };
  return {nodes,ids,resolve};
}

export function buildArchitectureMermaid(graph){
  validateArchitectureGraph(graph);
  const {nodes,ids,resolve}=architectureNodes(graph);
  const edgesByKey=new Map();
  for(const edge of graph.edges){
    if(edge.verified!==true||!edge.citations?.length)continue;
    const source=resolve(edge.source),target=resolve(edge.target);
    if(!source||!target||source===target)continue;
    const label=relationLabel(edge),key=`${source}:${target}:${label}`;
    const existing=edgesByKey.get(key)||{source,target,label,citations:[]};
    existing.citations.push(...edge.citations);edgesByKey.set(key,existing);
  }
  const edges=[...edgesByKey.values()].sort((a,b)=>ids.get(a.source).localeCompare(ids.get(b.source))||
    ids.get(a.target).localeCompare(ids.get(b.target))||a.label.localeCompare(b.label));
  const declarations=nodes.map(node=>{
    const id=ids.get(node.id),label=safeLabel(node.label);
    return node.type==='database'?`  ${id}[("${label}")]`:`  ${id}["${label}"]`;
  });
  const relations=edges.map(edge=>`  ${ids.get(edge.source)} -->${edge.label?`|${safeLabel(edge.label)}|`:''} ${ids.get(edge.target)}`);
  const classes=nodes.map(node=>{
    const className=node.type==='database'?'store':node.type==='external'?'integration':node.type==='artifact'?'artifact':'component';
    return `  class ${ids.get(node.id)} ${className};`;
  });
  const source=[
    '%%{init: {"theme":"base","flowchart":{"curve":"basis","nodeSpacing":42,"rankSpacing":52,"htmlLabels":true},"themeVariables":{"background":"#0a0a0a","primaryColor":"#202020","primaryTextColor":"#d5d5d5","primaryBorderColor":"#a0a0a0","lineColor":"#a0a0a0","secondaryColor":"#202020","tertiaryColor":"#202020","fontFamily":"Inter, Arial, sans-serif"}}}%%',
    'flowchart TD',
    ...declarations,
    ...relations,
    '  classDef component fill:#202020,stroke:#a0a0a0,color:#d5d5d5,stroke-width:1px;',
    '  classDef store fill:#202020,stroke:#a0a0a0,color:#d5d5d5,stroke-width:1px;',
    '  classDef integration fill:#202020,stroke:#a0a0a0,color:#d5d5d5,stroke-width:1px;',
    '  classDef artifact fill:#202020,stroke:#a0a0a0,color:#d5d5d5,stroke-width:1px;',
    ...classes,
  ].join('\n');
  return {source:normalizeMermaidSource(source),nodes,edges};
}

export function architectureEvidence(graph,diagram){
  const rows=[];
  const add=(label,citations)=>{
    for(const citation of citations||[])rows.push({label,file_path:citation.file_path,start_line:citation.start_line,end_line:citation.end_line});
  };
  for(const node of diagram.nodes)add(node.label,node.citations);
  add('Architecture summary',graph.summary_sources||[]);
  const visibleById=new Map(diagram.nodes.map(node=>[node.id,node]));
  for(const edge of diagram.edges){
    const sourceNode=visibleById.get(edge.source),targetNode=visibleById.get(edge.target);
    add(`${sourceNode?.label||'Component'} → ${targetNode?.label||'Component'}`,edge.citations);
  }
  const unique=new Map();
  for(const row of rows){const key=`${row.label}:${row.file_path}:${row.start_line}-${row.end_line}`;unique.set(key,row);}
  return [...unique.values()];
}

export function architectureSummary(graph,diagram){
  if(typeof graph.summary==='string'&&graph.summary.trim())return graph.summary.trim();
  const relationships=diagram.edges.length;
  return `This source-grounded overview includes ${diagram.nodes.length} major components and ${relationships} verified relationships from ${graph.metadata?.represented_source_files??graph.sources.length} indexed source files.`;
}

function triggerDownload(blob,filename,browser=globalThis){
  if(!browser?.document||!browser?.URL?.createObjectURL)return false;
  const url=browser.URL.createObjectURL(blob),anchor=browser.document.createElement('a');
  anchor.href=url;anchor.download=filename;anchor.style.display='none';browser.document.body.appendChild(anchor);anchor.click();anchor.remove();
  browser.setTimeout(()=>browser.URL.revokeObjectURL(url),0);return true;
}

export function architectureFilename(repositoryName,extension){
  const safe=String(repositoryName||'repository').split('/').pop().replace(/[^a-z0-9._-]+/gi,'_').replace(/^_+|_+$/g,'')||'repository';
  return `${safe}_Architecture.${extension}`;
}

export function downloadArchitectureMermaid(source,repositoryName,browser=globalThis){
  if(!source?.trim())return false;
  return triggerDownload(new Blob([source],{type:'text/plain;charset=utf-8'}),architectureFilename(repositoryName,'mmd'),browser);
}

export function downloadArchitectureSvg(svg,repositoryName,browser=globalThis){
  if(!svg?.includes('<svg'))return false;
  return triggerDownload(new Blob([svg],{type:'image/svg+xml;charset=utf-8'}),architectureFilename(repositoryName,'svg'),browser);
}

export async function downloadArchitecturePng(svg,repositoryName,browser=globalThis){
  if(!svg?.includes('<svg')||!browser?.Image||!browser?.document)return false;
  const url=browser.URL.createObjectURL(new Blob([svg],{type:'image/svg+xml;charset=utf-8'}));
  try{
    const image=new browser.Image();
    await new Promise((resolve,reject)=>{image.onload=resolve;image.onerror=reject;image.src=url;});
    const width=image.naturalWidth||image.width,height=image.naturalHeight||image.height;
    if(!width||!height)return false;
    const canvas=browser.document.createElement('canvas');canvas.width=width*2;canvas.height=height*2;
    const context=canvas.getContext('2d');if(!context)return false;
    context.fillStyle='#0a0a0a';context.fillRect(0,0,canvas.width,canvas.height);context.scale(2,2);context.drawImage(image,0,0,width,height);
    const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
    return blob?triggerDownload(blob,architectureFilename(repositoryName,'png'),browser):false;
  }finally{browser.URL.revokeObjectURL(url);}
}
