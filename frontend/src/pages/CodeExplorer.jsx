import {useEffect,useRef,useState} from 'react';
import {useNavigate,useSearchParams} from 'react-router-dom';
import {BookOpen,LoaderCircle,Sparkles} from 'lucide-react';
import FileTree from '../components/FileTree';
import SourceViewer from '../components/SourceViewer';
import ExplanationMarkdown from '../components/ExplanationMarkdown';
import {sourceExplorerUrl} from '../utils/sourceCitations';
import useIndexedSource from '../hooks/useIndexedSource';
import {useRepositoryPage} from '../context/WorkspaceContext';
import api from '../services/api';

export default function CodeExplorer(){
  const {repo}=useRepositoryPage(),navigate=useNavigate(),[params,setParams]=useSearchParams();
  const selected=params.get('file')||'',line=Number(params.get('line'))||0;
  const initialInsight=params.get('insight')==='summary'?'summary':params.get('insight')==='explanation'?'explanation':'explanation';
  const [paths,setPaths]=useState([]),[listError,setListError]=useState(''),[mode,setMode]=useState(initialInsight);
  const [insight,setInsight]=useState(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[revision,setRevision]=useState(()=>params.get('insight')?1:0);
  const requestId=useRef(0),source=useIndexedSource(repo.id,selected);
  useEffect(()=>{let active=true;api.get(`/repositories/${repo.id}/files`).then(r=>{if(active)setPaths(r.data)}).catch(e=>{if(active)setListError(e.response?.data?.detail||'Could not load indexed files.')});return()=>{active=false}},[repo.id]);
  useEffect(()=>{requestId.current+=1;setInsight(null);setError('');setBusy(false)},[selected]);
  useEffect(()=>{
    if(!selected||!revision||repo.status!=='ready')return;
    const current=++requestId.current;let active=true;setBusy(true);setError('');setInsight(null);
    const request=mode==='summary'
      ?api.post(`/repositories/${repo.id}/summarize`,{scope:'file',path:selected})
      :api.post(`/repositories/${repo.id}/explain-file`,{file_path:selected,mode:'detailed'});
    request.then(r=>{if(active&&current===requestId.current)setInsight({content:mode==='summary'?r.data.summary:r.data.explanation,sources:r.data.sources||[],cached:r.data.cached})})
      .catch(e=>{if(active&&current===requestId.current)setError(e.response?.data?.detail||e.message||`Could not ${mode==='summary'?'summarize':'explain'} this file.`)})
      .finally(()=>{if(active&&current===requestId.current)setBusy(false)});
    return()=>{active=false};
  },[repo.id,repo.status,selected,mode,revision]);
  function choose(path,highlight=0){setParams(path?{file:path,...highlight?{line:String(highlight)}:{}}:{});setRevision(0);setMode('explanation');}
  function requestInsight(nextMode,path=selected){if(!path)return;setMode(nextMode);if(path!==selected)setParams({file:path});setRevision(value=>value+1);}
  function openSource(path,startLine){navigate(sourceExplorerUrl(repo.id,path,startLine));}
  return <>
    <div className="page-heading"><div><div className="eyebrow">REPOSITORY TOOL / SOURCE</div><h1>Code Explorer</h1><p className="muted">Browse one indexed file, inspect its source, and ask for a file explanation or summary.</p></div></div>
    <div className="explanation-workflow code-explorer-workflow">
      <section className="explanation-step"><div className="explanation-step-heading"><span>01</span><div><h2>Repository Files</h2><p>Search, expand folders, and select a file.</p></div></div>{listError&&<div className="error banner">{listError}</div>}<FileTree paths={paths} selectedPath={selected} onSelect={path=>choose(path)} onExplain={path=>requestInsight('explanation',path)}/></section>
      <section className="explanation-step"><div className="explanation-step-heading"><span>02</span><div><h2>Source Code</h2><p>{selected||'Select a file above to load its source.'}</p></div></div><SourceViewer path={selected} chunks={source.chunks} busy={source.loading} onExplain={path=>requestInsight('explanation',path)} onSummarize={path=>requestInsight('summary',path)} insightBusy={busy} highlightLine={line}/>{source.error&&<div className="error banner">{source.error}</div>}</section>
      <section className="explanation-step"><div className="explanation-step-heading"><span>03</span><div><h2>AI Insights</h2><p>{selected?`Insights for ${selected}`:'Select a file, then request an explanation or summary.'}</p></div></div>
        <section className="panel explanation-panel code-insights-panel"><div className="insight-tabs" role="tablist" aria-label="AI insight type"><button className={mode==='explanation'?'active':''} role="tab" aria-selected={mode==='explanation'} onClick={()=>requestInsight('explanation')} disabled={!selected}><BookOpen size={14}/> Explanation</button><button className={mode==='summary'?'active':''} role="tab" aria-selected={mode==='summary'} onClick={()=>requestInsight('summary')} disabled={!selected}><Sparkles size={14}/> Summary</button></div>
          {!selected?<div className="explanation-empty">Choose a file to view its AI insights.</div>:busy?<div className="explanation-loading" role="status"><LoaderCircle className="spin" size={18}/><b>{mode==='summary'?'Summarizing':'Explaining'} {selected}</b><span>Using indexed source and related repository evidence…</span></div>:error?<div className="error explanation-error">{error}<button className="text-link" onClick={()=>requestInsight(mode)}>Try again</button></div>:insight?<div className="insight-result"><ExplanationMarkdown content={insight.content} sources={insight.sources} onOpenSource={openSource}/></div>:<div className="explanation-empty"><Sparkles size={17}/><span>Choose Explanation or Summary to generate source-grounded insights.</span></div>}
        </section>
        {selected&&repo.status!=='ready'&&<div className="indexing-note"><Sparkles size={14}/>{repo.status==='indexing'?'AI insights will be available when indexing finishes.':'The repository must be indexed before AI insights can be generated.'}</div>}
      </section>
    </div>
  </>;
}
