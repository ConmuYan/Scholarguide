"""Small live research acceptance run with a persistent monetary admission guard.

This is product QA, not an expert-labelled benchmark or a causal comparison.
Admission initially reserves ten times unverified profile prices. The officially
verified MiniMax M3.1 rate uses a twofold safety margin, with a conservative
10 CNY/USD budget factor (not an FX quote). Unknown usage retains its whole
token/output bound. Reconciliation preserves original reservations and failures.
"""
from dataclasses import replace
from datetime import datetime, timezone
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from common import Budget, Config, GuideError
from costing import cost_breakdown
from llm import JsonLLM
from model_identity import research_model_identity, backend_identity
from providers import ProviderStore
from report import markdown
from source import AcademicSource
from workflow import run
from urllib.parse import urlsplit

CASES=[
 {'id':'3d-frontier','request':'推荐5篇近年的3D场景重建论文，了解不同技术路线、方法局限和开放问题，寻找可检验的研究方向。',
  'task':{'topic':'3D scene reconstruction','purpose':'了解不同技术路线、方法局限和开放问题，寻找可检验的研究方向。',
          'query':'3D scene reconstruction','target_count':5,'reading_goal':'frontier','prefer_recent':True,
          'year_from':2024,'year_to':2026,'venue_scope':'preferred'}},
 {'id':'iclr-poster','request':'推荐3篇ICLR 2025正式录用为Poster的视觉表征学习论文，比较方法。',
  'task':{'topic':'visual representation learning','purpose':'比较视觉表征学习方法。',
          'query':'visual representation learning','target_count':3,'reading_goal':'comparison',
          'venue':'ICLR','year_from':2025,'year_to':2025,'publication_type':'conference',
          'accepted_only':True,'presentation_types':['poster']}},
 {'id':'journal-survey','request':'推荐3篇Artificial Intelligence Review近年关于大语言模型的论文，至少2篇综述，用于梳理研究脉络。',
  'task':{'topic':'large language model survey','purpose':'梳理大语言模型研究脉络。',
          'query':'large language model survey','target_count':3,'reading_goal':'orientation',
          'venue':'AI Review','year_from':2024,'year_to':2026,'publication_type':'journal',
          'accepted_only':True,'category':'survey','minimum_category_count':2}},
]

def atomic(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    temporary.replace(path)

def fingerprint():
    names=list(ROOT.glob('*.py'))+[Path(__file__).resolve()]
    return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(names)}

