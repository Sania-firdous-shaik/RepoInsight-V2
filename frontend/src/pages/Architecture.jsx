import {useCallback,useEffect,useMemo,useState} from 'react';
import {useNavigate} from 'react-router-dom';
import {AlertTriangle,ArrowDownToLine,RefreshCw,Workflow} from 'lucide-react';
import '../architecture-explorer.css';
import {useRepositoryPage} from '../context/WorkspaceContext';
import ArchitectureDiagram from '../components/ArchitectureDiagram';
import ReferencedSources from '../components/ReferencedSources';
import api from '../services/api';
import {validateArchitectureGraph} from '../utils/architectureValidation';
import {architectureEvidence,architectureSummary,buildArchitectureMermaid,downloadArchitectureMermaid,
  downloadArchitecturePng,downloadArchitectureSvg} from '../utils/architectureMermaid';
import {sourceExplorerUrl} from '../utils/sourceCitations';

const AI_UNAVAILABLE=new Set(['rate_limited_deterministic_fallback','deterministic_fallback','not_configured','no_selected_evidence','partially_interpreted']);
const AI_FAILURE_LABELS={model_not_configured:'Groq is not configured',rate_limited:'Groq rate limit reached',
  provider_unavailable:'Groq is temporarily unavailable',provider_rejected_request:'Groq rejected the request',
  invalid_json:'Groq returned invalid JSON',truncated_output:'Groq output was truncated',
  invalid_components_or_relationships:'Groq response omitted the required architecture fields',
  response_not_object:'Groq response was not a JSON object',invalid_response_schema:'Groq response had an invalid schema',
  evidence_validation_rejected:'Generated components or relationships did not pass source verification',
  unsupported_claims_filtered:'Unsupported AI components or relationships were filtered out',
  no_source_evidence:'No eligible indexed source excerpts were available',
  unexpected_provider_or_validation_error:'Architecture interpretation failed unexpectedly'};

