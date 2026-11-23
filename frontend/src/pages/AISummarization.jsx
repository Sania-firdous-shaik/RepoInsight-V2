import {useEffect,useMemo,useState} from 'react';
import {BookOpen,Check,Clipboard,LoaderCircle,RefreshCw,Sparkles} from 'lucide-react';
import {useNavigate} from 'react-router-dom';
import {useRepositoryPage} from '../context/WorkspaceContext';
import ExplanationMarkdown,{readableMarkdownCopy} from '../components/ExplanationMarkdown';
import {sourceExplorerUrl} from '../utils/sourceCitations';
import api from '../services/api';

const scopes=[['repository','Repository Summary'],['folder','Folder Summary'],['file','File Summary'],['module','Module Summary']];
const suggestions=['User authentication','Product management','Shopping cart','Order placement','Payment processing','Admin management','AI recommendations'];
export default function AISummarization(){
  const {repo}=useRepositoryPage(),navigate=useNavigate();
  const [scope,setScope]=useState('repository'),[paths,setPaths]=useState([]),[path,setPath]=useState(''),[topic,setTopic]=useState('');
  const [result,setResult]=useState(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[copied,setCopied]=useState(false);
  useEffect(()=>{let active=true;api.get(`/repositories/${repo.id}/files`).then(r=>{if(active)setPaths(r.data)}).catch(e=>{if(active)setError(e.response?.data?.detail||'Could not load indexed files.')});return()=>{active=false}},[repo.id]);
  const folders=useMemo(()=>[...new Set(paths.flatMap(file=>{const parts=file.split('/').slice(0,-1);return parts.map((_,i)=>parts.slice(0,i+1).join('/'))}))].sort(),[paths]);
  const fileOptions=scope==='file'?paths:folders;
  function changeScope(value){setScope(value);setPath('');setResult(null);setError('');}
  async function generate(){setBusy(true);setError('');setCopied(false);try{const response=await api.post(`/repositories/${repo.id}/summarize`,{scope,...(scope==='file'||scope==='folder'?{path}:{}),...(scope==='module'?{topic}:{})});setResult(response.data)}catch(e){setError(e.response?.data?.detail||e.message||'Summary generation failed.')}finally{setBusy(false)}}
  async function copy(){try{await navigator.clipboard.writeText(readableMarkdownCopy(result.summary,result.sources||[]));setCopied(true);setTimeout(()=>setCopied(false),1600)}catch{setError('Clipboard access is unavailable in this browser.')}}
  function openSource(file,line){navigate(sourceExplorerUrl(repo.id,file,line));}
  const ready=repo.status==='ready',hasScope=scope==='repository'||(scope==='module'?Boolean(topic.trim()):Boolean(path));
  return <>
    <div className="page-heading"><div><div className="eyebrow">REPOSITORY TOOL / AI SUMMARIZATION</div><h1>AI Summarization</h1><p className="muted">Summarize indexed implementation at repository, folder, file, or workflow level.</p></div></div>
    <section className="panel summary-controls"><div className="summary-control-grid"><label>Summary scope<select value={scope} onChange={e=>changeScope(e.target.value)}>{scopes.map(([value,label])=><option key={value} value={value}>{label}</option>)}</select></label>
      {(scope==='folder'||scope==='file')&&<label>{scope==='folder'?'Folder':'File'}<select value={path} onChange={e=>{setPath(e.target.value);setResult(null)}}><option value="">Select {scope}</option>{fileOptions.map(value=><option key={value} value={value}>{value}</option>)}</select></label>}
      {scope==='module'&&<label>Feature or workflow<input value={topic} onChange={e=>{setTopic(e.target.value);setResult(null)}} placeholder="e.g. order placement and payment"/></label>}
      <button className="primary summary-generate" onClick={generate} disabled={!ready||!hasScope||busy}>{busy?<LoaderCircle className="spin" size={15}/>:<Sparkles size={15}/>} {result?'Regenerate Summary':'Generate Summary'}</button>
    </div>{scope==='module'&&<div className="summary-suggestions"><span>Examples</span>{suggestions.map(item=><button key={item} onClick={()=>{setTopic(item);setResult(null)}}>{item}</button>)}</div>}
      {!ready&&<p className="summary-note">Repository indexing must finish before summaries can be generated.</p>}
    </section>
    {error&&<div className="error banner">{error}</div>}
    {busy&&<section className="panel summary-loading" role="status"><LoaderCircle className="spin" size={19}/><div><b>Building a source-grounded summary</b><p>Reviewing relevant files and combining their evidence…</p></div></section>}
    {result&&!busy&&<section className="panel summary-result"><header><div><BookOpen size={15}/><div><b>{scopes.find(([value])=>value===result.scope)?.[1]||'Summary'}</b><small>{result.target||repo.owner+'/'+repo.name}{result.cached?' · saved for this commit':''}</small></div></div><div className="summary-actions"><button className="outline compact" onClick={copy}>{copied?<Check size={13}/>:<Clipboard size={13}/>} {copied?'Copied':'Copy Summary'}</button><button className="outline compact" onClick={generate} disabled={busy}><RefreshCw size={13}/> Regenerate</button></div></header><div className="summary-markdown"><ExplanationMarkdown content={result.summary} sources={result.sources||[]} onOpenSource={openSource}/></div></section>}
  </>;
}
