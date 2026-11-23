import {useEffect,useState} from 'react';
import {Link,Navigate,Route,Routes,useNavigate,useParams,useSearchParams} from 'react-router-dom';
import {ArrowUpRight,Code2,LoaderCircle,ShieldCheck} from 'lucide-react';
import api from './services/api';
import './workspace.css';
import './workspace-mobile.css';
import {GlobalLayout,RepositoryLayout,WorkspaceProvider} from './context/WorkspaceContext';
import Overview from './pages/Overview';
import MyRepositories from './pages/MyRepositories';
import Settings from './pages/Settings';
import RepositoryOverview from './pages/RepositoryOverview';
import Assistant from './pages/Assistant';
import CodeExplorer from './pages/CodeExplorer';
import AISummarization from './pages/AISummarization';
import CodeReview from './pages/CodeReview';
import SemanticSearch from './pages/SemanticSearch';
import Architecture from './pages/Architecture';
import Documentation from './pages/Documentation';

function message(error){return error.response?.data?.detail||error.message||'Something went wrong.'}
function Auth({onLogin}){
  const [register,setRegister]=useState(false),[email,setEmail]=useState(''),[password,setPassword]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false);
  async function submit(event){event.preventDefault();setBusy(true);setError('');try{let result;if(register)result=await api.post('/auth/register',{email,password});else{const form=new URLSearchParams({username:email,password});result=await api.post('/auth/login',form,{headers:{'Content-Type':'application/x-www-form-urlencoded'}})}localStorage.setItem('token',result.data.access_token);onLogin()}catch(e){setError(message(e))}finally{setBusy(false)}}
  return <main className="auth-page"><section className="auth-art"><Link to="/" className="brand"><span className="brand-icon"><Code2 size={18}/></span> repo<span>insight</span></Link><div className="hero-copy"><div className="eyebrow">CODEBASE INTELLIGENCE</div><h1>Understand the<br/><em>whole system.</em></h1><p>Explore code, map relationships, and get answers grounded in repository evidence.</p><div className="mini-graph"><div className="graph-lines"/><span className="node n1"><Code2/></span><span className="node n2"><Code2/></span><span className="node n3"><Code2/></span><div className="graph-label">REPOSITORY MAP <b>READY</b></div></div></div><div className="auth-foot">BUILT FOR THE WAY CODE REALLY WORKS <span>V 2.0.0</span></div></section><section className="auth-form-wrap"><form className="auth-form" onSubmit={submit}><div className="eyebrow">WELCOME {register?'ABOARD':'BACK'}</div><h2>{register?'Create your workspace':'Sign in to RepoInsight'}</h2><p className="muted">{register?'Start exploring a repository in minutes.':'Your codebase knowledge, ready when you are.'}</p><label>Email address<input type="email" autoComplete="email" required placeholder="you@company.com" value={email} onChange={e=>setEmail(e.target.value)}/></label><label>Password<input type="password" autoComplete={register?'new-password':'current-password'} minLength="10" required placeholder="At least 10 characters" value={password} onChange={e=>setPassword(e.target.value)}/></label>{error&&<div className="error">{error}</div>}<button className="primary wide" disabled={busy}>{busy?<LoaderCircle className="spin" size={16}/>:register?'Create account':'Sign in'} <ArrowUpRight size={15}/></button><p className="auth-switch">{register?'Already have an account?':'New to RepoInsight?'} <button type="button" onClick={()=>setRegister(!register)}>{register?'Sign in':'Create account'}</button></p><div className="secure"><ShieldCheck size={14}/> Your repositories remain private to your account</div></form></section></main>;
}

function LegacyExplanationRedirect(){const {repoId}=useParams();const [params]=useSearchParams();const query=params.toString();return <Navigate to={`/repos/${repoId}/explorer${query?`?${query}`:''}`} replace/>;}
function AuthenticatedApp({user,setUser}){
  const navigate=useNavigate();
  function logout(){localStorage.removeItem('token');localStorage.removeItem('selected_repo_id');setUser(null);navigate('/')}
  return <Routes><Route element={<WorkspaceProvider user={user} onLogout={logout}/> }><Route element={<GlobalLayout/>}><Route path="/" element={<Overview/>}/><Route path="/repositories" element={<MyRepositories/>}/><Route path="/settings" element={<Settings/>}/></Route><Route path="/repos/:repoId" element={<RepositoryLayout/>}><Route index element={<Navigate to="overview" replace/>}/><Route path="overview" element={<RepositoryOverview/>}/><Route path="assistant" element={<Assistant/>}/><Route path="explorer" element={<CodeExplorer/>}/><Route path="explain" element={<LegacyExplanationRedirect/>}/><Route path="review" element={<CodeReview/>}/><Route path="summarize" element={<AISummarization/>}/><Route path="search" element={<SemanticSearch/>}/><Route path="architecture" element={<Architecture/>}/><Route path="docs" element={<Documentation/>}/></Route></Route><Route path="*" element={<Navigate to="/" replace/>}/></Routes>;
}

export default function App(){
  const [user,setUser]=useState(null),[ready,setReady]=useState(false);
  useEffect(()=>{if(localStorage.getItem('token'))api.get('/auth/me').then(r=>setUser(r.data)).catch(()=>{localStorage.removeItem('token');localStorage.removeItem('selected_repo_id')}).finally(()=>setReady(true));else setReady(true)},[]);
  if(!ready)return <div className="loading"><LoaderCircle className="spin"/> Starting RepoInsight…</div>;
  if(!user)return <Auth onLogin={()=>api.get('/auth/me').then(r=>setUser(r.data)).catch(()=>{localStorage.removeItem('token')})}/>;
  return <AuthenticatedApp user={user} setUser={setUser}/>;
}
