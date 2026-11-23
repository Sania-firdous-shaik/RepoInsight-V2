import {useEffect,useState} from 'react';
import api from '../services/api';

export default function useIndexedSource(repoId,path){
  const [chunks,setChunks]=useState([]),[loading,setLoading]=useState(false),[error,setError]=useState('');
  useEffect(()=>{let active=true;if(!path){setChunks([]);setError('');return()=>{active=false}}setLoading(true);setError('');api.get(`/repositories/${repoId}/source`,{params:{path}}).then(r=>{if(active)setChunks(r.data.chunks||[])}).catch(e=>{if(active)setError(e.response?.data?.detail||e.message||'Could not load indexed source.')}).finally(()=>{if(active)setLoading(false)});return()=>{active=false}},[repoId,path]);
  return {chunks,loading,error};
}
