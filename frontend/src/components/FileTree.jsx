import {useMemo,useState} from 'react';
import {ChevronDown,ChevronRight,FileCode2,Folder,FolderOpen,Search,MessageSquareText} from 'lucide-react';

function makeTree(paths){
  const root={name:'',path:'',children:new Map(),file:false};
  for(const filePath of paths){
    const parts=filePath.split('/').filter(Boolean);let node=root,current='';
    parts.forEach((part,i)=>{current=current?`${current}/${part}`:part;if(!node.children.has(part))node.children.set(part,{name:part,path:current,children:new Map(),file:i===parts.length-1});node=node.children.get(part);if(i===parts.length-1)node.file=true;});
  }
  return root;
}
function TreeNode({node,selectedPath,onSelect,onExplain,query}){
  const [open,setOpen]=useState(true);const matches=node.path.toLowerCase().includes(query.toLowerCase());
  const visible=[...node.children.values()].filter(child=>matches||!query||child.path.toLowerCase().includes(query.toLowerCase()));
  if(query&&!matches&&!visible.length)return null;
  if(node.file)return <div className={'tree-file '+(selectedPath===node.path?'selected':'')}><button className="tree-file-name" onClick={()=>onSelect(node.path)} title={node.path}><FileCode2 size={14}/><span>{node.name}</span></button><button className="tree-explain" aria-label={`Explain ${node.path}`} title="Explain file" onClick={()=>onExplain(node.path)}><MessageSquareText size={13}/></button></div>;
  return <div className="tree-folder"><button className="tree-folder-name" onClick={()=>setOpen(!open)}>{open?<ChevronDown size={13}/>:<ChevronRight size={13}/>} {open?<FolderOpen size={14}/>:<Folder size={14}/>}<span>{node.name}</span></button>{open&&<div className="tree-children">{visible.map(child=><TreeNode key={child.path} node={child} selectedPath={selectedPath} onSelect={onSelect} onExplain={onExplain} query={query}/>)}</div>}</div>;
}
export default function FileTree({paths=[],selectedPath='',onSelect=()=>{},onExplain=()=>{},title='Repository files'}){
  const [query,setQuery]=useState('');const tree=useMemo(()=>makeTree(paths),[paths]);
  return <section className="panel file-tree-panel"><div className="panel-head"><div><Folder size={15}/>{title}</div><span>{paths.length} files</span></div><label className="tree-search"><Search size={14}/><input value={query} onChange={e=>setQuery(e.target.value)} placeholder="Filter paths" aria-label="Filter files"/></label><div className="tree-scroll">{[...tree.children.values()].map(node=><TreeNode key={node.path} node={node} selectedPath={selectedPath} onSelect={onSelect} onExplain={onExplain} query={query}/>)}{!paths.length&&<div className="panel-empty">No indexed files are available yet.</div>}</div></section>;
}
