import {useState} from 'react';
import {FileCode2,ArrowUpRight} from 'lucide-react';
import api from '../services/api';

export default function CitationEvidence({repoId,source,onOpen}){
  const [preview,setPreview]=useState(''),[loading,setLoading]=useState(false),[error,setError]=useState('');
  async function toggle(event){if(!event.currentTarget.open||preview||loading)return;setLoading(true);setError('');try{const r=await api.get(`/repositories/${repoId}/source`,{params:{path:source.file_path}});const chunk=r.data.chunks.find(c=>c.start_line<=source.end_line&&c.end_line>=source.start_line);setPreview(chunk?chunk.content:'Source excerpt unavailable.')}catch(e){setError(e.response?.data?.detail||'Could not load source preview.')}finally{setLoading(false)}}
  return <details className="citation-evidence" onToggle={toggle}><summary><FileCode2 size={13}/><span>{source.file_path}:{source.start_line}-{source.end_line}</span><ArrowUpRight size={12}/></summary><div className="citation-preview">{loading?'Loading source…':error?<span className="error-text">{error}</span>:<><pre>{preview}</pre><button className="text-link" onClick={()=>onOpen?.(source.file_path,source.start_line)}>Open in Code Explorer</button></>}</div></details>;
}
