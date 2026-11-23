"""Read-only retrieval benchmark for an already indexed Hastakala repository.

Run from backend/: python scripts/evaluate_hastakala.py [--repo-id ID] [--include-generation]
The default run never calls Groq and never changes PostgreSQL or Qdrant.
"""
from __future__ import annotations

import argparse
import json
import re
from time import perf_counter

from sqlalchemy import select
from app.db.session import SessionLocal
from app.models.models import Repository
from app.services.embeddings.service import embed_texts
from app.services.retrieval.query_analysis import analyze_query, documentation_retrieval_query
from app.services.retrieval.context_builder import build_context
from app.services.retrieval.pipeline import SearchHit, merge_rankings, lexical_rank, retrieve_results
from app.services.vector_store.qdrant import COLLECTION, client, repository_files, search
from app.core.config import settings
from qdrant_client import models

BENCHMARK = [
    {"name":"previous_authentication_query", "question":"How does JWT authentication middleware validate a token and identify the authenticated user? Explain how the token is generated.",
     "expected":["craft-app-backend/middleware/auth.js","craft-app-backend/utils/jwt.js"],
     "anchors":{"craft-app-backend/middleware/auth.js":["jwt","verify","user"],"craft-app-backend/utils/jwt.js":["jwt","sign","token"]}},
    {"name":"previous_order_creation_query", "question":"Explain how a user places an order from the frontend and how the order is created and saved in the backend.",
     "expected":["craft-app-frontend/src/pages/Checkout.jsx","craft-app-backend/routes/orders.js","craft-app-backend/models/Order.js"],
     "anchors":{"craft-app-frontend/src/pages/Checkout.jsx":["order","checkout","api"],"craft-app-backend/routes/orders.js":["order","create","save"]}},
    {"name":"previous_payment_verification_query", "question":"Explain the complete Razorpay payment verification flow, including signature validation and order updates.",
     "expected":["craft-app-frontend/src/pages/Checkout.jsx","craft-app-backend/routes/payments.js"],
     "anchors":{"craft-app-frontend/src/pages/Checkout.jsx":["payment","verify","signature"],"craft-app-backend/routes/payments.js":["signature","verify","order"]}},
    {"name":"previous_complete_workflow_query", "question":"Explain the complete order creation and payment verification workflow, including frontend components, backend routes, database model and authentication.",
     "expected":["craft-app-frontend/src/pages/Checkout.jsx","craft-app-backend/routes/orders.js","craft-app-backend/routes/payments.js","craft-app-backend/models/Order.js","craft-app-backend/middleware/auth.js","craft-app-backend/utils/jwt.js"],
     "anchors":{"craft-app-frontend/src/pages/Checkout.jsx":["order","payment","api"],"craft-app-backend/routes/orders.js":["order","create"],"craft-app-backend/routes/payments.js":["signature","verify"]}},
    {"name":"complete_authentication", "question":"Explain the complete user registration, email verification, login and protected frontend authentication workflow.",
     "expected":["craft-app-backend/routes/auth.js","craft-app-backend/middleware/auth.js","craft-app-backend/utils/jwt.js","craft-app-frontend/src/context/AuthContext.jsx","craft-app-frontend/src/components/Auth/Login.jsx","craft-app-frontend/src/components/Auth/Register.jsx","craft-app-frontend/src/components/Auth/PrivateRoute.jsx","craft-app-frontend/src/components/Auth/AdminRoute.jsx","craft-app-frontend/src/components/Auth/CreatorRoute.jsx"],
     "anchors":{"craft-app-backend/routes/auth.js":["register","verify","login","token"],"craft-app-frontend/src/components/Auth/Register.jsx":["register","email","password"],"craft-app-frontend/src/pages/VerifyEmail.jsx":["verify","token"]}},
    {"name":"complete_order_payment", "question":"Explain the complete order creation and payment verification workflow, including the frontend, backend routes, database model and authentication middleware.",
     "expected":["craft-app-frontend/src/pages/Checkout.jsx","craft-app-backend/routes/orders.js","craft-app-backend/routes/payments.js","craft-app-backend/models/Order.js","craft-app-backend/middleware/auth.js"],
     "anchors":{"craft-app-frontend/src/pages/Checkout.jsx":["order","payment","api"],"craft-app-backend/routes/orders.js":["order","save","create"],"craft-app-backend/routes/payments.js":["verify","signature","order"]}},
    {"name":"specific_symbol", "question":"Find the handlePlaceOrder function and explain what it does, including the API calls it makes.",
     "expected":["craft-app-frontend/src/pages/Checkout.jsx","craft-app-backend/routes/orders.js"],
     "anchors":{"craft-app-frontend/src/pages/Checkout.jsx":["handlePlaceOrder"],"craft-app-backend/routes/orders.js":["order","post"]}},
    {"name":"product_management", "question":"How are products created and managed by administrators from the frontend through the API and product model?",
     "expected":["craft-app-frontend/src/pages/AdminDashboard.jsx","craft-app-backend/routes/admin.js","craft-app-backend/routes/products.js","craft-app-backend/models/Product.js"],
     "anchors":{"craft-app-frontend/src/pages/AdminDashboard.jsx":["product","create","admin"],"craft-app-backend/routes/products.js":["product","router","create"],"craft-app-backend/models/Product.js":["schema","product","category"]}},
    {"name":"database_models", "question":"Which database models represent users, products, orders, reviews and payments, and how do their fields reference one another?",
     "expected":["craft-app-backend/models/User.js","craft-app-backend/models/Product.js","craft-app-backend/models/Order.js","craft-app-backend/models/Review.js","craft-app-backend/models/Payment.js"],
     "anchors":{"craft-app-backend/models/User.js":["schema","email","role"],"craft-app-backend/models/Product.js":["schema","category"],"craft-app-backend/models/Order.js":["schema","user","product"]}},
    {"name":"specific_file", "question":"Explain craft-app-backend/routes/auth.js, including the route handlers and their line ranges.",
     "expected":["craft-app-backend/routes/auth.js"],"anchors":{"craft-app-backend/routes/auth.js":["router","register","login","verify"]}},
    {"name":"frontend_backend_api", "question":"How does the Checkout frontend communicate with the backend payment and order routes, and which middleware and models are involved?",
     "expected":["craft-app-frontend/src/pages/Checkout.jsx","craft-app-backend/routes/payments.js","craft-app-backend/routes/orders.js","craft-app-backend/middleware/auth.js","craft-app-backend/models/Order.js"],
     "anchors":{"craft-app-frontend/src/pages/Checkout.jsx":["api","payment","order"],"craft-app-backend/routes/payments.js":["router","signature","payment"],"craft-app-backend/routes/orders.js":["router","order"]}},
    {"name":"unsupported_feature", "question":"Does this repository implement an SAP Fiori connector for quantum entanglement telemetry?",
     "expected":[],"anchors":{}},
]

