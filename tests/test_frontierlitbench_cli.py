import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'evaluation' / 'frontierlitbench_cli.py'


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=ROOT,
                              text=True, encoding='utf-8', capture_output=True)

    def test_prompt_does_not_reveal_gold(self):
        p = self.run_cli('prompt', '--task', 'FB-14', '--json-format')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('ICLR, ICML', p.stdout)
        self.assertNotIn('gold_world_models', p.stdout)

    def test_chat_import_and_score_preserve_raw_and_ranking(self):
        records = [json.loads(s) for s in (ROOT/'frontierlitbench/corpus_v1.1/gold_world_models.jsonl').read_text(encoding='utf8').splitlines() if s.strip()]
        first = next(x for x in records if x['venue'] == 'ICLR')
        raw = 'Here is the answer:\n```json\n' + json.dumps({'papers':[{'rank':1,'title':first['title'],'year':first['year']}]}) + '\n```\n'
        with tempfile.TemporaryDirectory() as d:
            d = Path(d); source=d/'chat.txt'; output=d/'result.json'; report=d/'score.json'
            source.write_text(raw, encoding='utf8')
            p=self.run_cli('import-chat','--task','FB-14','--response',source,'--system','test','--tools','none','--out',output)
            self.assertEqual(p.returncode,0,p.stderr)
            self.assertEqual(json.loads(output.read_text(encoding='utf8'))['raw_response'],raw)
            p=self.run_cli('score','--task','FB-14','--result',output,
                           '--corpus',ROOT/'frontierlitbench/corpus_v1.1/gold_world_models.jsonl',
                           '--adjudications',ROOT/'frontierlitbench/review/final_adjudications_v1.1.json',
                           '--out',report)
            self.assertEqual(p.returncode,0,p.stderr)
            data=json.loads(report.read_text(encoding='utf8'))
            self.assertEqual(data['positions'][0]['status'],'matched')
            self.assertEqual(data['eligible_gold_count'],65)
            self.assertEqual(data['reference_status'],'reviewed_v1.1')
            self.assertNotEqual(self.run_cli('score','--task','FB-14','--result',output,'--out',report).returncode,0)

    def test_does_not_silently_reorder_baseline(self):
        with tempfile.TemporaryDirectory() as d:
            d=Path(d); response=d/'response.txt'
            response.write_text('{"papers":[{"rank":2,"title":"X"}]}',encoding='utf8')
            p=self.run_cli('import-chat','--task','FB-03','--response',response,'--system','test','--tools','none','--out',d/'result.json')
            self.assertNotEqual(p.returncode,0)
            self.assertIn('rank',p.stderr)

    def test_summary_refuses_partial_macro_and_mixed_systems(self):
        with tempfile.TemporaryDirectory() as d:
            d=Path(d)
            record={'task':'FB-03','run_id':'one','status':'pending_adjudication','metrics':None,
                    'corpus':'gold_agents.jsonl','adjudications':None,'system':'test'}
            (d/'one.json').write_text(json.dumps(record),encoding='utf8')
            p=self.run_cli('summary','--track','C','--scores',d/'one.json','--out',d/'summary.json')
            self.assertEqual(p.returncode,0,p.stderr)
            result=json.loads((d/'summary.json').read_text(encoding='utf8'))
            self.assertEqual(result['status'],'incomplete')
            self.assertIsNone(result['macro_ndcg_at_5'])
            record.update(run_id='two',system='different')
            (d/'two.json').write_text(json.dumps(record),encoding='utf8')
            p=self.run_cli('summary','--track','C','--scores',d/'one.json',d/'two.json','--out',d/'mixed.json')
            self.assertNotEqual(p.returncode,0)

    def test_overall_requires_all_three_complete_track_summaries(self):
        with tempfile.TemporaryDirectory() as d:
            d=Path(d); paths=[]
            for track, value in [('A', .3), ('B', .6), ('C', .9)]:
                path=d/(track+'.json'); paths.append(path)
                path.write_text(json.dumps({'track':track,'system':'test','conditions':{'tools':'search'},
                                            'repetitions_per_task':3,'status':'scored','macro_ndcg_at_5':value}),encoding='utf8')
            p=self.run_cli('overall','--summaries',*paths,'--out',d/'overall.json')
            self.assertEqual(p.returncode,0,p.stderr)
            self.assertAlmostEqual(json.loads((d/'overall.json').read_text(encoding='utf8'))['overall_ndcg_at_5'],.6)
            value=json.loads(paths[2].read_text(encoding='utf8'));value.update(status='incomplete',macro_ndcg_at_5=None)
            paths[2].write_text(json.dumps(value),encoding='utf8')
            p=self.run_cli('overall','--summaries',*paths,'--out',d/'incomplete.json')
            self.assertEqual(p.returncode,0,p.stderr)
            self.assertIsNone(json.loads((d/'incomplete.json').read_text(encoding='utf8'))['overall_ndcg_at_5'])


if __name__ == '__main__':
    unittest.main()
