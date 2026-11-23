import unittest
from types import SimpleNamespace
from unittest.mock import patch
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.services.ai.groq_service import GroqRateLimitError, complete, complete_with_metadata
from app.services.ai.review_scheduler import GroqReviewScheduler, ReviewRateLimitExhausted
from app.models.models import GeneratedDocument, Repository
from app.api import routes
from fastapi import HTTPException


class ReviewSchedulerTests(unittest.TestCase):
    def scheduler(self, **kwargs):
        sleeps=[]
        config=dict(min_interval=0,max_retries=3,backoff_base=2,max_retry_wait=30,jitter=0,
            sleep=sleeps.append,random_uniform=lambda _low,_high:0)
        config.update(kwargs);scheduler=GroqReviewScheduler(**config)
        return scheduler,sleeps

    def test_429_respects_retry_after_then_succeeds(self):
        scheduler,sleeps=self.scheduler()
        responses=iter([GroqRateLimitError(7),"ok"])
        result=scheduler.run(lambda: self.next_response(responses))
        self.assertEqual(result,"ok")
        self.assertEqual(sleeps,[7])

    def test_exponential_backoff_is_bounded_and_has_jitter(self):
        sleeps=[]; responses=iter([GroqRateLimitError(),GroqRateLimitError(),"ok"])
        scheduler=GroqReviewScheduler(min_interval=0,max_retries=3,backoff_base=2,max_retry_wait=30,jitter=.5,
            sleep=sleeps.append,random_uniform=lambda low,high:high)
        self.assertEqual(scheduler.run(lambda:self.next_response(responses)),"ok")
        self.assertEqual(sleeps,[2.5,4.5])

    def test_retry_after_above_wait_ceiling_stops_without_sleeping(self):
        scheduler,sleeps=self.scheduler(max_retry_wait=10)
        with self.assertRaises(ReviewRateLimitExhausted) as caught:
            scheduler.run(lambda:(_ for _ in ()).throw(GroqRateLimitError(90)))
        self.assertEqual(caught.exception.retry_after,90)
        self.assertEqual(sleeps,[])

    def test_groq_429_preserves_retry_after_header(self):
        error=RuntimeError('rate limited');error.status_code=429
        error.response=SimpleNamespace(headers={'retry-after':'12'})
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs:(_ for _ in ()).throw(error))))
        with patch('app.services.ai.groq_service.settings.groq_api_key','configured'),patch('app.services.ai.groq_service.settings.groq_model','test-model'),patch('app.services.ai.groq_service.Groq',return_value=client):
            with self.assertRaises(GroqRateLimitError) as caught:complete('system','user',max_retries=0,raise_rate_limit=True)
        self.assertEqual(caught.exception.retry_after,12)
        with patch('app.services.ai.groq_service.settings.groq_api_key','configured'),patch('app.services.ai.groq_service.settings.groq_model','test-model'),patch('app.services.ai.groq_service.Groq',return_value=client):
            with self.assertRaises(HTTPException) as generic:complete('system','user',max_retries=0)
        self.assertEqual(generic.exception.status_code,503)

    def test_json_mode_falls_back_when_model_rejects_response_format(self):
        calls=[]
        unsupported=RuntimeError('response_format unsupported');unsupported.status_code=400
        answer=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"findings":[]}'))])
        def create(**params):
            calls.append(params)
            if 'response_format' in params:raise unsupported
            return answer
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch('app.services.ai.groq_service.settings.groq_api_key','configured'),patch('app.services.ai.groq_service.settings.groq_model','test-model'),patch('app.services.ai.groq_service.Groq',return_value=client):
            response=complete('system','user',max_retries=0,json_mode=True)
        self.assertEqual(response,'{"findings":[]}')
        self.assertEqual(len(calls),2)
        self.assertIn('response_format',calls[0]);self.assertNotIn('response_format',calls[1])

    def test_generation_metadata_reports_finish_reason_and_usage_without_prompt_logging(self):
        answer=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{}'),finish_reason='stop')],
                               usage=SimpleNamespace(prompt_tokens=123,completion_tokens=34,total_tokens=157))
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kwargs:answer)))
        with (patch('app.services.ai.groq_service.settings.groq_api_key','configured'),
              patch('app.services.ai.groq_service.settings.groq_model','test-model'),
              patch('app.services.ai.groq_service.Groq',return_value=client)):
            result=complete_with_metadata('private prompt','private evidence',max_retries=0,include_usage=True)
        self.assertEqual(result,('{}','stop',{'prompt_tokens':123,'completion_tokens':34,'total_tokens':157}))

    def test_default_review_queue_keeps_only_one_request_active(self):
        scheduler=GroqReviewScheduler(concurrency=1,min_interval=0,max_retries=0)
        guard=threading.Lock();active=0;maximum=0
        def request():
            nonlocal active,maximum
            with guard:active+=1;maximum=max(maximum,active)
            time.sleep(.01)
            with guard:active-=1
        with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(lambda _item:scheduler.run(request),range(8)))
        self.assertEqual(maximum,1)

    def test_rate_limited_job_keeps_deterministic_findings_and_can_resume(self):
        source={'a.py':{1:'eval(user_input)'},'b.py':{1:'value = 1'}}
        record=self.make_record(source)
        row=SimpleNamespace(id=5,kind='review_v2',repository_id=7,content=json.dumps(record))
        repo=SimpleNamespace(id=7,commit_sha='abc',index_generation='g1')
        db=_FakeDB(row,repo)
        with patch.object(routes,'SessionLocal',return_value=db),patch.object(routes,'_review_source_lines',side_effect=lambda _repo,path:source[path]),patch.object(routes,'_review_ai_sources',side_effect=ReviewRateLimitExhausted(60)):
            routes._run_repository_review(5)
        saved=json.loads(row.content)
        payload=routes._review_job_payload(saved)
        self.assertEqual(saved['status'],'rate_limited')
        self.assertTrue(payload['can_resume'])
        self.assertGreaterEqual(payload['progress']['deterministic_reviewed'],1)
        self.assertTrue(any(item['source']=='static' for item in payload['findings']))
        self.assertEqual(payload['progress']['rate_limited'],2)

    def test_resume_skips_already_completed_ai_batch(self):
        large={n:(f'const value_{n} = "'+('x'*65)+'";') for n in range(1,201)}
        source={'a.py':{1:'const a = 1;'},'b.py':{1:'const b = 2;'},'large.js':large}
        record=self.make_record(source)
        first_key=hashlib.sha256(json.dumps([('a.py',1,1),('b.py',1,1)],separators=(',',':')).encode()).hexdigest()[:24]
        record['completed_batches']=[first_key]
        for item in record['files'][:2]:item.update(deterministic_done=True,deterministic_status='completed')
        record['files'][0]['findings']=[{'id':'saved','source':'static','file_path':'a.py','start_line':1,'severity':'High','title':'Saved'}]
        row=SimpleNamespace(id=6,kind='review_v2',repository_id=7,content=json.dumps(record))
        repo=SimpleNamespace(id=7,commit_sha='abc',index_generation='g1');db=_FakeDB(row,repo)
        with patch.object(routes,'SessionLocal',return_value=db),patch.object(routes,'_review_source_lines',side_effect=lambda _repo,path:source[path]),patch.object(routes,'_review_ai_sources',return_value=[]) as ai_call:
            routes._run_repository_review(6)
        self.assertTrue(all('a.py' not in call.args[1] and 'b.py' not in call.args[1] for call in ai_call.call_args_list))
        payload=routes._review_job_payload(json.loads(row.content))
        self.assertEqual(payload['status'],'completed')
        self.assertEqual(payload['progress']['ai_reviewed'],3)
        self.assertTrue(any(item['id']=='saved' for item in payload['findings']))

    @staticmethod
    def make_record(source):
        files=[{'path':path,'deterministic_status':'waiting','deterministic_done':False,'ai_status':'pending','findings':[]} for path in source]
        return {'repository_id':7,'commit_sha':'abc','index_generation':'g1','scope':'repository','categories':['Security','Bug'],
            'files':files,'files_by_path':{item['path']:item for item in files},'completed_batches':[],'status':'queued'}

    @staticmethod
    def next_response(responses):
        value=next(responses)
        if isinstance(value,Exception): raise value
        return value


class _FakeDB:
    def __init__(self,row,repo):self.row=row;self.repo=repo
    def get(self,model,_identity):return self.row if model is GeneratedDocument else self.repo if model is Repository else None
    def commit(self):pass
    def close(self):pass


if __name__=='__main__':
    unittest.main()