def baseline_search(repository_id: int, generation: str | None, vector: list[float], query: str):
    """Reproduce the previous top-24 dense + first-1500 simple-substring RRF for comparison."""
    must=[models.FieldCondition(key="repository_id",match=models.MatchValue(value=repository_id))]
    if generation: must.append(models.FieldCondition(key="index_generation",match=models.MatchValue(value=generation)))
    flt=models.Filter(must=must)
    dense=client.query_points(COLLECTION,query=vector,query_filter=flt,limit=24,with_payload=True).points
    dense=[p for p in dense if p.payload.get('repository_id')==repository_id and (not generation or p.payload.get('index_generation')==generation)]
    pool=client.scroll(COLLECTION,scroll_filter=flt,limit=1500,with_payload=True,with_vectors=False)[0]
    pool=[p for p in pool if p.payload.get('repository_id')==repository_id and (not generation or p.payload.get('index_generation')==generation)]
    terms={t.lower() for t in query.split() if len(t)>2}
    lexical=sorted(pool,key=lambda p:sum(1 for term in terms if term in p.payload.get('content','').lower()),reverse=True)[:24]
    ranks={}; point_by_id={p.id:p for p in dense+lexical}
    for rows in (dense,lexical):
        for rank,p in enumerate(rows,1): ranks[p.id]=ranks.get(p.id,0)+1/(60+rank)
    return [point_by_id[i] for i in sorted(ranks,key=lambda k:(-ranks[k],str(k)))[:8]]

