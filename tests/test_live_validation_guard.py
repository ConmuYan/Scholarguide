"""Monetary validation admission is exercised without network/provider access."""
from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import Config, GuideError
from scripts.validate_research_quality import MonetaryGuard

class ValidationGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'ledger.json'
        self.config=Config(model='offline',base_url='https://offline.invalid',
                           input_price=0.15,output_price=0.6,currency='USD',max_output_tokens=6000)
    def tearDown(self):self.tmp.cleanup()

    def test_reservation_survives_restart_and_unknown_usage_is_never_refunded(self):
        guard=MonetaryGuard(self.path,10,self.config)
        item=guard.reserve([{'content':'request'}],{},'plan','fixture')
        amount=guard.used()
        guard.settle(item,{'input_tokens':None,'output_tokens':None},False)
        resumed=MonetaryGuard(self.path,10,self.config)
        self.assertEqual(resumed.used(),amount)
        self.assertGreater(amount,0)

    def test_admission_refuses_before_api_can_run(self):
        guard=MonetaryGuard(self.path,0.001,self.config)
        with self.assertRaises(GuideError):guard.reserve([{'content':'request'}],{},'plan','fixture')
        self.assertEqual(guard.data['entries'],[])

    def test_usage_is_a_conservative_configured_estimate_with_safety_margin(self):
        guard=MonetaryGuard(self.path,10,self.config)
        item=guard.reserve([{'content':'request'}],{},'plan','fixture')
        guard.settle(item,{'input_tokens':100,'output_tokens':100},True)
        self.assertAlmostEqual(guard.used(),100*(100*0.15+100*0.6)/1_000_000)

    def test_unknown_prices_or_different_backend_are_not_silently_accepted(self):
        from dataclasses import replace
        with self.assertRaises(GuideError):MonetaryGuard(self.path,10,replace(self.config,input_price=None))
        MonetaryGuard(self.path,10,self.config)
        with self.assertRaises(GuideError):MonetaryGuard(self.path,10,replace(self.config,model='other'))

    def test_verified_price_reconciliation_preserves_original_unknown_call_bound(self):
        from dataclasses import replace
        from model_identity import backend_identity
        config=replace(self.config,model='MiniMax-M3.1-Flash',base_url='https://api.minimaxi.com/v1')
        old={'limit_cny':10,'profile_model_identity':backend_identity(config),
             'pricing_basis':'unverified profile x10','entries':[
                {'status':'failed_or_unknown','reserved_cny':1.0}]}
        self.path.write_text(json.dumps(old),encoding='utf-8')
        guard=MonetaryGuard(self.path,10,config)
        self.assertAlmostEqual(guard.used(),0.2)
        self.assertEqual(guard.data['entries'][0]['reserved_cny'],1.0)
        self.assertEqual(guard.data['entries'][0]['status'],'failed_or_unknown')
        self.assertIn('official_price_verification',guard.data)

    def test_authorization_history_is_additive_and_individually_bounded(self):
        guard=MonetaryGuard(self.path,10,self.config)
        self.assertEqual(guard.data['authorizations'][0]['cny'],10)
        self.assertAlmostEqual(guard.grant(10,'continuation-20261021','user granted additional CNY 10'),20)
        self.assertEqual(guard.limit,20)
        self.assertEqual([record['id'] for record in guard.data['authorizations']],
                         ['initial','continuation-20261021'])
        reopened=MonetaryGuard(self.path,20,self.config)
        self.assertAlmostEqual(reopened.limit,20)
        with self.assertRaises(GuideError):reopened.grant(10.01,'too-much')
        with self.assertRaises(GuideError):reopened.grant(0,'zero')
        reopened.grant(5,'continuation-20261021')
        self.assertAlmostEqual(reopened.limit,20)

    def test_reported_total_must_match_the_authorization_history(self):
        guard=MonetaryGuard(self.path,10,self.config)
        guard.grant(10,'continuation-20261021')
        with self.assertRaises(GuideError):MonetaryGuard(self.path,10,self.config)
        with self.assertRaises(GuideError):guard.require_limit(15)
        self.assertAlmostEqual(MonetaryGuard(self.path,20,self.config).limit,20)

    def test_legacy_ledger_without_history_backfills_one_authorization(self):
        self.path.write_text(json.dumps({'limit_cny':10,
            'profile_model_identity':__import__('model_identity').backend_identity(self.config),
            'entries':[{'status':'returned','reserved_cny':1.0,'protected_cny':0.5}]}),encoding='utf-8')
        guard=MonetaryGuard(self.path,10,self.config)
        self.assertEqual(len(guard.data['authorizations']),1)
        self.assertTrue(guard.data['authorizations'][0]['backfilled'])
        self.assertAlmostEqual(guard.data['limit_cny'],10)

    def test_spending_above_the_authorized_ceiling_is_refused(self):
        guard=MonetaryGuard(self.path,0.5,self.config)
        guard.reserve([{'content':'first'}],{},'plan','fixture')
        with self.assertRaises(GuideError):
            guard.reserve([{'content':'second'}],{},'plan','fixture')
        guard.grant(0.5,'continuation')
        guard.reserve([{'content':'third'}],{},'plan','fixture')

    def test_used_since_separates_a_continuation_authorization(self):
        guard=MonetaryGuard(self.path,10,self.config)
        guard.reserve([{'content':'old'}],{},'plan','fixture')
        stamp=guard.data['authorizations'][0]['granted_at']
        before=guard.used_since(stamp)
        self.assertGreater(before,0)
        self.assertEqual(guard.used_since('2999-01-01T00:00:00+00:00'),0)
        later=guard.data['entries'][-1]['started_at']
        self.assertAlmostEqual(guard.used_since(later),before)

if __name__=='__main__':unittest.main()
