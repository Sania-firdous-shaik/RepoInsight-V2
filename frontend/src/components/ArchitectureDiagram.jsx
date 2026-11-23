import {useEffect,useState} from 'react';
import {normalizeMermaidSource,renderMermaidOnce} from '../utils/mermaidSource';

function withCanvasBackground(svg){
  if(!svg)return '';
  return svg.replace(/(<svg\b[^>]*>)/i,'$1<rect width="100%" height="100%" fill="#0a0a0a"/>');
}

export default function ArchitectureDiagram({code,onRenderedSvg}){
  const [svg,setSvg]=useState(''),[error,setError]=useState(''),[rendering,setRendering]=useState(false);
  const source=normalizeMermaidSource(code);
  useEffect(()=>{
    let active=true;setSvg('');setError('');setRendering(Boolean(source));onRenderedSvg?.('');
    if(!source){setRendering(false);return()=>{active=false};}
    import('mermaid').then(({default:mermaid})=>renderMermaidOnce(source,mermaid))
      .then(result=>{
        if(!active)return;
        if(result?.svg){const output=withCanvasBackground(result.svg);setSvg(output);onRenderedSvg?.(output);}
        else setError('The generated diagram could not be validated. Its source evidence is preserved, but no incomplete diagram was displayed.');
      })
      .catch(()=>{if(active)setError('The generated diagram could not be rendered. Its source evidence is preserved.');})
      .finally(()=>{if(active)setRendering(false);});
    return()=>{active=false};
  },[source,onRenderedSvg]);
  return <div className="architecture-diagram" aria-label="Generated architecture diagram">
    {rendering&&<div className="architecture-diagram-message" role="status">Validating and rendering the source-grounded diagram…</div>}
    {error&&<div className="architecture-diagram-message error" role="alert">{error}</div>}
    {svg&&<div className="architecture-diagram-svg" dangerouslySetInnerHTML={{__html:svg}}/>}
  </div>;
}
