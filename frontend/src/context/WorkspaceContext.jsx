import {createContext,useContext,useEffect,useState} from 'react';
import {Outlet,useOutletContext,useParams} from 'react-router-dom';
import AppShell from '../components/AppShell';
import api from '../services/api';

const WorkspaceContext=createContext(null);
export function WorkspaceProvider({user,onLogout}){
  const [repos,setRepos]=useState([]),[selectedRepoId,setSelectedRepoId]=useState(localStorage.getItem('selected_repo_id')||''),[error,setError]=useState('');
  async function refresh(){try{const result=await api.get('/repositories');setRepos(result.data);setError('');if(selectedRepoId&&!result.data.some(r=>String(r.id)===String(selectedRepoId))){setSelectedRepoId('');localStorage.removeItem('selected_repo_id')} }catch(e){setError(e.response?.data?.detail||e.message||'Could not load repositories.')}}
  useEffect(()=>{refresh();const timer=setInterval(refresh,5000);return()=>clearInterval(timer)},[selectedRepoId]);
  function selectRepo(id){const value=String(id||'');setSelectedRepoId(value);if(value)localStorage.setItem('selected_repo_id',value);else localStorage.removeItem('selected_repo_id')}
  const context={repos,selectedRepoId,selectRepo,refresh,error,user,onLogout};
  return <WorkspaceContext.Provider value={context}><Outlet/></WorkspaceContext.Provider>;
}
export function useWorkspace(){return useContext(WorkspaceContext)}
export function GlobalLayout(){const {user,onLogout,repos,selectedRepoId,selectRepo}=useWorkspace();return <AppShell user={user} onLogout={onLogout} repos={repos} selectedRepoId={selectedRepoId} onRepoChange={selectRepo}><Outlet/></AppShell>}
export function RepositoryLayout(){
  const {repoId}=useParams();const {user,onLogout,repos,selectRepo}=useWorkspace();const [repo,setRepo]=useState(null),[error,setError]=useState('');
  useEffect(()=>{selectRepo(repoId);setRepo(null);api.get(`/repositories/${repoId}`).then(r=>setRepo(r.data)).catch(e=>setError(e.response?.data?.detail||'Could not load repository.'))},[repoId]);
  return <AppShell user={user} onLogout={onLogout} repos={repos} selectedRepoId={repoId} repoId={repoId} onRepoChange={selectRepo}><div className="repo-page-context">{error?<div className="error banner">{error}</div>:!repo?<div className="loading-inline">Loading repository…</div>:<Outlet context={{repo,setRepo}}/>}</div></AppShell>;
}
export function useRepositoryPage(){return useOutletContext();}
