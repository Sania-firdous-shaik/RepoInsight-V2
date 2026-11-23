import hashlib, json, logging, re
from datetime import datetime, timezone, timedelta
from typing import Literal
import threading
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
from app.db.session import get_db, SessionLocal
from app.models.models import User, Repository, Conversation, ChatMessage, GeneratedDocument
from app.core.security import hash_password, verify_password, create_token
from app.api.deps import current_user
from app.services.ingestion.indexer import parse_github_url, index_repository
from app.services.embeddings.service import embed_texts
from app.services.vector_store.qdrant import search
from app.services.ai.groq_service import complete, complete_with_metadata, GroqRateLimitError
from app.services.ai.review_scheduler import review_scheduler, ReviewRateLimitExhausted
from app.core.config import settings
from app.services.retrieval.context_builder import build_context, estimate_tokens, split_hits_for_budget
from app.services.retrieval.query_analysis import analyze_query, documentation_retrieval_query
from app.services.retrieval.workflow_context import (authentication_workflow_index_inventory, authentication_workflow_roles,
    authentication_workflow_stage_requirements, business_workflow_stage_requirements,
    format_authentication_workflow_inventory, missing_authentication_workflow_evidence)
from app.services.retrieval.grounded_answer import IncompleteGroundedAnswer, append_not_found_notes, generate_grounded_answer, render_grounded_answer
from app.services.retrieval.pipeline import SearchHit
from app.services.architecture.graph import cached_architecture_graph
from app.services.architecture.generator import interpret_architecture_graph
from app.services.vector_store.qdrant import file_points, repository_files, repository_points, delete_stale_generations

logger = logging.getLogger(__name__)

def extract_citation_ids(text:str)->set[str]:
    """Read common ASCII and full-width citation styles, including compact ranges."""
    found=set()
    pattern=r"[\[【［]\s*((?:S\d+)(?:\s*[-‐‑‒–—]\s*S\d+|\s*,\s*S\d+)*)\s*[\]】］]"
    for expression in re.findall(pattern,text):
        parts=re.split(r"\s*,\s*",expression)
        for part in parts:
            numbers=[int(value) for value in re.findall(r"S(\d+)",part)]
            if len(numbers)==2 and re.search(r"[-‐‑‒–—]",part):
                start,end=numbers
                if end>=start and end-start<=200:
                    found.update(f"S{number}" for number in range(start,end+1))
            else:
                found.update(f"S{number}" for number in numbers)
    return found

router=APIRouter(prefix="/api")
class Register(BaseModel): email: EmailStr; password: str=Field(min_length=10,max_length=128)
class RepoCreate(BaseModel): url: str; branch: str|None=None
class Ask(BaseModel): question: str=Field(min_length=3,max_length=4000); conversation_id: int|None=None
class SearchRequest(BaseModel): query: str=Field(min_length=2,max_length=1000)
class FileExplanationRequest(BaseModel): file_path: str=Field(min_length=1,max_length=1000); mode: Literal['detailed','overview']='detailed'
class SummaryRequest(BaseModel):
    scope: Literal['repository','folder','file','module']
    path: str|None=Field(default=None,max_length=1000)
    topic: str|None=Field(default=None,max_length=500)
class ReviewRequest(BaseModel):
    scope: Literal['file','repository']='file'
    file_path: str|None=Field(default=None,max_length=1000)
    review_id: int|None=None
    categories: list[Literal['Security','Bug','Performance','Code Quality','Error Handling','Best Practices']]=Field(default_factory=lambda:['Security','Bug','Performance','Code Quality','Error Handling','Best Practices'],max_length=6)
    review_id: int|None=None

REVIEW_CATEGORIES=('Security','Bug','Performance','Code Quality','Error Handling','Best Practices')
REVIEW_SEVERITIES=('Critical','High','Medium','Low','Suggestion')
REVIEW_SOURCE_EXTENSIONS={'.py','.pyi','.js','.jsx','.mjs','.cjs','.ts','.tsx','.go','.rs','.java','.kt','.kts','.scala','.c','.h','.cc','.cpp','.hpp','.cs','.php','.rb','.swift','.sh','.sql','.vue','.svelte'}

def _is_reviewable_source_path(path:str)->bool:
    import os
    normalized=path.replace('\\','/').lower();parts=normalized.split('/');name=parts[-1]
    excluded={'node_modules','vendor','dist','build','coverage','.git','.venv','venv','__pycache__','.next','target','out','generated','generated_code','.cache'}
    if os.path.splitext(name)[1] not in REVIEW_SOURCE_EXTENSIONS or any(part in excluded for part in parts):return False
    if name.endswith(('.min.js','.min.css','.generated.js','.generated.ts')):return False
    if re.search(r'(?:secret|credential|private.?key)',name):return False
    return True

def _redact_review_secrets(text:str)->str:
    # Keep assignment shape and line count while removing likely credential values before Groq sees them.
    pattern=r"(?i)((?:api[_-]?key|secret|password|token|private[_-]?key)\s*[:=]\s*)(['\"])([^'\"]{6,})(\2)"
    return re.sub(pattern,lambda match:f"{match.group(1)}{match.group(2)}[REDACTED]{match.group(4)}",text)

def _review_static_findings(path:str,source:str,categories:list[str])->list[dict]:
    """Small deterministic checks; evidence is derived from actual source and secrets are redacted."""
    rules=[]
    if 'Security' in categories:
        rules += [(re.compile(r'\beval\s*\('),'Dynamic code evaluation','High','Security','Untrusted input passed to eval can execute arbitrary code.'),
                  (re.compile(r'(?:verify|rejectUnauthorized)\s*:\s*false'),'TLS certificate verification disabled','High','Security','Disabling certificate verification can allow man-in-the-middle attacks.'),
                  (re.compile(r'(?i)(?:api[_-]?key|secret|password|token)\s*[:=]\s*[\'\"](?!process\.env|os\.environ)[^\'\"]{8,}[\'\"]'),'Possible hardcoded credential','High','Security','A credential embedded in source may be exposed through repository access or distribution.')]
    if 'Bug' in categories:
        rules += [(re.compile(r'\bJSON\.parse\s*\('),'JSON parsing may throw','Low','Bug','Invalid input causes a runtime exception unless this parse is guarded.')]
    findings=[]
    for line_no,line in enumerate(source.splitlines(),1):
        for pattern,title,severity,category,impact in rules:
            if pattern.search(line):
                findings.append({'id':f'static-{line_no}-{len(findings)}','title':title,'severity':severity,'category':category,
                    'file_path':path,'start_line':line_no,'end_line':line_no,'description':'A potentially risky pattern was found by a deterministic source check.',
                    'impact':impact,'recommendation':'Review this operation and validate or constrain its inputs; use a safer alternative where possible.',
                    'suggested_code':'','confidence':0.78,'evidence':'Potential hardcoded credential assignment (value redacted).' if title=='Possible hardcoded credential' else line.strip()[:240],
                    'source':'static'})
    return findings

def _parse_review_json(raw:str)->list:
    text=raw.strip().lstrip('\ufeff');decoder=json.JSONDecoder();cursor=0;last_error=None
    while True:
        start=text.find('{',cursor)
        if start<0:break
        try:data,_end=decoder.raw_decode(text,start)
        except json.JSONDecodeError as exc:last_error=exc;cursor=start+1;continue
        if isinstance(data,dict) and isinstance(data.get('findings'),list):return data['findings']
        cursor=start+1
    if last_error:raise last_error
    raise ValueError('Expected a JSON object containing a findings array')

def _validate_review_findings(items:list,path:str,source:str,categories:list[str],offset:int=0)->list[dict]:
    lines=source.splitlines(); normalized=[]
    category_alias={'Bug Detection':'Bug','Security Review':'Security','Performance Review':'Performance','Code Quality':'Code Quality','Best Practices':'Best Practices'}
    for index,item in enumerate(items):
        if not isinstance(item,dict): continue
        category=category_alias.get(str(item.get('category','')),str(item.get('category','')))
        if category not in categories or item.get('file_path',path)!=path: continue
        try: start=int(item.get('start_line')); end=int(item.get('end_line',start))-offset
        except (TypeError,ValueError): continue
        start-=offset
        if start<1 or end<start or end>len(lines): continue
        severity=item.get('severity','Suggestion')
        if severity not in REVIEW_SEVERITIES: severity='Suggestion'
        confidence=item.get('confidence',0.5)
        try: confidence=max(0.0,min(1.0,float(confidence)))
        except (TypeError,ValueError): confidence=0.5
        snippet='\n'.join(lines[start-1:end])[:1200]
        normalized.append({'id':str(item.get('id') or f'ai-{start}-{index}'),'title':str(item.get('title') or 'Potential issue')[:180],
            'severity':severity,'category':category,'file_path':path,'start_line':start,'end_line':end,
            'description':str(item.get('description') or '')[:1200],'impact':str(item.get('impact') or '')[:800],
            'recommendation':str(item.get('recommendation') or '')[:1200],'suggested_code':str(item.get('suggested_code') or '')[:3000],
            'confidence':confidence,'evidence':snippet,'source':'ai'} )
    return normalized

