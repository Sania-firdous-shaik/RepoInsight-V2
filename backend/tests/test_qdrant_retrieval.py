import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services.vector_store import qdrant

def p(pid,path,symbol,content,repo=1,generation='active'):
    return SimpleNamespace(id=pid,payload={'repository_id':repo,'index_generation':generation,
        'file_path':path,'symbol_name':symbol,'symbol_type':'function','content':content,
        'start_line':1,'end_line':4,'content_hash':f'{path}:{symbol}'})

class FakeClient:
    def __init__(self,points,dense=()): self.points=points; self.dense=dense; self.offsets=[]; self.query_filter=None
    def collection_exists(self,name): return True
    def query_points(self,name,query,query_filter,limit,with_payload):
        self.query_filter=query_filter
        return SimpleNamespace(points=list(self.dense)[:limit])
    def scroll(self,name,scroll_filter,limit,offset,with_payload,with_vectors):
        start=offset or 0; self.offsets.append(start)
        batch=self.points[start:start+limit]
        next_offset=start+limit if start+limit<len(self.points) else None
        return batch,next_offset

class QdrantAdapterTests(unittest.TestCase):
    def setUp(self): qdrant.clear_repository_cache()
    def tearDown(self): qdrant.clear_repository_cache()

    def test_search_paginates_all_repo_chunks_and_finds_late_exact_symbol(self):
        points=[p(i,f'src/file_{i}.py',f'worker{i}','generic worker') for i in range(1600)]
        target=p('target','craft-app-backend/routes/payments.js','verifyPayment','function verifyPayment(signature) { return verify(signature) }')
        points.append(target)
        fake=FakeClient(points,dense=[p('cross','other/repo.js','verifyPayment','verifyPayment',repo=99)])
        with patch.object(qdrant,'client',fake), patch.object(qdrant,'ensure_collection',lambda:None):
            hits=qdrant.search(1,[0.0]*384,'verifyPayment',index_generation='active')
        self.assertGreaterEqual(len(fake.offsets),4)
        self.assertTrue(hits)
        self.assertEqual(hits[0].id,'target')
        self.assertTrue(all(h.payload['repository_id']==1 for h in hits))
        self.assertEqual(fake.query_filter.must[0].key,'repository_id')
        self.assertEqual(fake.query_filter.must[0].match.value,1)

    def test_legacy_generation_reads_only_untagged_points(self):
        points=[p('legacy','src/old.py','legacySymbol','legacySymbol',generation=None),p('staged','src/new.py','stagedSymbol','stagedSymbol',generation='pending')]
        fake=FakeClient(points)
        with patch.object(qdrant,'client',fake), patch.object(qdrant,'ensure_collection',lambda:None):
            loaded=qdrant._load_repository_points(1,None)
        self.assertEqual([x.id for x in loaded],['legacy'])

    def test_multi_file_queries_get_budget_bounded_candidates_without_changing_simple_query_limit(self):
        points=[p(str(i),f'src/module_{i}.js',f'module{i}',f'order workflow implementation step {i}') for i in range(12)]
        fake=FakeClient(points,dense=points)
        with (patch.object(qdrant,'client',fake),patch.object(qdrant,'ensure_collection',lambda:None),
              patch.object(qdrant.settings,'rag_max_context_chunks',4),patch.object(qdrant.settings,'rag_context_token_budget',4000)):
            workflow=qdrant.search(1,[0.0]*384,'Trace the complete order workflow across all modules',index_generation='active')
        self.assertEqual(len(workflow),8)
        self.assertLessEqual(len(workflow),4000//500)

        qdrant.clear_repository_cache()
        simple=FakeClient(points,dense=points)
        with (patch.object(qdrant,'client',simple),patch.object(qdrant,'ensure_collection',lambda:None),
              patch.object(qdrant.settings,'rag_max_context_chunks',4),patch.object(qdrant.settings,'rag_context_token_budget',4000)):
            ordinary=qdrant.search(1,[0.0]*384,'What is module 3?',index_generation='active')
        self.assertEqual(len(ordinary),4)

if __name__=='__main__': unittest.main()
