import {FileCode2} from 'lucide-react';
import {groupSourceRanges} from '../utils/sourceCitations';

export default function ReferencedSources({sources=[],onOpenSource}){
  const references=groupSourceRanges(sources);
  if(!references.length)return null;
  return <section className="referenced-sources" aria-label="Referenced Sources">
    <h3>Referenced Sources</h3>
    <ul>{references.map(file=><li key={file.file_path}>
      <span className="referenced-source-file"><FileCode2 size={13}/>{file.file_path}</span>
      <span className="referenced-source-ranges">{file.ranges.map(range=><button className="text-link source-reference-link" key={`${range.start}-${range.end}`} onClick={()=>onOpenSource?.(file.file_path,range.start)} aria-label={`Open ${file.file_path}, lines ${range.start} to ${range.end} in Code Explorer`}>Lines {range.start}–{range.end}</button>)}</span>
    </li>)}</ul>
  </section>;
}