def _review_ai_batch(repo,path,source,categories,offset=0,related_context=''):
    lines=source.splitlines()
    numbered='\n'.join(f'{offset+i}: {line}' for i,line in enumerate((_redact_review_secrets(line) for line in lines),1))
    system=("Review only the provided source for the requested categories. Return a compact JSON object only, shaped as {\"findings\":[{\"title\":str,\"severity\":\"Critical|High|Medium|Low|Suggestion\",\"category\":str,\"file_path\":str,\"start_line\":int,\"end_line\":int,\"description\":str,\"impact\":str,\"recommendation\":str,\"suggested_code\":str,\"confidence\":number}]}. "
        "At most 2 high-confidence findings for this source batch. Keep descriptions, impact, and recommendations to one concise sentence; leave suggested_code empty unless a small safe patch is clear. "
        "Only report actionable issues supported by exact source lines. Use the exact supplied path and absolute line numbers. Do not invent behavior or findings. Never reproduce secrets. Categories: "+'; '.join(categories))
    prompt=f"File: {path}\nNumbered source:\n{numbered}"
    if related_context: prompt+=f"\n\nRelated indexed repository context (may clarify behavior; never use it for finding locations):\n{related_context}"
    raw=review_scheduler.run(lambda:complete(system,prompt,max_retries=0,raise_rate_limit=True,json_mode=True))
    try: items=_parse_review_json(raw)
    except (ValueError,TypeError):
        raw=review_scheduler.run(lambda:complete(system+" If uncertain, return an empty findings array.",prompt+"\nReturn only compact valid JSON; do not add Markdown fences or commentary.",max_retries=0,raise_rate_limit=True,json_mode=True))
        try: items=_parse_review_json(raw)
        except (ValueError,TypeError): raise HTTPException(502,'AI review returned an invalid structured response. Please retry.')
    return _validate_review_findings(items,path,source,categories,offset)

