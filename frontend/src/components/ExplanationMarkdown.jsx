import {useMemo} from 'react';
import {marked} from 'marked';
import DOMPurify from 'dompurify';
import {extractSourceCitations,readableMarkdownCopy} from '../utils/sourceCitations';
import ReferencedSources from './ReferencedSources';

export {readableMarkdownCopy};

function renderMarkdown(markdown){
  const html=marked.parse(markdown,{gfm:true,breaks:false});
  return DOMPurify.sanitize(html,{USE_PROFILES:{html:true}});
}

export default function ExplanationMarkdown({content,sources=[],onOpenSource}){
  const {html,references}=useMemo(()=>{
    const extracted=extractSourceCitations(content,sources);
    return {html:renderMarkdown(extracted.markdown),references:extracted.references};
  },[content,sources]);
  return <div className="grounded-response">
    <div className="explanation-markdown" dangerouslySetInnerHTML={{__html:html}}/>
    <ReferencedSources sources={references.flatMap(file=>file.ranges.map(range=>({file_path:file.file_path,start_line:range.start,end_line:range.end})))} onOpenSource={onOpenSource}/>
  </div>;
}
