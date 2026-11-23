const MARKER=/^S\d+(?:\s*[-‐‑‒–—]\s*S\d+|\s*,\s*S\d+)*$/;

function markerIds(value){
  const ids=[];
  for(const part of value.split(/\s*,\s*/)){
    const numbers=[...part.matchAll(/S(\d+)/g)].map(match=>Number(match[1]));
    if(numbers.length===2&&/[-‐‑‒–—]/.test(part)&&numbers[1]>=numbers[0]&&numbers[1]-numbers[0]<=200){
      for(let id=numbers[0];id<=numbers[1];id++)ids.push(`S${id}`);
    }else ids.push(...numbers.map(id=>`S${id}`));
  }
  return ids;
}

function matchingSource(sourceMap,file,start,end){
  return sourceMap.find(source=>source.file_path===file&&Number(source.start_line)<=end&&Number(source.end_line)>=start);
}

function consumeCitation(raw,sources,sourceMap,used){
  const inner=raw.slice(1,-1).trim();
  if(MARKER.test(inner)){
    for(const id of markerIds(inner))if(sourceMap.has(id))used.push(sourceMap.get(id));
    return true;
  }
  const match=inner.match(/^(.+?)\s*[·|—-]\s*lines?\s+(\d+)\s*[-–—]\s*(\d+)$/i);
  if(!match)return false;
  const start=Number(match[2]),end=Number(match[3]);
  if(!start||end<start)return false;
  const source=matchingSource(sources,match[1].trim(),start,end)||sources.find(item=>item.file_path?.split('/').at(-1)===match[1].trim()&&Number(item.start_line)<=end&&Number(item.end_line)>=start);
  if(source)used.push({...source,start_line:start,end_line:end});
  return true;
}

// Scans prose while leaving fenced/inline code and ordinary Markdown links intact.
export function extractSourceCitations(markdown,sources=[]){
  const sourceMap=new Map(sources.filter(source=>source?.citation_id).map(source=>[source.citation_id,source]));
  const used=[];const lines=String(markdown||'').split('\n');let fenced=null;
  const clean=lines.map(line=>{
    const usedBefore=used.length;
    const fence=line.match(/^\s*(```+|~~~+)/);
    if(fenced){if(fence&&fence[1][0]===fenced.char&&fence[1].length>=fenced.length)fenced=null;return line;}
    if(fence){fenced={char:fence[1][0],length:fence[1].length};return line;}
    let out='',i=0,codeTicks=0;
    while(i<line.length){
      if(line[i]==='`'){
        let end=i+1;while(line[end]==='`')end++;
        const length=end-i;
        if(!codeTicks)codeTicks=length;else if(codeTicks===length)codeTicks=0;
        out+=line.slice(i,end);i=end;continue;
      }
      if(!codeTicks&&line[i]==='['){
        let slashes=0;for(let back=i-1;back>=0&&line[back]==='\\';back--)slashes++;
        if(slashes%2){out+=line[i++];continue;}
        const end=line.indexOf(']',i+1);
        if(end!==-1){
          const raw=line.slice(i,end+1),after=line.slice(end+1);
          // [text](url) and [text][label] are links, even if their label resembles a citation.
          if(!/^\s*(?:\(|\[)/.test(after)&&consumeCitation(raw,sources,sourceMap,used)){
            if(/^[.,;:!?]/.test(after))out=out.replace(/[ \t]+$/,'');
            i=end+1;continue;
          }
          out+=raw;i=end+1;continue;
        }
      }
      out+=line[i++];
    }
    return used.length>usedBefore&&/^\s*[-*+]\s*$/.test(out)?'':out;
  }).join('\n');
  // API responses already contain the validated source set. Include it even if an older
  // generated answer omitted its marker, without altering the answer or its verification.
  const all=[...used,...sources.filter(source=>source&&source.file_path&&Number(source.start_line)>0&&Number(source.end_line)>=Number(source.start_line))];
  const references=groupSourceRanges(all);
  let normalized=clean;
  if(references.length){
    normalized=normalized.split('\n').filter(line=>!/^\s{0,3}#{1,6}\s+referenced sources\s*#*\s*$/i.test(line)).join('\n');
  }
  return {markdown:normalized,references};
}

export function groupSourceRanges(sources=[]){
  const files=new Map();
  for(const source of sources){
    if(!source?.file_path)continue;
    const start=Number(source.start_line),end=Number(source.end_line);
    if(!Number.isInteger(start)||!Number.isInteger(end)||start<1||end<start)continue;
    if(!files.has(source.file_path))files.set(source.file_path,[]);
    files.get(source.file_path).push({start,end});
  }
  return [...files].sort(([a],[b])=>a.localeCompare(b)).map(([file_path,ranges])=>{
    ranges.sort((a,b)=>a.start-b.start||a.end-b.end);const merged=[];
    for(const range of ranges){const last=merged.at(-1);if(last&&range.start<=last.end)last.end=Math.max(last.end,range.end);else merged.push({...range});}
    return {file_path,ranges:merged};
  });
}

export function readableMarkdownCopy(markdown,sources=[]){
  const {markdown:clean,references}=extractSourceCitations(markdown,sources);
  if(!references.length)return clean;
  return `${clean.trimEnd()}\n\n### Referenced Sources\n\n${references.map(file=>`- ${file.file_path} — ${file.ranges.map(range=>`Lines ${range.start}–${range.end}`).join(', ')}`).join('\n')}`;
}

export function sourceExplorerUrl(repoId,filePath,line){
  return `/repos/${repoId}/explorer?file=${encodeURIComponent(filePath)}&line=${Number(line)||1}`;
}