def _review_file(repo,path,categories):
    points=file_points(repo.id,path,repo.index_generation)
    if not points: raise HTTPException(404,'File is not present in the selected repository index')
    points.sort(key=lambda p:(int(p.payload.get('start_line',0)),str(p.id)))
    chunks=[]; seen=set()
    for point in points:
        content=str(point.payload.get('content','')); digest=point.payload.get('content_hash') or hashlib.sha256(content.encode()).hexdigest()
        if digest not in seen: chunks.append((int(point.payload.get('start_line',1)),content)); seen.add(digest)
    # Keep each call within a modest context budget, preserving absolute line numbers.
    source_lines={}
    for start,content in chunks:
        for i,line in enumerate(content.splitlines()): source_lines.setdefault(start+i,line)
    if not _meaningful_review_source(source_lines):
        return _static_review_map(file_path,source_lines,categories),'',False
    line_numbers=sorted(source_lines)
    import_lines=[line.strip() for line in source_lines.values() if re.match(r"\s*(?:import\s|from\s+\S+\s+import|const\s+.*?require\()",line)]
    related_context=''
    if import_lines:
        related_query=(f"implementation and behavior connected to {path} "+' '.join(import_lines[:20]))[:1200]
        related_hits=[hit for hit in search(repo.id,embed_texts([related_query])[0],related_query,limit=6,index_generation=repo.index_generation)
                      if hit.payload.get('file_path')!=path]
        related_context=_redact_review_secrets(build_context(related_hits,min(1000,max(400,settings.rag_context_token_budget//8)))['context'])
    all_findings=[]; batches=[]; pending=[]; pending_chars=0; ai_warning='';rate_limited=False
    for line_no in line_numbers:
        line_chars=len(source_lines[line_no][:1200])+16
        if pending and (len(pending)>=120 or pending_chars+line_chars>12000):
            batches.append(pending);pending=[];pending_chars=0
        pending.append(line_no);pending_chars+=line_chars
    if pending:batches.append(pending)
    for batch_index,batch_nums in enumerate(batches):
        base=batch_nums[0]; batch='\n'.join(source_lines[n][:1200] for n in batch_nums)
        try: findings=_review_ai_batch(repo,path,batch,categories,base-1,related_context if batch_index==0 else '')
        except ReviewRateLimitExhausted as exc:
            ai_warning=f"Groq rate limit retry limit reached; retry after {exc.retry_after or 'a short delay'} seconds."
            rate_limited=True
            break
        except HTTPException as exc:
            ai_warning=exc.detail
            break
        # Validate generated snippet against exact indexed lines (including possible sparse chunks).
        for finding in findings:
            finding['start_line']+=base-1; finding['end_line']+=base-1
            first=finding['start_line']; last=finding['end_line']
            finding['evidence']=_redact_review_secrets('\n'.join(source_lines.get(n,'') for n in range(first,last+1)))[:1200]
        all_findings.extend(findings)
    static=[]
    static_rules=[]
    if 'Security' in categories:
        static_rules += [(re.compile(r'\beval\s*\('),'Dynamic code evaluation','High','Security','Untrusted input passed to eval can execute arbitrary code.'),
            (re.compile(r'(?:verify|rejectUnauthorized)\s*:\s*false'),'TLS certificate verification disabled','High','Security','Disabling certificate verification can allow man-in-the-middle attacks.'),
            (re.compile(r'(?i)(?:api[_-]?key|secret|password|token)\s*[:=]\s*[\'\"](?!process\.env|os\.environ)[^\'\"]{8,}[\'\"]'),'Possible hardcoded credential','High','Security','A credential embedded in source may be exposed through repository access or distribution.')]
    if 'Bug' in categories: static_rules.append((re.compile(r'\bJSON\.parse\s*\('),'JSON parsing may throw','Low','Bug','Invalid input causes a runtime exception unless this parse is guarded.'))
    for line_no,line in source_lines.items():
        for pattern,title,severity,category,impact in static_rules:
            if pattern.search(line):
                static.append({'id':f'static-{line_no}-{len(static)}','title':title,'severity':severity,'category':category,'file_path':path,
                    'start_line':line_no,'end_line':line_no,'description':'A potentially risky pattern was found by a deterministic source check.',
                    'impact':impact,'recommendation':'Review this operation and validate or constrain its inputs; use a safer alternative where possible.',
                    'suggested_code':'','confidence':0.78,'evidence':'Potential hardcoded credential assignment (value redacted).' if title=='Possible hardcoded credential' else _redact_review_secrets(line.strip())[:240],'source':'static'})
    return all_findings+static,ai_warning,rate_limited

_active_review_jobs=set()
_active_review_jobs_lock=threading.Lock()

def _review_source_lines(repo,path):
    points=file_points(repo.id,path,repo.index_generation)
    if not points:return {}
    points.sort(key=lambda p:(int(p.payload.get('start_line',0)),str(p.id)))
    result={};seen=set()
    for point in points:
        content=str(point.payload.get('content',''));digest=point.payload.get('content_hash') or hashlib.sha256(content.encode()).hexdigest()
        if digest in seen:continue
        seen.add(digest);start=int(point.payload.get('start_line',1))
        for offset,line in enumerate(content.splitlines()):result.setdefault(start+offset,line)
    return result

def _meaningful_review_source(lines):
    return any((line:=value.strip()) and not re.match(r'^(?:#|//|/\*|\*|\*/)',line) and re.search(r'[A-Za-z_$][\w$]*|[{}()=<>;]',line) for value in lines.values())

def _static_review_map(path,lines,categories):
    patterns=[]
    if 'Security' in categories:
        patterns += [(re.compile(r'\beval\s*\('),'Dynamic code evaluation','High','Security','Untrusted input passed to eval can execute arbitrary code.'),
          (re.compile(r'(?:verify|rejectUnauthorized)\s*:\s*false'),'TLS certificate verification disabled','High','Security','Disabling certificate verification can allow man-in-the-middle attacks.'),
          (re.compile(r'(?i)(?:api[_-]?key|secret|password|token)\s*[:=]\s*[\'\"](?!process\.env|os\.environ)[^\'\"]{8,}[\'\"]'),'Possible hardcoded credential','High','Security','A credential embedded in source may be exposed through repository access or distribution.')]
    if 'Bug' in categories:patterns.append((re.compile(r'\bJSON\.parse\s*\('),'JSON parsing may throw','Low','Bug','Invalid input causes a runtime exception unless this parse is guarded.'))
    findings=[];file_key=hashlib.sha256(path.encode()).hexdigest()[:8]
    for line_no,line in lines.items():
        for pattern,title,severity,category,impact in patterns:
            if pattern.search(line):findings.append({'id':f'static-{file_key}-{line_no}-{len(findings)}','title':title,'severity':severity,'category':category,'file_path':path,
                'start_line':line_no,'end_line':line_no,'description':'A potentially risky pattern was found by a deterministic source check.','impact':impact,
                'recommendation':'Review this operation and validate or constrain its inputs; use a safer alternative where possible.','suggested_code':'',
                'confidence':.78,'evidence':'Potential hardcoded credential assignment (value redacted).' if title=='Possible hardcoded credential' else _redact_review_secrets(line.strip())[:240],'source':'static'})
    return findings

def _review_ai_sources(repo,sources,categories):
    sections=[f"FILE {path}\n"+'\n'.join(f"{line_no}: {_redact_review_secrets(text)[:1200]}" for line_no,text in sorted(lines.items())) for path,lines in sources.items()]
    system=("Review only the supplied source for actionable issues in the requested categories. Return a compact JSON object only as {\"findings\":[{\"title\":str,\"severity\":\"Critical|High|Medium|Low|Suggestion\",\"category\":str,\"file_path\":str,\"start_line\":int,\"end_line\":int,\"description\":str,\"impact\":str,\"recommendation\":str,\"suggested_code\":str,\"confidence\":number}]}. "
      "Return at most 2 high-confidence findings per FILE section. Keep descriptions, impact, and recommendations to one concise sentence; leave suggested_code empty unless a small safe patch is clear. "
      "Use exactly a supplied FILE path and absolute source line numbers. Every finding must be supported by the cited lines; do not infer missing documentation or invent behavior. Never reproduce credentials. Categories: "+'; '.join(categories))
    prompt='Review these indexed excerpts independently by FILE path:\n\n'+'\n\n'.join(sections)
    try:items=_parse_review_json(review_scheduler.run(lambda:complete(system,prompt,max_retries=0,raise_rate_limit=True,json_mode=True)))
    except (ValueError,TypeError):
        try:items=_parse_review_json(review_scheduler.run(lambda:complete(system,prompt+'\nReturn only the compact specified JSON object without Markdown fences. If no findings, use {"findings":[]}.',max_retries=0,raise_rate_limit=True,json_mode=True)))
        except (ValueError,TypeError):raise HTTPException(502,'AI review returned invalid structured output. Resume or retry the review.')
    normalized=[];aliases={'Bug Detection':'Bug','Security Review':'Security','Performance Review':'Performance'}
    for idx,item in enumerate(items):
        if not isinstance(item,dict):continue
        path=item.get('file_path');lines=sources.get(path);category=aliases.get(str(item.get('category','')),str(item.get('category','')))
        if not lines or category not in categories:continue
        try:start=int(item.get('start_line'));end=int(item.get('end_line',start))
        except (TypeError,ValueError):continue
        if end<start or end-start>80 or any(n not in lines for n in range(start,end+1)):continue
        severity=item.get('severity','Suggestion')
        if severity not in REVIEW_SEVERITIES:severity='Suggestion'
        try:confidence=max(0.0,min(1.0,float(item.get('confidence',.5))))
        except (TypeError,ValueError):confidence=.5
        normalized.append({'id':str(item.get('id') or f'ai-{path}-{start}-{idx}'),'title':str(item.get('title') or 'Potential issue')[:180],
            'severity':severity,'category':category,'file_path':path,'start_line':start,'end_line':end,'description':str(item.get('description') or '')[:1200],
            'impact':str(item.get('impact') or '')[:800],'recommendation':str(item.get('recommendation') or '')[:1200],
            'suggested_code':str(item.get('suggested_code') or '')[:3000],'confidence':confidence,
            'evidence':_redact_review_secrets('\n'.join(lines[n] for n in range(start,end+1)))[:1200],'source':'ai'})
    return normalized

def _repository_review_paths(repo):
    excluded=re.compile(r'(^|/)(node_modules|vendor|dist|build|coverage|\.git|\.venv|venv|__pycache__)(/|$)|(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|.*\.min\.(js|css))$',re.I)
    sensitive=re.compile(r'(^|/)(\.env[^/]*|.*(secret|credential|private.?key).*)$',re.I)
    return [path for path in repository_files(repo.id,repo.index_generation) if _is_reviewable_source_path(path) and not excluded.search(path) and not sensitive.search(path)]

def _review_job_payload(record):
    files=record.get('files',[])
    progress={'total':len(files),'deterministic_reviewed':sum(x.get('deterministic_status')=='completed' for x in files),
      'ai_reviewed':sum(x.get('ai_status')=='completed' for x in files),'waiting':sum(x.get('ai_status') in ('waiting','pending') for x in files),
      'skipped':sum(x.get('ai_status')=='skipped' for x in files),'rate_limited':len(set(record.get('rate_limited_files',[]))|{x.get('path') for x in files if x.get('ai_status')=='rate_limited'}),
      'failed':sum(x.get('ai_status')=='failed' for x in files)}
    findings=[];seen=set()
    for item in files:
        for finding in item.get('findings',[]):
            key=(finding.get('file_path'),finding.get('start_line'),str(finding.get('title','')).lower())
            if key not in seen:seen.add(key);findings.append(finding)
    findings.sort(key=lambda item:(REVIEW_SEVERITIES.index(item.get('severity','Suggestion')) if item.get('severity') in REVIEW_SEVERITIES else len(REVIEW_SEVERITIES),str(item.get('file_path','')),int(item.get('start_line') or 0)))
    status=record.get('status','queued')
    summary=record.get('summary')
    if not summary and status=='rate_limited':summary=f"Partial review paused by Groq rate limits: {progress['deterministic_reviewed']} files checked deterministically; {progress['ai_reviewed']} completed by AI."
    elif not summary and status=='failed':summary=f"Partial review paused after an AI error: {progress['deterministic_reviewed']} files checked deterministically; {progress['ai_reviewed']} completed by AI."
    elif not summary and status!='completed':summary=f"Review {status}: {progress['deterministic_reviewed']} of {progress['total']} files checked deterministically; {progress['ai_reviewed']} reviewed by AI."
    elif not summary:summary=f"Review completed for {progress['total']} files."
    public_record={key:value for key,value in record.items() if key not in ('files_by_path','files')}
    public_record['files']=[{key:item.get(key) for key in ('path','deterministic_status','ai_status','skip_reason','ai_batches_total','ai_batches_completed') if key in item} for item in files]
    return {**public_record,'findings':findings,'progress':progress,'summary':summary,'can_resume':status in ('rate_limited','failed','interrupted')}

def _save_review_job(db,row,record):
    record['heartbeat_at']=datetime.now(timezone.utc).isoformat()
    row.content=json.dumps(record);db.commit()

def _review_batch_key(batch):
    return hashlib.sha256(json.dumps([(path,start,end) for path,start,end in batch],separators=(',',':')).encode()).hexdigest()[:24]

def _make_review_batches(repo,record,on_progress=lambda:None):
    """Index deterministic findings and create source-range descriptors, retaining no corpus in memory."""
    context_limit=max(1000,int(settings.review_ai_max_input_chars))
    digest_to_path={};files=record['files_by_path'];small=[];large=[];already=set(record.get('completed_batches',[]))
    for item in files.values():item['ai_batches_total']=0;item['ai_batches_completed']=[]
    for path,item in files.items():
        lines=_review_source_lines(repo,path)
        if not lines:
            item.update(deterministic_status='skipped',ai_status='skipped',skip_reason='No indexed source chunks.');on_progress();continue
        if not item.get('deterministic_done'):
            item['findings']=_static_review_map(path,lines,record['categories']);item['deterministic_done']=True;item['deterministic_status']='completed'
        if not _meaningful_review_source(lines):
            item.update(ai_status='skipped',skip_reason='No meaningful source statements.');on_progress();continue
        digest=hashlib.sha256('\n'.join(lines[n] for n in sorted(lines)).encode()).hexdigest()
        if digest in digest_to_path:
            item.update(ai_status='skipped',skip_reason=f"Duplicate source content; AI context is covered by {digest_to_path[digest]}.");on_progress();continue
        digest_to_path[digest]=path
        chunks=[];current=[];size=0
        for n,line in sorted(lines.items()):
            cost=len(line[:1200])+16
            if current and (len(current)>=180 or size+cost>context_limit):chunks.append(current);current=[];size=0
            current.append(n);size+=cost
        if current:chunks.append(current)
        descriptors=[(path,numbers[0],numbers[-1]) for numbers in chunks]
        if len(chunks)==1 and size<=min(5000,context_limit):small.append((descriptors[0],size))
        else:large.extend((descriptor,size) for descriptor,size in zip(descriptors,[sum(len(lines[n][:1200])+16 for n in chunk) for chunk in chunks]))
        item['ai_status']='waiting';on_progress()
    batches=[];group=[];size=0
    for descriptor,cost in small:
        path=descriptor[0];parent=lambda value:value.rsplit('/',1)[0] if '/' in value else ''
        same_folder=not group or parent(path)==parent(group[0][0])
        if group and (len(group)>=4 or size+cost>context_limit or not same_folder):batches.append(group);group=[];size=0
        group.append(descriptor);size+=cost
    if group:batches.append(group)
    batches.extend([[descriptor] for descriptor,_ in large])
    for batch in batches:
        key=_review_batch_key(batch)
        for path,_,_ in batch:
            item=files[path];item['ai_batches_total']+=1
            if key in already:item['ai_batches_completed'].append(key)
            else:item['ai_status']='waiting'
            if item['ai_batches_total'] and len(item['ai_batches_completed'])>=item['ai_batches_total']:item['ai_status']='completed'
    return batches

def _run_repository_review(review_id):
    with _active_review_jobs_lock:
        if review_id in _active_review_jobs:return
        _active_review_jobs.add(review_id)
    db=SessionLocal()
    try:
        row=db.get(GeneratedDocument,review_id)
        if not row or row.kind!='review_v2':return
        record=json.loads(row.content);record['files_by_path']={item['path']:item for item in record.get('files',[])};repo=db.get(Repository,row.repository_id)
        if not repo or repo.commit_sha!=record.get('commit_sha') or repo.index_generation!=record.get('index_generation'):
            record['status']='failed';record['warning']='The indexed commit changed; start a new review for the current source.';_save_review_job(db,row,record);return
        record['status']='running';record['warning']=None;_save_review_job(db,row,record)
        batches=_make_review_batches(repo,record,lambda:_save_review_job(db,row,record));_save_review_job(db,row,record)
        completed=set(record.get('completed_batches',[]))
        for batch in batches:
            key=_review_batch_key(batch)
            if key in completed:continue
            source_maps={}
            for path,start,end in batch:
                if path not in source_maps:source_maps[path]=_review_source_lines(repo,path)
                source_maps[path]={n:line for n,line in source_maps[path].items() if start<=n<=end}
            try:findings=_review_ai_sources(repo,source_maps,record['categories'])
            except ReviewRateLimitExhausted as exc:
                for path,_,_ in batch:record['files_by_path'][path]['ai_status']='rate_limited'
                record['rate_limited_files']=sorted(set(record.get('rate_limited_files',[]))|{path for path,_,_ in batch})
                record['status']='rate_limited';record['warning']=f"Groq rate limit retry limit reached. Retry after {int(exc.retry_after or 0)} seconds, then resume this review.";_save_review_job(db,row,record);return
            except HTTPException as exc:
                for path,_,_ in batch:record['files_by_path'][path]['ai_status']='failed'
                record['status']='failed';record['warning']=f"AI analysis paused: {exc.detail} Resume to continue unfinished work.";_save_review_job(db,row,record);return
            for finding in findings:record['files_by_path'][finding['file_path']]['findings'].append(finding)
            completed.add(key);record['completed_batches']=sorted(completed)
            for path,_,_ in batch:
                item=record['files_by_path'][path];item.setdefault('ai_batches_completed',[]).append(key)
                item['ai_status']='waiting'
                if len(item['ai_batches_completed'])>=item.get('ai_batches_total',1):item['ai_status']='completed'
            record['rate_limited_files']=sorted(set(record.get('rate_limited_files',[]))-{path for path,_,_ in batch})
            _save_review_job(db,row,record)
        record['status']='completed';record['warning']=None
        record['summary']=f"Review completed: {sum(x.get('deterministic_status')=='completed' for x in record['files'])} files checked deterministically; {sum(x.get('ai_status')=='completed' for x in record['files'])} received AI analysis."
        _save_review_job(db,row,record)
    except Exception:
        logger.exception('Repository review job %s failed',review_id)
        try:
            row=db.get(GeneratedDocument,review_id)
            if row and row.kind=='review_v2':
                record=json.loads(row.content);record['status']='failed';record['warning']='Review worker stopped unexpectedly. Resume to continue unfinished work.';_save_review_job(db,row,record)
        except Exception:logger.exception('Unable to save interrupted repository review %s',review_id)
    finally:
        db.close()
        with _active_review_jobs_lock:_active_review_jobs.discard(review_id)

@router.post('/repositories/{repo_id}/review')
def review_repository(repo_id:int,body:ReviewRequest,bg:BackgroundTasks,user:User=Depends(current_user),db:Session=Depends(get_db)):
    repo=owned_repo(repo_id,user,db)
    if repo.status!='ready': raise HTTPException(409,'Repository indexing is not complete')
    categories=list(dict.fromkeys(body.categories)) or list(REVIEW_CATEGORIES)
    if body.scope=='file':
        if not body.file_path: raise HTTPException(422,'Select a repository file to review')
        paths=[_clean_file_path(body.file_path)]
        if not _is_reviewable_source_path(paths[0]): raise HTTPException(422,'Select a supported source-code file to review')
    else:
        if body.review_id:
            row=db.scalar(select(GeneratedDocument).where(GeneratedDocument.id==body.review_id,GeneratedDocument.repository_id==repo.id,GeneratedDocument.kind=='review_v2'))
            if not row:raise HTTPException(404,'Review job not found')
            record=json.loads(row.content)
            if record.get('commit_sha')!=repo.commit_sha or record.get('index_generation')!=repo.index_generation:raise HTTPException(409,'Indexed commit changed; start a new review.')
            if set(categories)!=set(record.get('categories',[])):raise HTTPException(409,'Resume this review with its original category selection.')
            with _active_review_jobs_lock:
                if body.review_id in _active_review_jobs:raise HTTPException(409,'This review is already running.')
            if record.get('status') not in ('rate_limited','failed','interrupted'):raise HTTPException(409,'This review does not need to be resumed.')
            record['status']='queued';record['warning']=None;_save_review_job(db,row,record);bg.add_task(_run_repository_review,row.id)
            return _review_job_payload({**record,'review_id':row.id})
        paths=_repository_review_paths(repo)
        if not paths:raise HTTPException(404,'No eligible indexed source files were found for review')
        file_states=[{'path':path,'deterministic_status':'waiting','deterministic_done':False,'ai_status':'pending','findings':[]} for path in paths]
        record={'version':2,'repository_id':repo.id,'commit_sha':repo.commit_sha,'index_generation':repo.index_generation,'branch':repo.branch,'scope':'repository',
            'file_path':None,'categories':categories,'files':file_states,'files_by_path':{item['path']:item for item in file_states},'completed_batches':[],
            'rate_limited_files':[],'status':'queued','warning':None,'created_at':datetime.now(timezone.utc).isoformat()}
        row=GeneratedDocument(repository_id=repo.id,kind='review_v2',content=json.dumps(record));db.add(row);db.flush();record['review_id']=row.id;_save_review_job(db,row,record)
        bg.add_task(_run_repository_review,row.id)
        return _review_job_payload(record)
    findings=[];warnings=[];rate_limited=False
    for path in paths:
        file_findings,warning,limited=_review_file(repo,path,categories);findings.extend(file_findings);rate_limited=rate_limited or limited
        if warning:warnings.append(f"{path}: {warning}")
        if limited:break
    # Aggregate exact duplicates while keeping file and line as part of the identity.
    unique={}
    for finding in findings: unique.setdefault((finding['file_path'],finding['start_line'],finding['title'].lower()),finding)
    findings=sorted(unique.values(),key=lambda f:(REVIEW_SEVERITIES.index(f['severity']),f['file_path'],f['start_line']))
    counts={level:sum(item['severity']==level for item in findings) for level in REVIEW_SEVERITIES}
    review_status='rate_limited' if rate_limited else 'partial' if warnings else 'completed'
    summary=(f"Partial review saved with {len(findings)} finding(s); AI reasoning did not complete." if review_status!='completed' else
             f"Review completed with {len(findings)} finding(s): {counts['Critical']} critical, {counts['High']} high, {counts['Medium']} medium, {counts['Low']} low, and {counts['Suggestion']} suggestions.")
    record={'repository_id':repo.id,'commit_sha':repo.commit_sha,'branch':repo.branch,'scope':body.scope,'file_path':paths[0] if body.scope=='file' else None,
        'categories':categories,'summary':summary,
        'findings':findings,'status':review_status,'warning':'AI reasoning did not complete; deterministic checks were still run. '+ ' '.join(warnings) if warnings else None,
        'created_at':datetime.now(timezone.utc).isoformat()}
    db.add(GeneratedDocument(repository_id=repo.id,kind='review_v1',content=json.dumps(record)));db.commit()
    return record

@router.get('/repositories/{repo_id}/reviews')
def review_history(repo_id:int,user:User=Depends(current_user),db:Session=Depends(get_db)):
    repo=owned_repo(repo_id,user,db)
    rows=db.scalars(select(GeneratedDocument).where(GeneratedDocument.repository_id==repo.id,GeneratedDocument.kind.in_(('review_v1','review_v2'))).order_by(GeneratedDocument.created_at.desc()).limit(10)).all()
    history=[]
    for row in rows:
        try: item=json.loads(row.content)
        except (ValueError,TypeError): continue
        if row.kind=='review_v2':item=_review_job_payload({**item,'review_id':row.id})
        elif item.get('warning') and re.search(r'rate.?limit|temporarily unavailable|AI reasoning|AI analysis',str(item['warning']),re.I):
            item['status']='partial';item['can_resume']=False
            item['summary']='Partial review saved: AI analysis stopped before all files were checked. Start a new repository review to continue.'
        if item.get('scope')=='file' and not _is_reviewable_source_path(str(item.get('file_path') or '')): continue
        item['stale']=item.get('commit_sha')!=repo.commit_sha
        history.append(item)
    return history

@router.get('/repositories/{repo_id}/reviews/{review_id}')
def review_progress(repo_id:int,review_id:int,user:User=Depends(current_user),db:Session=Depends(get_db)):
    repo=owned_repo(repo_id,user,db)
    row=db.scalar(select(GeneratedDocument).where(GeneratedDocument.id==review_id,GeneratedDocument.repository_id==repo.id,GeneratedDocument.kind=='review_v2'))
    if not row:raise HTTPException(404,'Review job not found')
    record=json.loads(row.content)
    with _active_review_jobs_lock:active=review_id in _active_review_jobs
    if record.get('status') in ('running','queued') and not active:
        try:last_seen=datetime.fromisoformat(record.get('heartbeat_at') or record.get('created_at',''))
        except (TypeError,ValueError):last_seen=datetime.min.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc)-last_seen>timedelta(minutes=10):
            record['status']='interrupted';record['warning']='Review worker stopped. Resume to continue unfinished batches.';_save_review_job(db,row,record)
    return _review_job_payload({**record,'review_id':review_id})

@router.post('/auth/register')
def register(body:Register,db:Session=Depends(get_db)):
    if db.scalar(select(User).where(User.email==body.email)): raise HTTPException(409,"Email is already registered")
    user=User(email=body.email,password_hash=hash_password(body.password)); db.add(user); db.commit(); db.refresh(user)
    return {"access_token":create_token(str(user.id)),"token_type":"bearer","user":{"id":user.id,"email":user.email}}

@router.post('/auth/login')
def login(form:OAuth2PasswordRequestForm=Depends(),db:Session=Depends(get_db)):
    user=db.scalar(select(User).where(User.email==form.username))
    if not user or not verify_password(form.password,user.password_hash): raise HTTPException(401,"Incorrect email or password")
    return {"access_token":create_token(str(user.id)),"token_type":"bearer","user":{"id":user.id,"email":user.email}}

@router.get('/auth/me')
def me(user:User=Depends(current_user)): return {"id":user.id,"email":user.email}

@router.get('/settings')
def account_settings(user:User=Depends(current_user)):
    return {"email":user.email,"embedding_model":settings.embedding_model,
            "generation_provider":"Groq","generation_configured":bool(settings.groq_api_key and settings.groq_model),
            "context_token_budget":settings.rag_context_token_budget}

def owned_repo(repo_id:int,user:User,db:Session):
    repo=db.scalar(select(Repository).where(Repository.id==repo_id,Repository.owner_id==user.id))
    if not repo: raise HTTPException(404,"Repository not found")
    return repo

@router.get('/repositories')
def repos(user:User=Depends(current_user),db:Session=Depends(get_db)):
    return [serialize_repo(r) for r in db.scalars(select(Repository).where(Repository.owner_id==user.id).order_by(Repository.created_at.desc())).all()]

def serialize_repo(r): return {"id":r.id,"name":r.name,"owner":r.owner,"url":r.url,"branch":r.branch,"commit_sha":r.commit_sha,"status":r.status,"file_count":r.file_count,"summary":r.summary,"created_at":r.created_at.isoformat(),"updated_at":r.updated_at.isoformat() if r.updated_at else None}

@router.post('/repositories')
def add_repo(body:RepoCreate,bg:BackgroundTasks,user:User=Depends(current_user),db:Session=Depends(get_db)):
    owner,name=parse_github_url(body.url)
    r=Repository(owner_id=user.id,name=name,owner=owner,url=body.url.rstrip('/'),branch=body.branch or '',status='queued')
    db.add(r); db.commit(); db.refresh(r)
    bg.add_task(run_index,r.id)
    return serialize_repo(r)

def run_index(repo_id:int):
    from app.db.session import SessionLocal
    db=SessionLocal()
    previous = None
    try:
        repo=db.get(Repository,repo_id)
        if repo:
            previous = {"status": repo.status, "summary": repo.summary, "owner": repo.owner, "name": repo.name,
                        "branch": repo.branch, "commit_sha": repo.commit_sha, "file_count": repo.file_count,
                        "index_generation": repo.index_generation}
            repo.status='indexing'; db.commit()
            result=index_repository(repo)
            repo.owner=result.owner; repo.name=result.name; repo.branch=result.branch
            repo.commit_sha=result.commit_sha; repo.index_generation=result.generation
            repo.file_count=result.file_count; repo.summary=result.summary; repo.status='ready'
            db.commit()
            try:
                delete_stale_generations(repo.id, result.generation)
            except Exception:
                logger.exception("Active index saved; stale Qdrant generation cleanup failed for repository %s", repo.id)
    except Exception:
        logger.exception("Repository indexing failed for repository %s", repo_id)
        db.rollback(); repo=db.get(Repository,repo_id)
        if repo:
            if previous and previous["status"] == "ready":
                repo.status=previous["status"]; repo.summary=previous["summary"]
                repo.owner=previous["owner"]; repo.name=previous["name"]; repo.branch=previous["branch"]
                repo.commit_sha=previous["commit_sha"]; repo.file_count=previous["file_count"]
                repo.index_generation=previous["index_generation"]
            else:
                repo.status='failed'; repo.summary='Indexing failed. Check backend logs and service configuration.'
            db.commit()
    finally: db.close()

@router.get('/repositories/{repo_id}')
def repo_detail(repo_id:int,user:User=Depends(current_user),db:Session=Depends(get_db)): return serialize_repo(owned_repo(repo_id,user,db))

@router.get('/repositories/{repo_id}/stats')
def repo_stats(repo_id:int,user:User=Depends(current_user),db:Session=Depends(get_db)):
    repo=owned_repo(repo_id,user,db)
    points=repository_points(repo.id,repo.index_generation) if repo.status=='ready' else []
    return {**serialize_repo(repo),"chunk_count":len(points),"last_updated_at":repo.updated_at.isoformat() if repo.updated_at else None}

@router.delete('/repositories/{repo_id}')
def remove_repo(repo_id:int,user:User=Depends(current_user),db:Session=Depends(get_db)):
    repo=owned_repo(repo_id,user,db)
    from app.services.vector_store.qdrant import delete_repository
    delete_repository(repo.id); db.delete(repo); db.commit(); return {"ok":True}

@router.post('/repositories/{repo_id}/ask')
def ask(repo_id:int,body:Ask,user:User=Depends(current_user),db:Session=Depends(get_db)):
    repo=owned_repo(repo_id,user,db)
    if repo.status!='ready': raise HTTPException(409,"Repository indexing is not complete")
    if body.conversation_id:
        conv=db.scalar(select(Conversation).where(Conversation.id==body.conversation_id,Conversation.user_id==user.id,Conversation.repository_id==repo.id))
        if not conv: raise HTTPException(404,"Conversation not found")
    else:
        conv=Conversation(user_id=user.id,repository_id=repo.id,title=body.question[:100]); db.add(conv); db.commit(); db.refresh(conv)
    db.add(ChatMessage(conversation_id=conv.id,role='user',content=body.question)); db.commit()
    vector=embed_texts([body.question])[0]
    found=search(repo.id,vector,body.question,index_generation=repo.index_generation)
    profile=analyze_query(body.question)
    evidence=build_context(found,settings.rag_context_token_budget,max_lines_per_source=12,
                           fair_by_file=any(intent in profile.intents for intent in ("workflow","cross_file","frontend_backend","architecture")),
                           prioritize_auth_workflow="authentication_workflow" in profile.intents,
                           profile=profile)
    sources=evidence['sources']
    indexed_path_context=""
    workflow_missing_notes=[]
    if "authentication_workflow" in profile.intents:
        inventory=authentication_workflow_index_inventory(repository_files(repo.id,repo.index_generation))
        indexed_path_context=("\n\nIndexed authentication-workflow path inventory (paths only; source content must come from citations below):\n"
                              +format_authentication_workflow_inventory(inventory))
        workflow_missing_notes=missing_authentication_workflow_evidence(inventory,sources)
    required_workflow_stages, unavailable_workflow_stages = authentication_workflow_stage_requirements(body.question,sources)
    business_stages, unavailable_business_stages = business_workflow_stage_requirements(body.question,sources)
    required_workflow_stages.update(business_stages)
    unavailable_workflow_stages.extend(unavailable_business_stages)
    workflow_missing_notes.extend(f"The {stage} stage was requested, but no matching source excerpt was available in this answer context." for stage in unavailable_workflow_stages)
    if not sources:
        answer_result=render_grounded_answer({"complete":True,"not_found":[
            "No source evidence was retrieved for this question, so the answer cannot be verified as indexed or absent. Try a more specific symbol, filename, endpoint, or workflow term."
        ]},[])
    else:
        system=("You are RepoInsight, a source-grounded codebase assistant. Return one compact JSON object only with keys "
                "facts, not_found, complete. facts is an array of objects shaped {\"kind\":\"source_code|documentation|inference\", "
                "\"stage\":string, \"claim\":string, \"citations\":[\"S1\"]}; omit stage when no workflow stage is requested. "
                "not_found is an array of strings; complete is true only when requested stages are covered or explicitly unavailable. "
                "Use only supplied evidence. Each factual claim must be one concise claim with exact citation IDs attached. "
                "Use one concise fact per requested stage; combine related evidence in that claim instead of repeating facts across categories. "
                "Use kind=source_code for implementation facts, documentation for facts explicitly stated in docs, and inference only for reasoned conclusions. "
                "Do not label directly stated facts as inferences. Never invent route guards, functions, fields, validation, security behavior, "
                "or calls. For missing evidence, state whether the requested source path is absent from the indexed path inventory or is indexed "
                "but its relevant content was not retrieved. Do not make unsupported claims. Explain workflow steps in execution order, and be "
                "concise enough to finish within the configured output limit.")
        if required_workflow_stages:
            system+=(" For each listed requested stage, return one directly supported source_code fact with that exact stage label and cite "
                     "at least one supplied excerpt from each listed path group. Never say supplied evidence was unavailable. Do not emit "
                     "unrequested stages or repeat the same claim.")
        prompt=(f"Repository: {repo.owner}/{repo.name}\nQuestion: {body.question}\n"
                f"\nEvidence excerpts (each label includes the precise file and line range; type is source_code or documentation):\n"
                f"{evidence['context']}{indexed_path_context}")
        if required_workflow_stages:
            source_by_id={source['citation_id']:source for source in sources}
            prompt+=("\n\nRequired requested stages and source path groups (cite at least one ID from every group for its stage):\n"
                     +"\n".join(f"- {stage}: " + "; ".join(
                         f"{source_by_id[group[0]]['file_path']}: " + ", ".join(f"[{cid}]" for cid in group)
                         for group in groups if group and group[0] in source_by_id)
                                 for stage,groups in required_workflow_stages.items()))
        try:
            answer_result=generate_grounded_answer(
                system,prompt,sources,
                lambda sys,user_prompt: complete_with_metadata(sys,user_prompt,max_retries=0,json_mode=True,include_usage=True),
                required_workflow_stages=required_workflow_stages,
            )
        except IncompleteGroundedAnswer as exc:
            logger.error("assistant_answer_rejected repository_id=%s reasons=%s",repo.id,",".join(exc.reasons))
            raise HTTPException(502,"A complete, citation-valid answer could not be generated within the configured output limit. Try a more focused question.") from exc
    answer_result=append_not_found_notes(answer_result,workflow_missing_notes)
    cited_sources=[source for source in sources if source['citation_id'] in answer_result.cited_source_ids]
    db.add(ChatMessage(conversation_id=conv.id,role='assistant',content=answer_result.markdown,sources=cited_sources)); db.commit()
    return {"conversation_id":conv.id,"answer":answer_result.markdown,"sources":cited_sources}

@router.post('/repositories/{repo_id}/documents')
def generate_doc(repo_id:int,kind:Literal['overview','readme','api','architecture','module']='overview',user:User=Depends(current_user),db:Session=Depends(get_db)):
    repo=owned_repo(repo_id,user,db)
    if repo.status!='ready': raise HTTPException(409,"Repository indexing is not complete")
    retrieval_query,retrieval_limit=documentation_retrieval_query(kind,f"{repo.owner}/{repo.name}")
    doc_profile=analyze_query(retrieval_query)
    vector=embed_texts([retrieval_query])[0]
    points=search(repo.id,vector,retrieval_query,limit=retrieval_limit,index_generation=repo.index_generation)
    evidence=build_context(points,settings.rag_context_token_budget,max_lines_per_source=12,
                           fair_by_file=kind in {'overview','api','architecture','module'},profile=doc_profile)
    if not evidence['sources']:
        raise HTTPException(404,"No indexed source evidence is available for this document")
    if kind=='architecture':
        instruction="Return only a valid Mermaid flowchart (no markdown fences), using simple node IDs and labels. Include only modules and relationships directly supported by the excerpts. Do not invent components or edges."
    elif kind=='overview':
        instruction="Write a concise project overview covering purpose, verified features, structure, entry points, and technologies only when supported by source or manifests. Separate documented claims from implementation facts and cite them."
    elif kind=='readme':
        instruction="Write README-style documentation for installation, configuration, environment variables, commands, scripts, usage, and features. Include each instruction only when the supplied evidence establishes it; mark unavailable details instead of inventing commands or options."
    elif kind=='api':
        instruction="Document only implemented API routes. For each route include its exact method and path, applicable middleware, request validation, response shape, and relevant failure statuses when present in evidence. Cite each implementation claim."
    elif kind=='module':
        instruction="Explain the indexed modules, their responsibilities, important functions or classes, imports, and directly supported caller/callee relationships. Keep unrelated modules out and cite each implementation claim."
    else:
        instruction=f"Generate {kind} technical documentation based only on the cited excerpts. Cite factual implementation claims with [S#]. Mark unsupported details as unknown; do not invent setup commands, endpoints, APIs or dependencies."
    content=complete("You are a repository documentation writer. Ground every codebase claim in provided evidence. "+instruction,
                     f"Repository {repo.owner}/{repo.name}; requested document: {kind}\n\n{evidence['context']}")
    doc=GeneratedDocument(repository_id=repo.id,kind=kind,content=content); db.add(doc); db.commit(); db.refresh(doc)
    return {"id":doc.id,"kind":kind,"content":content,"sources":evidence['sources']}

@router.get('/repositories/{repo_id}/architecture-graph')
def architecture_graph(repo_id:int,user:User=Depends(current_user),db:Session=Depends(get_db)):
    """Return a read-only architecture graph grounded in the active indexed generation."""
    try:
        repo=owned_repo(repo_id,user,db)
        if repo.status!='ready': raise HTTPException(409,"Repository indexing is not complete")
        points=repository_points(repo.id,repo.index_generation)
        if not points:
            logger.warning("Architecture graph unavailable: no indexed source points for repository_id=%s",repo_id)
            raise HTTPException(422,"No indexed source data is available for architecture generation.")
        return cached_architecture_graph(f"{repo.owner}/{repo.name}",repo.id,repo.index_generation,
                                         points,commit_sha=repo.commit_sha,
                                         interpreter=interpret_architecture_graph)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Architecture graph request failed for repository_id=%s",repo_id)
        if isinstance(exc,SQLAlchemyError):
            status_code=503;detail="Repository database is unavailable. Check PostgreSQL and backend logs."
        elif isinstance(exc,(ResponseHandlingException,UnexpectedResponse)):
            status_code=503;detail="Indexed source storage is unavailable. Check Qdrant and backend logs."
        elif isinstance(exc,GroqRateLimitError):
            status_code=503;detail="AI architecture interpretation is rate limited. Retry later; deterministic output may be available after recovery."
        elif isinstance(exc,ValueError):
            status_code=422;detail="Indexed architecture evidence could not be validated. Check repository indexing and backend logs."
        else:
            status_code=500;detail="Architecture generation failed. Check backend logs for details."
        raise HTTPException(status_code,detail) from exc

@router.get('/repositories/{repo_id}/files')
def files(repo_id:int,user:User=Depends(current_user),db:Session=Depends(get_db)):
    repo=owned_repo(repo_id,user,db)
    return repository_files(repo.id,repo.index_generation)

@router.get('/repositories/{repo_id}/source')
def source(repo_id:int,path:str,user:User=Depends(current_user),db:Session=Depends(get_db)):
    repo=owned_repo(repo_id,user,db)
    pts=file_points(repo.id,path,repo.index_generation)
    if not pts: raise HTTPException(404,"Source file not found in index")
    pts.sort(key=lambda p:(int(p.payload.get('start_line',0)),int(p.payload.get('end_line',0)),str(p.id)))
    return {"path":path,"chunks":[{"start_line":p.payload['start_line'],"end_line":p.payload['end_line'],"content":p.payload['content'],"symbol_name":p.payload.get('symbol_name'),"symbol_type":p.payload.get('symbol_type'),"language":p.payload.get('language')} for p in pts]}

@router.post('/repositories/{repo_id}/search')
def semantic_search(repo_id:int,body:SearchRequest,user:User=Depends(current_user),db:Session=Depends(get_db)):
    repo=owned_repo(repo_id,user,db)
    if repo.status!='ready': raise HTTPException(409,"Repository indexing is not complete")
    vector=embed_texts([body.query])[0]
    hits=search(repo.id,vector,body.query,index_generation=repo.index_generation)
    return {"query":body.query,"results":[{"file_path":h.payload.get('file_path'),"start_line":h.payload.get('start_line'),
            "end_line":h.payload.get('end_line'),"symbol_name":h.payload.get('symbol_name'),"symbol_type":h.payload.get('symbol_type'),
            "language":h.payload.get('language'),"excerpt":h.payload.get('content',''),"score":round(h.score,6),"rank":h.rank,
            "chunk_id":h.payload.get('chunk_id')} for h in hits]}

def _file_context_groups(hits:list[SearchHit],token_budget:int):
    groups=[]; current=[]; cost=0
    for hit in split_hits_for_budget(hits,token_budget):
        estimate=estimate_tokens(hit.payload.get('content',''))+40
        if current and cost+estimate>token_budget:
            groups.append(current); current=[]; cost=0
        current.append(hit); cost+=estimate
    if current: groups.append(current)
    return groups

def _clean_file_path(path:str):
    normalized=path.replace('\\','/').strip()
    if not normalized or normalized.startswith('/') or any(part in ('..','') for part in normalized.split('/')):
        raise HTTPException(422,"Provide a repository-relative indexed file path")
    return normalized

@router.post('/repositories/{repo_id}/explain-file')
def explain_file(repo_id:int,body:FileExplanationRequest,user:User=Depends(current_user),db:Session=Depends(get_db)):
    repo=owned_repo(repo_id,user,db)
    if repo.status!='ready': raise HTTPException(409,"Repository indexing is not complete")
    file_path=_clean_file_path(body.file_path)
    cache_hash=hashlib.sha256(file_path.encode()).hexdigest()[:32]
    # Version the cache so explanations created under the old presentation prompt are regenerated.
    cache_kind=f"fe3_{'d' if body.mode=='detailed' else 'o'}_{cache_hash}"
    cached=db.scalar(select(GeneratedDocument).where(GeneratedDocument.repository_id==repo.id,GeneratedDocument.kind==cache_kind).order_by(GeneratedDocument.created_at.desc()))
    if cached:
        try:
            record=json.loads(cached.content)
            if record.get('commit_sha')==repo.commit_sha and record.get('index_generation')==repo.index_generation:
                return {"file_path":file_path,"mode":body.mode,"explanation":record['explanation'],"sources":record['sources'],"cached":True}
        except (ValueError,KeyError,TypeError):
            logger.warning("Ignoring malformed cached file explanation for repository %s",repo.id)
    points=file_points(repo.id,file_path,repo.index_generation)
    if not points:
        raise HTTPException(404,"File is not present in the selected repository's active index")
    points.sort(key=lambda p:(int(p.payload.get('start_line',0)),int(p.payload.get('end_line',0)),str(p.id)))
    seen=set(); file_hits=[]
    for p in points:
        digest=p.payload.get('content_hash')
        if digest and digest in seen: continue
        if digest: seen.add(digest)
        file_hits.append(SearchHit(p,1.0/(60+len(file_hits)+1),len(file_hits)+1))
    section_budget=max(1200,settings.rag_explanation_section_token_budget)
    groups=_file_context_groups(file_hits,section_budget)
    source_offset=0; file_sources=[]; notes=[]
    for section_no,group in enumerate(groups,1):
        evidence=build_context(group,section_budget,source_offset)
        source_offset+=len(evidence['sources']); file_sources.extend(evidence['sources'])
        if len(groups)==1:
            notes.append(evidence['context'])
        else:
            note=complete("Analyze this section of the selected source file. Return concise, non-repetitive factual notes about declarations, imports, data flow, validation, errors, and calls. Preserve supplied [S#] citations for each supported fact. Do not infer beyond this section. Use short headings or bullets, not tables or decorative separators.",
                          f"File {file_path}, section {section_no}/{len(groups)}:\n{evidence['context']}")
            notes.append(note)
    symbol_names=list(dict.fromkeys(str(p.payload.get('symbol_name','')) for p in points if p.payload.get('symbol_name')))
    imports=[]
    for p in points:
        for line in str(p.payload.get('content','')).splitlines():
            if re.match(r"\s*(?:import\s|from\s+\S+\s+import|const\s+.*?require\(|import\s*\{)",line):
                imports.append(line.strip())
    related_query=(f"files that import, call, define, configure, or interact with {file_path} "
                   + " ".join(symbol_names[:20]) + " " + " ".join(imports[:20]))[:1500]
    related_hits=[]
    if related_query.strip():
        related_vector=embed_texts([related_query])[0]
        related_hits=[h for h in search(repo.id,related_vector,related_query,limit=10,index_generation=repo.index_generation)
                      if h.payload.get('file_path')!=file_path]
    related_evidence=build_context(related_hits,max(1000,min(2400,settings.rag_context_token_budget//3)),source_offset)
    all_sources=file_sources+related_evidence['sources']
    file_notes="\n\n".join(notes)
    if len(notes)>1:
        # Keep very large files manageable while preserving the source labels through recursive note reduction.
        while estimate_tokens(file_notes)>max(3000,settings.rag_context_token_budget-3000) and len(notes)>1:
            compact=[]
            for i in range(0,len(notes),4):
                batch=notes[i:i+4]
                compact.append(complete("Consolidate these notes about the selected file. Retain source labels verbatim beside supported facts; remove repetition and unsupported conclusions. Use concise paragraphs or bullets, not tables or decorative separators.","\n\n".join(batch)))
            notes=compact; file_notes="\n\n".join(notes)
    system=("Explain the selected file itself, using its indexed source as the primary evidence and related repository excerpts only to explain real cross-file connections. "
            "Prioritize implementation source over README or descriptive documentation. Explain important functions, components, parameters, return values, and execution/data flow accurately when evidence supports them. "
            "Use Markdown with concise, clear headings and readable paragraphs or bullets. Use these headings when relevant: File Overview; Imports and Dependencies; Main Functions and Components; Execution Flow; "
            "API and Database Interaction; Error Handling and Validation; Connections to Other Files; Simple Explanation. Omit headings that have no evidence; do not include empty sections. "
            "Cite each factual code claim with its exact supplied ASCII [S#] label; never use decorated or full-width citation brackets. Keep citations adjacent to the claim they support. Mark reasonable interpretations as Inference and distinguish them from confirmed behavior. "
            "Keep the answer concise (at most about 1,200 words). Use 2-4 sentences per relevant section and cover the most important functions/components without enumerating every state variable or repeating details. "
            "Do not repeat the same fact. Do not use Markdown tables, pipe-delimited content, horizontal rules, decorative separators, terminal-style dumps, or code fences unless a tiny code example is essential. "
            "Never invent symbols, dependencies, parameters, return values, routes, requests, database operations, validation, or behavior. State when requested details cannot be determined from the evidence.")
    final_prompt=f"Repository: {repo.owner}/{repo.name}\nSelected file (explain this exact file): {file_path}\nExplanation mode: {body.mode}\n\nSelected file evidence and section notes (primary source):\n{file_notes}"
    if related_evidence['context']:
        final_prompt+=f"\n\nRelated repository evidence (use only for cross-file connections):\n{related_evidence['context']}"
    explanation=complete(system,final_prompt)
    citations=extract_citation_ids(explanation)
    sources=[s for s in all_sources if s['citation_id'] in citations]
    if not sources:
        sources=all_sources[:min(64,len(all_sources))]
    record={"commit_sha":repo.commit_sha,"index_generation":repo.index_generation,"explanation":explanation,"sources":sources}
    db.add(GeneratedDocument(repository_id=repo.id,kind=cache_kind,content=json.dumps(record))); db.commit()
    return {"file_path":file_path,"mode":body.mode,"explanation":explanation,"sources":sources,"cached":False}


def _summary_prompt(scope:str, target:str, final:bool=False):
    common=("Write a concise, structured Markdown summary grounded only in the supplied indexed implementation source. "
            "Use meaningful headings, paragraphs, and short bullets; keep the final answer under about 1,000 words. Avoid tables, pipe separators, horizontal rules, decorative symbols, and repetition. "
            "Preserve exact [S#] citations beside factual claims; mark inferences explicitly and never invent behavior, dependencies, integrations, or relationships.")
    if scope=='file' and not final:
        return common+" Summarize this one source file in 100-160 words: purpose, its most important functions/components, and the main flow. Retain citations. Do not inventory every state variable."
    if scope=='file':
        return common+(" Summarize only the selected file in at most 450 words. Use these headings only when relevant: Purpose; Key Dependencies; Important Functions; Execution Flow; Validation and Errors; Simple Explanation. "
                       "Keep each section brief. Do not add component-state tables, source-code blocks, or a second execution summary. Mention inputs and outputs only when the source makes them clear.")
    if final and scope=='repository':
        return common+(" Combine the intermediate file and module summaries into a repository overview of at most 800 words. Use concise headings when evidence exists for: Project Purpose; Technologies; Frontend; Backend; "
                       "Data and Models; Main Features and Flow; External Integrations; Important Directories. Omit headings without evidence. No tables, code blocks, or repeated conclusions. "
                       "Do not claim the repository is complete if evidence coverage is partial; identify unknowns.")
    if final and scope in ('folder','module'):
        return common+f" Summarize {scope} {target} in at most 600 words: responsibility, the most important files, how their components work together, and supported connections to other modules. Do not use tables or code blocks."
    return common+f" Summarize the implementation of {target} as a concise intermediate note, retaining citations for every code claim."


@router.post('/repositories/{repo_id}/summarize')
def summarize_repository(repo_id:int,body:SummaryRequest,user:User=Depends(current_user),db:Session=Depends(get_db)):
    repo=owned_repo(repo_id,user,db)
    if repo.status!='ready': raise HTTPException(409,"Repository indexing is not complete")
    target=''
    if body.scope in ('file','folder'):
        if not body.path: raise HTTPException(422,"A repository-relative file or folder path is required")
        target=_clean_file_path(body.path)
    elif body.scope=='module':
        target=(body.topic or '').strip()
        if not target: raise HTTPException(422,"Describe the module or workflow to summarize")
    cache_key=hashlib.sha256(f"{body.scope}\0{target.lower()}".encode()).hexdigest()[:32]
    cache_kind=f"sum4_{body.scope[0]}_{cache_key}"
    cached=db.scalar(select(GeneratedDocument).where(GeneratedDocument.repository_id==repo.id,GeneratedDocument.kind==cache_kind).order_by(GeneratedDocument.created_at.desc()))
    if cached:
        try:
            record=json.loads(cached.content)
            if record.get('commit_sha')==repo.commit_sha and record.get('index_generation')==repo.index_generation:
                return {"scope":body.scope,"target":target,"summary":record['summary'],"sources":record['sources'],"cached":True}
        except (ValueError,KeyError,TypeError):
            logger.warning("Ignoring malformed cached summary for repository %s",repo.id)

    # Build a bounded set of representative files rather than sending the repository corpus to Groq.
    point_groups:dict[str,list]={}
    if body.scope=='file':
        points=file_points(repo.id,target,repo.index_generation)
        if points: point_groups[target]=[SearchHit(p,1.0/(60+i+1),i+1) for i,p in enumerate(sorted(points,key=lambda p:(int(p.payload.get('start_line',0)),str(p.id))))]
    elif body.scope=='folder':
        prefix=target.rstrip('/')+'/'
        points=[p for p in repository_points(repo.id,repo.index_generation) if str(p.payload.get('file_path','')).startswith(prefix)]
        for p in points:
            point_groups.setdefault(str(p.payload['file_path']),[]).append(SearchHit(p,0.5,int(p.payload.get('start_line',0)) or 1))
        point_groups=dict(list(sorted(point_groups.items()))[:8])
    else:
        query_seeds=([target,f"implementation, files, entry points, and integrations for {target}"] if body.scope=='module' else [
            "project purpose, root entry points, technologies, and initialization",
            "frontend architecture pages components routing and state",
            "backend architecture API routes services authentication and business logic",
            "database models schemas persistence and queries",
            "external API integrations payments email AI and storage",
        ])
        ranked_paths=[]; seen_paths=set()
        for seed in query_seeds:
            vector=embed_texts([seed])[0]
            for hit in search(repo.id,vector,seed,limit=8,index_generation=repo.index_generation):
                path=str(hit.payload.get('file_path',''))
                if path and path not in seen_paths:
                    seen_paths.add(path);ranked_paths.append(path)
        max_files=8 if body.scope=='repository' else 8
        for path in ranked_paths[:max_files]:
            points=file_points(repo.id,path,repo.index_generation)
            if points:
                point_groups[path]=[SearchHit(p,1.0/(60+i+1),i+1) for i,p in enumerate(sorted(points,key=lambda p:(int(p.payload.get('start_line',0)),str(p.id))))]
    if not point_groups:
        raise HTTPException(404,"No indexed source was found for this summary scope")

    if body.scope=='file':
        hits=point_groups[target]
        evidence=build_context(hits,settings.rag_context_token_budget)
        summary=complete(_summary_prompt('file',target,True),f"Repository: {repo.owner}/{repo.name}\nSelected file: {target}\nIndexed source evidence:\n{evidence['context']}")
        all_sources=evidence['sources']
    else:
        file_notes=[];all_sources=[];offset=0
        per_file_budget=max(1000,min(2600,settings.rag_context_token_budget//4))
        for path,hits in point_groups.items():
            evidence=build_context(hits,per_file_budget,offset)
            if not evidence['sources']: continue
            all_sources.extend(evidence['sources']);offset+=len(evidence['sources'])
            file_notes.append((path,complete(_summary_prompt('module',path),f"Selected source file {path}:\n{evidence['context']}")))
        if not file_notes:
            raise HTTPException(404,"No indexed source was available for this summary")
        grouped_notes:dict[str,list[str]]={}
        for path,note in file_notes:
            group=path.split('/',1)[0] if body.scope=='repository' and '/' in path else (target if body.scope=='folder' else target)
            grouped_notes.setdefault(group,[]).append(f"File: {path}\n{note}")
        module_notes=[]
        for group,notes in grouped_notes.items():
            module_notes.append(complete(_summary_prompt('folder' if body.scope=='folder' else 'module',group),
                f"Scope: {body.scope} {group}\nIntermediate source-file summaries:\n"+'\n\n'.join(notes)))
        if body.scope=='repository':
            final_context='Intermediate module summaries:\n'+'\n\n'.join(module_notes)
            final_system=_summary_prompt('repository',f"{repo.owner}/{repo.name}",True)
        else:
            final_context='Intermediate source summaries:\n'+'\n\n'.join(module_notes)
            final_system=_summary_prompt(body.scope,target,True)
        summary=complete(final_system,f"Repository: {repo.owner}/{repo.name}\nRequested scope: {body.scope} {target}\n{final_context}")
    cited=extract_citation_ids(summary)
    sources=[item for item in all_sources if item['citation_id'] in cited]
    if not sources: sources=all_sources[:min(48,len(all_sources))]
    record={"commit_sha":repo.commit_sha,"index_generation":repo.index_generation,"summary":summary,"sources":sources}
    db.add(GeneratedDocument(repository_id=repo.id,kind=cache_kind,content=json.dumps(record)));db.commit()
    return {"scope":body.scope,"target":target,"summary":summary,"sources":sources,"cached":False}