class MonetaryGuard:
    """Admission guard whose spending ceiling is an auditable authorization history.

    The ledger records every granted authorization separately, so a continuation
    cannot silently raise its own ceiling: the effective limit is always the sum
    of the recorded grants, and any mismatch is refused.
    """
    MAX_AUTHORIZATION_CNY=10.0

    @staticmethod
    def _authorization(kind,amount,note,stamp=None,backfilled=False):
        record={'id':kind,'cny':amount,'note':note,
                'granted_at':stamp or datetime.now(timezone.utc).isoformat()}
        if backfilled:record['backfilled']=True
        return record

    def __init__(self,path,limit,config):
        self.path,self.config=path,config
        for price in (config.input_price,config.output_price):
            if price is None or not math.isfinite(price) or price<=0:
                raise GuideError('Live validation requires positive configured token prices.')
        currency=config.currency.upper()
        if currency not in ('USD','CNY','RMB'):
            raise GuideError('Only a USD or CNY priced profile can use this CNY admission guard.')
        self.factor=10.0 if currency=='USD' else 1.0
        verified=(config.model.casefold()=='minimax-m3.1-flash' and currency=='USD'
            and config.input_price==0.15 and config.output_price==0.6
            and urlsplit(config.base_url).hostname in ('api.minimaxi.com','api.minimax.io','api.minimax.cn'))
        self.margin=2.0 if verified else 10.0
        self.data=json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        if self.data is None:
            if limit is None or not math.isfinite(limit) or not 0<limit<=self.MAX_AUTHORIZATION_CNY:
                raise GuideError('A new validation ledger cannot start above one CNY 10 authorization.')
            self.data={
                'limit_cny':limit,'profile_model_identity':backend_identity(config),'entries':[],
                'authorizations':[self._authorization('initial',limit,
                    'Original authorization recorded when this validation ledger was created.')],
                'pricing_basis':'Profile token estimates x10 safety margin; USD budget conversion factor 10 CNY/USD, not an FX quote.'}
        if self.data.get('profile_model_identity')!=backend_identity(config):
            raise GuideError('Existing validation ledger has a different limit or backend identity.')
        if 'authorizations' not in self.data:
            # A ledger written before authorization history existed: its recorded
            # limit was itself the single grant, so preserve it as one backfilled
            # authorization instead of inventing a new ceiling.
            self.data['authorizations']=[self._authorization('initial',self.data['limit_cny'],
                'Backfilled from the limit recorded before authorization history existed.',
                stamp=(self.data['entries'][0].get('started_at') if self.data.get('entries') else None),
                backfilled=True)]
        authorized=sum(record['cny'] for record in self.data['authorizations'])
        if abs(authorized-self.data['limit_cny'])>1e-9:
            raise GuideError('Authorization history does not add up to the recorded limit.')
        if limit is not None and abs(self.data['limit_cny']-limit)>1e-9:
            raise GuideError('Existing validation ledger has a different limit or backend identity.')
        self.limit=self.data['limit_cny']
        if verified:
            self.data.setdefault('initial_pricing_basis',self.data.get('pricing_basis'))
            self.data['pricing_basis']='Official MiniMax M3.1 rates, 2x safety margin; conservative USD budget conversion factor 10 CNY/USD, not an FX quote.'
            for entry in self.data['entries']:
                original=entry.get('protected_cny',entry['reserved_cny'])
                entry['admission_bound_cny']=original*self.margin/entry.get('pricing_safety_factor',10.0)
            self.data['official_price_verification']={
                'reference':'https://www.minimax.io/blog/minimax-m31f',
                'input_usd_per_1m':0.15,'output_usd_per_1m':0.60,
                'checked_at':datetime.now(timezone.utc).isoformat(),
                'safety_factor':self.margin,'usd_budget_conversion_factor_cny':self.factor,
                'reconciliation':'Original reservations are retained; unknown calls retain their full maximum token bound at verified rates.'}
        atomic(self.path,self.data)

    def used(self):
        return sum(entry.get('admission_bound_cny',entry.get('protected_cny',entry['reserved_cny']))
                   for entry in self.data['entries'])

    @classmethod
    def expected_total(cls,path,authorize_cny=None,authorization_id=None):
        """Read-only ceiling check that runs before any provider profile is loaded."""
        path=Path(path)
        if not path.exists():return None
        data=json.loads(path.read_text(encoding='utf-8'))
        grants=data.get('authorizations')
        total=sum(grant['cny'] for grant in grants) if grants is not None else data['limit_cny']
        if authorize_cny is not None and not any(grant['id']==authorization_id for grant in grants or []):
            total+=authorize_cny
        return total

    def require_limit(self,limit):
        """Adopt or verify the effective ceiling after any grant in this process."""
        if abs(self.data['limit_cny']-limit)>1e-9:
            raise GuideError('Requested budget does not match the recorded authorizations.')
        self.limit=limit
        return self

    def grant(self,amount,authorization_id,note=''):
        """Append one user authorization. Each grant is bounded and unique."""
        if not math.isfinite(amount) or not 0<amount<=self.MAX_AUTHORIZATION_CNY:
            raise GuideError('Each validation authorization is limited to CNY 10.')
        if not str(authorization_id or '').strip():
            raise GuideError('An authorization needs a stable id.')
        if any(record['id']==authorization_id for record in self.data['authorizations']):
            return self.data['limit_cny']
        self.data['authorizations'].append(self._authorization(authorization_id,amount,
            note or 'User-granted continuation authorization.'))
        self.data['limit_cny']=sum(record['cny'] for record in self.data['authorizations'])
        self.limit=self.data['limit_cny']
        atomic(self.path,self.data)
        return self.limit

    def used_since(self,stamp):
        """Spending bound attributable to one authorization window."""
        return sum(entry.get('admission_bound_cny',entry.get('protected_cny',entry['reserved_cny']))
                   for entry in self.data['entries']
                   if str(entry.get('started_at') or '')>=str(stamp))

    def reserve(self,messages,schema,stage,case):
        token_bound=len(json.dumps([messages,schema],ensure_ascii=False).encode('utf-8'))+8192
        c=self.config
        reserve=self.margin*self.factor*(token_bound*c.input_price+c.max_output_tokens*c.output_price)/1_000_000
        if self.used()+reserve>self.limit:
            raise GuideError('Validation monetary reservation limit reached before issuing an API request.')
        item={'case':case,'stage':stage,'status':'reserved','reserved_cny':reserve,
              'pricing_safety_factor':self.margin,
              'input_byte_token_bound':token_bound,'output_token_bound':c.max_output_tokens,
              'started_at':datetime.now(timezone.utc).isoformat()}
        self.data['entries'].append(item);atomic(self.path,self.data)
        return item

    def settle(self,item,usage,success):
        item['status']='returned' if success else 'failed_or_unknown'
        if all(type(usage.get(k)) is int and usage[k]>=0 for k in ('input_tokens','output_tokens')):
            c=self.config
            estimated=(usage['input_tokens']*c.input_price+usage['output_tokens']*c.output_price)/1_000_000
            item.update(usage=usage,profile_estimate=estimated,profile_currency=c.currency,
                        protected_cny=item.get('pricing_safety_factor',10.0)*self.factor*estimated)
        item['finished_at']=datetime.now(timezone.utc).isoformat()
        atomic(self.path,self.data)