export default function Architecture(){
  const {repo}=useRepositoryPage(),navigate=useNavigate();
  const [graph,setGraph]=useState(null),[error,setError]=useState(''),[busy,setBusy]=useState(false),[svg,setSvg]=useState(''),[retryWait,setRetryWait]=useState(0);
  useEffect(()=>{setGraph(null);setError('');setSvg('');setBusy(false);setRetryWait(0);},[repo?.id,repo?.status]);
  useEffect(()=>{if(!retryWait)return;const timer=setTimeout(()=>setRetryWait(value=>Math.max(0,value-1)),1000);return()=>clearTimeout(timer);},[retryWait]);

  const generate=useCallback(async()=>{
    if(!repo?.id||repo.status!=='ready'||busy||retryWait>0)return;
    setBusy(true);setError('');setGraph(null);setSvg('');
    try{
      const response=await api.get(`/repositories/${repo.id}/architecture-graph`);
      setGraph(validateArchitectureGraph(response.data));
      setRetryWait(0);
    }catch(requestError){
      setError(requestError.response?.data?.detail||requestError.message||'The source-grounded architecture could not be generated.');
      setRetryWait(10);
    }finally{setBusy(false);}
  },[repo?.id,repo?.status,busy,retryWait]);

  const diagram=useMemo(()=>graph?buildArchitectureMermaid(graph):null,[graph]);
  const evidence=useMemo(()=>graph&&diagram?architectureEvidence(graph,diagram):[],[graph,diagram]);
  const summary=useMemo(()=>graph&&diagram?architectureSummary(graph,diagram):'', [graph,diagram]);
  const technologiesByNode=useMemo(()=>{
    if(!graph)return new Map();
    const names=new Map(graph.nodes.map(node=>[node.id,node.label])),result=new Map();
    for(const edge of graph.edges.filter(item=>item.type==='implemented_with')){
      if(!names.has(edge.target))continue;
      result.set(edge.source,[...new Set([...(result.get(edge.source)||[]),names.get(edge.target)])]);
    }
    return result;
  },[graph]);
  const onRenderedSvg=useCallback(value=>setSvg(value),[]);
  const openSource=citation=>navigate(sourceExplorerUrl(repo.id,citation.file_path,citation.start_line));
  const unavailable=AI_UNAVAILABLE.has(graph?.metadata?.ai_architecture_status);
  const aiFailure=AI_FAILURE_LABELS[graph?.metadata?.ai_architecture_failure_category];
  const genericOnly=diagram?.nodes.length===1&&/^(application modules|shared modules|backend application|frontend application)$/i.test(diagram.nodes[0].label)&&diagram.edges.length===0;
  const aiStatus=graph?.metadata?.ai_architecture_status==='rate_limited_deterministic_fallback'?'Groq rate limit reached; this diagram uses deterministic source-verified relationships.':
    graph?.metadata?.ai_architecture_status==='not_configured'?'Groq is not configured; this diagram uses deterministic source-verified relationships.':
    graph?.metadata?.ai_architecture_status==='no_selected_evidence'?'No suitable source excerpts were available for AI interpretation; deterministic relationships are shown.':
    graph?.metadata?.ai_architecture_status==='deterministic_fallback'?`${aiFailure||'AI interpretation failed'}; deterministic source-verified relationships are shown.`:
    graph?.metadata?.ai_architecture_status==='partially_interpreted'?`${aiFailure||'Unsupported AI claims were filtered'}; remaining verified relationships are shown.`:
    genericOnly?'The indexed graph exposes only a generic application boundary; no unsupported components or relationships were added.':
    unavailable?'AI interpretation is unavailable; this diagram uses only deterministic, source-verified relationships.':
    graph?.metadata?.ai_architecture_status?.startsWith('interpreted')?'AI interpretation was checked against indexed source evidence.':
    'Architecture was assembled from indexed source evidence.';

  return <main className="architecture-explorer architecture-generator">
    <header className="page-heading architecture-generator-heading">
      <div><div className="eyebrow">REPOSITORY TOOL / SYSTEM MAP</div><h1>Architecture Explorer</h1>
        <p className="muted">Generate a source-grounded architecture diagram for your repository.</p>
      </div>
      <button className="primary architecture-generate-button" onClick={generate} disabled={busy||retryWait>0||repo?.status!=='ready'}>
        {busy?<RefreshCw size={14} className="spin"/>:<Workflow size={14}/>}{busy?'Generating Architecture':retryWait?`Retry in ${retryWait}s`:'Generate Architecture'}
      </button>
    </header>
    {!repo&&<section className="panel architecture-generator-empty"><h2>Select a repository</h2><p className="muted">Choose an indexed repository to generate its source-grounded architecture.</p></section>}
    {repo?.status!=='ready'&&<section className="panel architecture-generator-empty"><h2>Indexing must finish first</h2><p className="muted">Architecture generation uses the repository’s active indexed source.</p></section>}
    {error&&<div className="error banner" role="alert">{error}</div>}
    {busy&&<section className="panel architecture-loading" role="status"><RefreshCw size={17} className="spin"/> Building the diagram from indexed source evidence…</section>}
    {!busy&&graph&&diagram&&<>
      <section className="architecture-generator-status panel" aria-live="polite">
        <span className={unavailable||genericOnly?'architecture-status-warning':'architecture-status-ready'}>{(unavailable||genericOnly)&&<AlertTriangle size={14}/>}<span>{aiStatus}</span></span>
        <span>{graph.metadata.represented_source_files} indexed source files · {diagram.nodes.length} components · {diagram.edges.length} verified relationships</span>
      </section>
      {!diagram.nodes.length?<section className="panel architecture-generator-empty"><h2>No architecture components were supported by the indexed evidence</h2><p className="muted">No generic diagram was substituted. Review the indexed files and generate again after the source evidence is available.</p></section>:<>
        <section className="panel architecture-render-panel" aria-label="Generated architecture diagram">
          <ArchitectureDiagram code={diagram.source} onRenderedSvg={onRenderedSvg}/>
        </section>
        <section className="panel architecture-generator-summary"><div className="eyebrow">ARCHITECTURE SUMMARY</div><p>{summary}</p></section>
        <section className="panel architecture-generator-components"><div className="eyebrow">VERIFIED COMPONENTS</div><div className="architecture-component-list">
          {diagram.nodes.map(node=>{
            const technologies=[...new Set([...(node.technologies||[]),...(technologiesByNode.get(node.id)||[])])];
            return <article key={node.id}><div><h2>{node.label}</h2><span>{node.type}</span></div>
              {node.responsibility&&<p>{node.responsibility}</p>}
              {!!technologies.length&&<small>Technologies: {technologies.join(' · ')}</small>}
            </article>;
          })}
        </div></section>
        <section className="panel architecture-generator-evidence">
          <div className="architecture-evidence-heading"><div><p>Indexed files supporting displayed components and relationships.</p></div>
            <details className="architecture-export-menu"><summary><ArrowDownToLine size={13}/> Export</summary><div>
              <button onClick={()=>downloadArchitecturePng(svg,repo.name)} disabled={!svg}>Download PNG</button>
              <button onClick={()=>downloadArchitectureSvg(svg,repo.name)} disabled={!svg}>Download SVG</button>
              <button onClick={()=>downloadArchitectureMermaid(diagram.source,repo.name)}>Download Mermaid (.mmd)</button>
            </div></details>
          </div>
          {!evidence.length?<p className="muted">No file citations were returned for the displayed architecture.</p>:<ReferencedSources sources={evidence} onOpenSource={(path,line)=>openSource({file_path:path,start_line:line})}/>}
        </section>
      </>}
      {graph.metadata.omitted_source_files>0&&<p className="architecture-limit-note"><AlertTriangle size={13}/> Indexed source coverage is bounded; {graph.metadata.omitted_source_files} files were not represented in the source graph.</p>}
    </>}
  </main>;
}