def path_matches(path: str, expected: str) -> bool:
    p=path.lower().replace('\\','/')
    e=expected.lower().replace('\\','/')
    return p == e or p.endswith('/'+e)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--repo-id',type=int)
    parser.add_argument('--include-generation',action='store_true',help='Call Groq for each query and assess citation references')
    args=parser.parse_args()
    with SessionLocal() as db:
        if args.repo_id:
            repo=db.get(Repository,args.repo_id)
        else:
            found=db.scalars(select(Repository).where(Repository.name.ilike('%hastakala%'))).all()
            if len(found)!=1:
                raise SystemExit(f"Expected exactly one Hastakala repository record; found {len(found)}. Pass --repo-id.")
            repo=found[0]
        if repo is None: raise SystemExit("Repository record not found")
        repo_id, generation, name = repo.id, repo.index_generation, f"{repo.owner}/{repo.name}"
    indexed_paths = repository_files(repo_id, generation)
    output={"repository":name,"repository_id":repo_id,"generation":generation,
            "baseline":"recorded pre-upgrade method: dense top-24 + first lexical page (up to 1500) + RRF top-8",
            "generation_called":bool(args.include_generation),
            "expectation_notes":[{"path":"craft-app-frontend/src/services/api.js",
                                  "status":"not_present_in_source_checkout",
                                  "details":"Checkout and AdminDashboard make direct Axios calls; no shared frontend services/api.js file exists."}],
            "results":[]}
    expected_total=baseline_total=current_expected=0
    for test in BENCHMARK:
        vector=embed_texts([test['question']])[0]
        baseline_started=perf_counter()
        old=baseline_search(repo_id,generation,vector,test['question'])
        baseline_ms=(perf_counter()-baseline_started)*1000
        retrieval_started=perf_counter()
        new=search(repo_id,vector,test['question'],index_generation=generation)
        retrieval_ms=(perf_counter()-retrieval_started)*1000
        old_paths=[p.payload.get('file_path','') for p in old]
        new_paths=[h.payload.get('file_path','') for h in new]
        expected=test['expected']
        hit_expected=[e for e in expected if any(path_matches(p,e) for p in new_paths)]
        baseline_expected=[e for e in expected if any(path_matches(p,e) for p in old_paths)]
        expected_total+=len(expected); current_expected+=len(hit_expected); baseline_total+=len(expected); baseline_expected_count=len(baseline_expected)
        # Keep this accumulator separate from total expected paths for the coverage comparison.
        if 'baseline_expected_count_total' not in output: output['baseline_expected_count_total']=0
        output['baseline_expected_count_total']+=baseline_expected_count
        new_duplicates=len(new)-len({(h.payload.get('file_path'),h.payload.get('content_hash')) for h in new})
        profile=analyze_query(test['question'])
        context=build_context(new,settings.rag_context_token_budget,max_lines_per_source=12,
                              fair_by_file=any(intent in profile.intents for intent in ('workflow','cross_file','frontend_backend','architecture')),
                              prioritize_auth_workflow='authentication_workflow' in profile.intents,profile=profile)
        anchor_groups=test.get('anchors',{})
        relevant_ranges={}
        for block in re.split(r"(?=\[S\d+\] )",context['context']):
            header=re.match(r"\[(S\d+)\] (.*?):(\d+)-(\d+)",block)
            if not header:continue
            citation_id,path,start,end=header.group(1),header.group(2),int(header.group(3)),int(header.group(4))
            body=block.split('```',2)[1] if block.count('```')>=2 else ''
            for expected_path in expected:
                if not path_matches(path,expected_path):continue
                anchors=next((terms for candidate,terms in anchor_groups.items() if path_matches(path,candidate)),[])
                if not anchors or any(term.lower() in body.lower() for term in anchors):
                    relevant_ranges.setdefault(expected_path,[]).append({"file_path":path,"start_line":start,"end_line":end,"citation_id":citation_id})
        record={"name":test['name'],"question":test['question'],"expected_paths":expected,
                "index_inventory":{"expected_indexed":[e for e in expected if any(path_matches(p,e) for p in indexed_paths)],
                                   "expected_not_indexed":[e for e in expected if not any(path_matches(p,e) for p in indexed_paths)]},
                "new":{"expected_retrieved":hit_expected,"expected_missing":[e for e in expected if e not in hit_expected],
                       "relevant_line_ranges":relevant_ranges,"context_tokens":context['estimated_tokens'],
                       "duplicate_count":new_duplicates,"documentation_result_count":sum(p.lower().endswith(('.md','.rst','.txt')) for p in new_paths),
                       "results":[{"file_path":h.payload.get('file_path'),"symbol_name":h.payload.get('symbol_name'),
                         "start_line":h.payload.get('start_line'),"end_line":h.payload.get('end_line'),"score":round(h.score,6)} for h in new]},
                "baseline":{"expected_retrieved":baseline_expected,"expected_missing":[e for e in expected if e not in baseline_expected],
                            "files":old_paths},
                "latency_ms":{"baseline_retrieval":round(baseline_ms,2),"current_retrieval":round(retrieval_ms,2)}}
        if not expected:
            record['no_answer_probe']={"retrieval_returned_results":bool(new),"retrieved_paths":list(dict.fromkeys(new_paths[:8])),
                                      "needs_grounded_abstention":True}
        if args.include_generation:
            evidence=build_context(new,settings.rag_context_token_budget)
            answer=""
            if evidence['sources']:
                from app.services.ai.groq_service import complete
                answer=complete("Answer only from evidence. Cite factual claims with exact [S#] labels. Say when details cannot be confirmed.",
                                f"Repository {name}\nQuestion: {test['question']}\n\n{evidence['context']}")
            citations={label for label in __import__('re').findall(r'\[(S\d+)\]',answer)}
            citation_sources={s['citation_id'] for s in evidence['sources'] if s['citation_id'] in citations}
            cited_paths=[s['file_path'] for s in evidence['sources'] if s['citation_id'] in citation_sources]
            record['generation']={"answer":answer,"citation_ids":sorted(citations),"cited_paths":cited_paths,
                                  "has_source_citations":bool(citations and cited_paths)}
        output['results'].append(record)
    # Evaluate the five documentation retrieval profiles independently of Groq.
    # This records which source kinds and implementation roles actually reach
    # their generation contexts without creating or changing indexed data.
    output['documentation_profiles'] = []
    for kind in ('overview', 'readme', 'api', 'architecture', 'module'):
        query, profile_limit = documentation_retrieval_query(kind, name)
        vector = embed_texts([query])[0]
        hits = search(repo_id, vector, query, limit=profile_limit, index_generation=generation)
        profile = analyze_query(query)
        context = build_context(hits, settings.rag_context_token_budget, max_lines_per_source=12,
                                fair_by_file=True, profile=profile)
        context_blocks = re.split(r"(?=\[S\d+\] )", context['context'])
        role_coverage = {role: [] for role in ('entrypoints','configuration','database_connections','routes','services')}
        for block in context_blocks:
            header = re.match(r"\[(S\d+)\] (.*?):(\d+)-(\d+)", block)
            if not header:
                continue
            _, path, _, _ = header.groups()
            basename = path.lower().replace('\\','/').rsplit('/',1)[-1]
            body = block.split('```',2)[1] if block.count('```') >= 2 else ''
            if basename in {'index.js','index.ts','index.jsx','index.tsx','main.py','main.js','main.ts','main.jsx','main.tsx','app.py','app.js','app.ts','app.jsx','app.tsx','server.js','server.ts','manage.py','main.go','main.rs'}:
                role_coverage['entrypoints'].append(path)
            if basename in {'package.json','pyproject.toml','requirements.txt','go.mod','cargo.toml','pom.xml','config.js','config.ts','settings.py','.env.example'} or '/config/' in path.lower():
                role_coverage['configuration'].append(path)
            if re.search(r'(?:mongoose\.connect|create_engine|MongoClient|QdrantClient|Redis|SessionLocal|DATABASE_URL)',body,re.I):
                role_coverage['database_connections'].append(path)
            if '/routes/' in path.lower() or 'symbol_type=route' in header.string.lower():
                role_coverage['routes'].append(path)
            if '/services/' in path.lower() or 'service' in header.string.lower():
                role_coverage['services'].append(path)
        output['documentation_profiles'].append({
            'kind': kind,
            'paths': list(dict.fromkeys(hit.payload.get('file_path', '') for hit in hits)),
            'source_kinds': sorted({source['source_kind'] for source in context['sources']}),
            'route_evidence': [source['file_path'] for source in context['sources']
                               if source.get('symbol_type') == 'route' or '/routes/' in source['file_path'].lower()],
            'role_coverage': {role:list(dict.fromkeys(paths)) for role,paths in role_coverage.items()},
            'context_sources': [{'file_path':source['file_path'],'start_line':source['start_line'],'end_line':source['end_line'],
                                 'source_kind':source['source_kind'],'symbol_type':source['symbol_type']}
                                for source in context['sources']],
        })
    output['aggregate']={"expected_path_coverage":{"previous_method":f"{output.pop('baseline_expected_count_total')}/{expected_total}",
                                                   "current":f"{current_expected}/{expected_total}"},
                        "query_count":len(BENCHMARK)}
    print(json.dumps(output,indent=2))

if __name__=='__main__': main()