class GuardedLLM(JsonLLM):
    def __init__(self,config,budget,guard,case):
        super().__init__(config,budget);self.guard,self.case=guard,case;self.stage=''
    def generate(self,stage,*args,**kwargs):
        self.stage=stage
        return super().generate(stage,*args,**kwargs)
    def _call(self,messages,schema):
        self.budget.check_cancelled()
        item=self.guard.reserve(messages,schema,self.stage,self.case)
        self.last_usage={'input_tokens':None,'output_tokens':None}
        success=False
        try:
            output=super()._call(messages,schema);success=True;return output
        finally:
            self.guard.settle(item,self.last_usage,success)

def checks(result):
    from research_policy import publication_matches
    papers=result.get('papers') or []
    brief=result.get('research_brief') or {}
    normal=lambda value:' '.join(str(value or '').split())
    return {'returned':len(papers),'unique_ids':len({p['id'] for p in papers})==len(papers),
        'all_quotes_present':bool(papers) and all(len(normal(p.get('evidence_quote')))>=12 and
              normal(p.get('evidence_quote')) in normal(p.get('abstract')) for p in papers),
        'publication_constraints':bool(papers) and all(publication_matches(p,result.get('request') or {}) for p in papers),
        'coverage_status':(result.get('research_coverage') or {}).get('status'),
        'missing_facets':(result.get('research_coverage') or {}).get('missing'),
        'brief_status':(result.get('research_brief') or {}).get('status'),
        'selected_curated_publications':sum(publication_matches(p,
            {**(result.get('request') or {}),'venue_scope':'preferred_only'}) for p in papers),
        'brief_sections':{key:len(brief.get(key) or []) for key in
            ('paper_notes','themes','open_questions','research_hypotheses','reading_order')},
        'quality_gold_available':False,'semantic_quality_score':None}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--ledger',type=Path,required=True)
    parser.add_argument('--budget-cny',type=float)
    parser.add_argument('--authorize-cny',type=float)
    parser.add_argument('--authorization-id')
    parser.add_argument('--authorization-note',default='')
    parser.add_argument('--ids',default=','.join(case['id'] for case in CASES))
    parser.add_argument('--repeat-first-warm',action='store_true')
    args=parser.parse_args()
    if args.authorize_cny is not None:
        if not args.authorization_id:parser.error('--authorize-cny requires --authorization-id.')
        if not math.isfinite(args.authorize_cny) or not 0<args.authorize_cny<=10:
            parser.error('Each authorization is limited to CNY 10.')
    if args.budget_cny is not None and not (math.isfinite(args.budget_cny) and args.budget_cny>0):
        parser.error('--budget-cny is the effective authorized total and must be positive.')
    if args.budget_cny is None and args.authorize_cny is None:
        parser.error('Provide --budget-cny (effective authorized total) and/or --authorize-cny.')
    if args.out.exists() and any(args.out.iterdir()):
        parser.error('Use a new output directory; paid evidence is never overwritten.')
    expected=MonetaryGuard.expected_total(args.ledger,args.authorize_cny,args.authorization_id)
    if expected is None:
        if args.budget_cny is None or not 0<args.budget_cny<=10:
            parser.error('A new ledger can only start with one authorization of at most CNY 10.')
    elif args.budget_cny is not None and abs(args.budget_cny-expected)>1e-9:
        parser.error('--budget-cny does not match the recorded authorizations.')
    args.out.mkdir(parents=True,exist_ok=True)
    from recovery import JobLease
    import atexit
    lease=JobLease(args.ledger.parent/(args.ledger.name+'.lease'))
    atexit.register(lease.close)
    base=ProviderStore().config(Config.from_env())
    config=replace(base,source='multi',research_mode=True,cache=True,cache_dir=args.out/'cache',
        max_output_tokens=base.max_output_tokens,source_timeout=base.source_timeout,request_timeout=base.request_timeout,
        task_timeout=base.task_timeout,deepseek_search_key='')
    config.validate()
    guard=MonetaryGuard(args.ledger,None if args.authorize_cny is not None else args.budget_cny,config)
    if args.authorize_cny is not None:
        guard.grant(args.authorize_cny,args.authorization_id,args.authorization_note)
        if args.budget_cny is not None:guard.require_limit(args.budget_cny)
    names=args.ids.split(',')
    if any(name not in {case['id'] for case in CASES} for name in names):parser.error('Unknown case id')
    cases=[case for case in CASES if case['id'] in names]
    if args.repeat_first_warm:cases+=[{**cases[0],'id':cases[0]['id']+'-warm'}]
    manifest={'started_at':datetime.now(timezone.utc).isoformat(),'config':config.public(),
        'source_sha256':fingerprint(),'tasks':cases,'cases':[],
        'mode':'live research product QA; no expert gold, no causal baseline claim',
        'budget_ledger':str(args.ledger.resolve()),'admission_limit_cny':args.budget_cny,
        'pricing_admission_basis':guard.data.get('official_price_verification') or guard.data['pricing_basis']}
    atomic(args.out/'manifest.json',manifest)
    for case in cases:
        started=time.monotonic();first=[];events=[]
        def progress(event,checkpoint=None):
            events.append({'elapsed_seconds':round(time.monotonic()-started,3),**event})
            if checkpoint and checkpoint.get('papers') and not first:first.append(time.monotonic()-started)
        progress.accepts_checkpoint=True
        budget=Budget(config.task_timeout)
        llm=GuardedLLM(config,budget,guard,case['id'])
        source=AcademicSource(config,budget)
        result=run(case['request'],config,request_data=case['task'],source=source,llm=llm,
                   budget=budget,progress=progress)
        directory=args.out/case['id'];directory.mkdir()
        atomic(directory/'result.json',result);atomic(directory/'events.json',events)
        (directory/'report.md').write_text(markdown(result),encoding='utf-8')
        row={'id':case['id'],'status':result['status'],'stop_reason':result['stop_reason'],
            'elapsed_seconds':round(time.monotonic()-started,3),
            'first_internal_verified_seconds':round(first[0],3) if first else None,
            'checks':checks(result),'cost':cost_breakdown(config,result.get('metrics'),result.get('trace')),
            'metrics':result.get('metrics'),'source_health':source.health_snapshot(),
            'protected_budget_consumed_cny':guard.used()}
        manifest['cases'].append(row);atomic(args.out/'manifest.json',manifest)
        print(json.dumps({key:row[key] for key in ('id','status','stop_reason','elapsed_seconds','checks','protected_budget_consumed_cny')},ensure_ascii=True),flush=True)
    manifest['source_changed_during_run']=[name for name,value in manifest['source_sha256'].items()
        if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=value]
    manifest['finished_at']=datetime.now(timezone.utc).isoformat()
    atomic(args.out/'manifest.json',manifest)

if __name__=='__main__':main()
