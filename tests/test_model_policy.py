"""Stage effort and cache identity contracts using a local response object only."""
from dataclasses import replace
from io import BytesIO
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import Budget,Config
from llm import JsonLLM
from model_identity import backend_identity,research_model_identity
from model_policy import inference_policy

class Reply(BytesIO):
    status=200
    headers={}

class ModelPolicyTests(unittest.TestCase):
    def config(self,**changes):
        return replace(Config(model='MiniMax-M3.1-Flash',base_url='https://api.minimaxi.com/v1',
            protocol='responses',research_mode=True),**changes)

    def request(self,stage,config):
        captured=[]
        def fetch(request,**kwargs):
            captured.append(json.loads(request.data))
            data={'status':'completed','output':[{'type':'message','content':[
                {'type':'output_text','text':'{"ok":true}'}]}],
                'usage':{'input_tokens':12,'output_tokens':9}}
            if config.protocol=='chat_completions':
                data={'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}],
                      'usage':{'prompt_tokens':12,'completion_tokens':9}}
            return Reply(json.dumps(data).encode())
        llm=JsonLLM(config,Budget(10))
        with patch('llm.urlopen',side_effect=fetch):
            self.assertEqual(llm.generate(stage,'Return JSON.',{}, {'type':'object'},lambda value:None),{'ok':True})
        return captured[0],llm

    def test_official_responses_uses_medium_for_semantic_assessment_and_synthesis(self):
        for stage,expected in (('assess','medium'),('research_plan','low'),('synthesis','medium')):
            with self.subTest(stage=stage):
                body,llm=self.request(stage,self.config())
                self.assertEqual(body['reasoning'],{'effort':expected})
                self.assertEqual(llm.events[0]['inference_policy']['reasoning_effort'],expected)
                self.assertNotIn('thinking',body)

    def test_chat_completions_uses_the_correct_protocol_field(self):
        body,_=self.request('assess',self.config(protocol='chat_completions'))
        self.assertEqual(body['reasoning_effort'],'medium')
        self.assertNotIn('reasoning',body)

    def test_legacy_unknown_model_or_custom_backend_keeps_provider_defaults(self):
        for config in (self.config(research_mode=False),self.config(model='unknown-model'),
                       self.config(base_url='https://other.invalid/v1')):
            with self.subTest(config=config.public()):
                body,_=self.request('assess',config)
                self.assertNotIn('reasoning',body)
                self.assertNotIn('reasoning_effort',body)

    def test_backend_budget_identity_is_stable_but_inference_cache_is_isolated(self):
        research=self.config();legacy=replace(research,research_mode=False)
        self.assertEqual(backend_identity(research),backend_identity(legacy))
        self.assertNotEqual(research_model_identity(research),research_model_identity(legacy))

    def test_synthesis_deadline_is_extended_but_still_bounded_by_the_task_budget(self):
        deadlines=[]
        def fetch(request,**kwargs):
            deadlines.append(kwargs['timeout'])
            return Reply(json.dumps({'status':'completed','output':[{'type':'message','content':[
                {'type':'output_text','text':'{"ok":true}'}]}],
                'usage':{'input_tokens':12,'output_tokens':9}}).encode())
        for budget in (1000,120):
            for stage in ('assess','synthesis'):
                llm=JsonLLM(self.config(),Budget(budget))
                with patch('llm.urlopen',side_effect=fetch):
                    llm.generate(stage,'Return JSON.',{}, {'type':'object'},lambda value:None)
        self.assertAlmostEqual(deadlines[0],90)
        self.assertAlmostEqual(deadlines[1],180)
        self.assertLessEqual(deadlines[2],120)
        self.assertLessEqual(deadlines[3],120)
        self.assertGreater(deadlines[3],deadlines[2])

if __name__=='__main__':unittest.main()
