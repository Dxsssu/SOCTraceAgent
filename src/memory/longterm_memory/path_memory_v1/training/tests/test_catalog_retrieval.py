import unittest
from src.memory.longterm_memory.path_memory_v1.training.retrieval import Retriever
from src.memory.longterm_memory.path_memory_v1.training.runtime import MemoryBundle


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.r=Retriever(lambda **kw:'{}')
        self.intent={'wanted':{'concept':'URL'},'retrieval_query':'malicious URL click suspicious exploration same device','constraints':[]}
        self.entities=[{'id':'E1','concept':'URL','value':'https://example.test'}]
        self.titles=[e['title'] for e in self.r.bundle.sm['events'].values()]

    def test_route_scope_and_known_wanted(self):
        cs=self.r.pm_candidates(self.intent,self.entities,self.titles)
        self.assertTrue(cs)
        self.assertTrue(all(c['known']=='URL' and c['wanted']=='URL' for c in cs))
        self.assertEqual(self.r.pm_candidates(self.intent,self.entities,[]),[])
        for c in cs:
            self.assertNotIn('source_refs',c)
            self.assertNotIn('answer',c)

    def test_sm_can_generate_without_em(self):
        self.r.bundle.em={}
        events={'URL':['A potentially malicious URL click was detected']}
        self.assertEqual(self.r.step_candidates(self.entities,events,'EM'),[])
        cards=self.r.step_candidates(self.entities,events,'SM')
        self.assertTrue(any(c['wanted']=='Account_SID' for c in cards))
        self.assertTrue(all('fields' in c for c in cards))

    def test_actual_output_types_filter_empty_templates(self):
        events={'URL':{'click':['Account_SID']}}
        for layer in ('EM','SM'):
            cs=self.r.step_candidates(self.entities,events,layer)
            self.assertTrue(cs)
            self.assertEqual({c['wanted'] for c in cs},{'Account_SID'})

    def test_json_format_retry_contains_error(self):
        import json
        prompts=[]
        def chat(**kw):
            prompts.append(json.loads(kw['user_prompt']))
            return '' if len(prompts)==1 else '{"decision":"no_match"}'
        self.r.chat=chat
        self.assertEqual(self.r.ask('prompt',{'context':'c'})['decision'],'no_match')
        self.assertIn('error',prompts[1])
        self.assertEqual(len(prompts),2)

    def test_inconsistent_constraint_choice_falls_back(self):
        self.r.ask=lambda *a:{'decision':'match','candidate_id':'a','entity_ids':['E1'],'constraint_checks':[]}
        intent={**self.intent,'constraints':['same device']}
        selected,decision=self.r.select('c','q',intent,self.entities,[{'id':'a','known':'URL'}])
        self.assertIsNone(selected)
        self.assertIn('validation_error',decision)

    def test_constraint_sources_must_come_from_question(self):
        self.r.intent_reader.identify=lambda *a:{'constraints':['Unknown answer'], 'constraint_sources':['system rule']}
        with self.assertRaisesRegex(ValueError,'literal input'):
            self.r.identify('context','question')

    def test_reject_unobserved_literal(self):
        import json
        self.r.chat=lambda **kw:json.dumps({'known':[{'id':'E1','concept':'URL','value':'invented'}]})
        with self.assertRaisesRegex(ValueError,'grounded'):
            self.r.intent_reader.identify('known URL','which URL?')

    def test_reject_unknown_candidate_and_binding(self):
        c={'id':'valid','known':'URL','wanted':'URL'}
        self.r.ask=lambda *a:{'decision':'match','candidate_id':'invented','entity_ids':['E1']}
        selected,d=self.r.select('c','q',self.intent,self.entities,[c])
        self.assertIsNone(selected)
        self.assertIn('outside',d['rejected_decisions'][0]['error'])
        self.r.ask=lambda *a:{'decision':'match','candidate_id':'valid','entity_ids':['missing']}
        selected,d=self.r.select('c','q',self.intent,self.entities,[c])
        self.assertIsNone(selected)
        self.assertIn('binding',d['rejected_decisions'][0]['error'])

    def test_constraint_validation(self):
        with self.assertRaisesRegex(ValueError,'Incomplete'):
            self.r.check_constraints({'constraint_checks':[]},{'constraints':['same device']})
        with self.assertRaisesRegex(ValueError,'Unsupported'):
            self.r.check_constraints({'constraint_checks':[{'constraint':'same device','supported':False,'reason':'unknown'}]}, {'constraints':['same device']})

    def test_embedding_rejects_bad_vectors(self):
        self.r.embed=lambda texts:[[0.0] for _ in texts]
        with self.assertRaisesRegex(ValueError,'embeddings'):
            self.r.rank('URL',[{'id':'a'}])

    def test_needs_evidence_falls_back(self):
        self.r.ask=lambda *a:{'decision':'needs_evidence','reason':'unknown scope'}
        selected,_=self.r.select('c','q',self.intent,self.entities,[{'id':'a','known':'URL'}])
        self.assertIsNone(selected)

if __name__=='__main__':unittest.main()

class LoopTests(unittest.TestCase):
    def test_em_discovery_returns_to_pm(self):
        from unittest.mock import patch
        r=Retriever(lambda **kw:'{}')
        r.intent_reader.identify=lambda *a:{'known':[{'id':'E1','concept':'URL','value':'u'}], 'wanted':{'concept':'URL'},'constraints':[],'retrieval_query':'q'}
        layers=[]
        def select(context,question,intent,entities,candidates):
            layer=candidates[0]['layer'];layers.append(layer)
            if layer=='PM' and len(entities)==1:return None,{'decision':'no_match'}
            if layer=='EM':
                eid=next(e['id'] for e in r.bundle.em.values() if e['signature']['known']=='URL' and e['signature']['wanted']=='Account_SID')
                return {'id':'em','episode_id':eid,'known':'URL','wanted':'Account_SID','event':'click','values':['u']},{'decision':'match'}
            return {'id':'pm','pm_id':'p','scenario_id':'s','route_id':'r','known':'Account_SID','wanted':'URL','values':['sid']},{'decision':'match'}
        r.select=select
        r.pm_candidates=lambda *a:[{'id':'pm','known':'Account_SID','layer':'PM'}]
        r.step_candidates=lambda *a:[{'id':'em','known':'URL','layer':'EM'}]
        def output(concept,value):return {'status':'ok','outputs':[{'concept':concept,'graph_value':value,'alert_id':'a'}]}
        class Backend:
            def execute(self,*a):return output('Account_SID','sid')
        r.bundle.execute=lambda *a:{'trace':[{'step':'T1','episode_id':'e',**output('URL','v')}]}
        r.ask=lambda *a:{'decision':'answer','values':['v'],'constraint_checks':[]}
        with patch('src.memory.longterm_memory.path_memory_v1.training.retrieval.available_events',return_value=([],{})):
            result=r.run('c','q',Backend())
        self.assertEqual(layers,['PM','EM','PM'])
        self.assertEqual(result['values'],['v'])

    def test_repeat_is_excluded(self):
        from unittest.mock import patch
        r=Retriever(lambda **kw:'{}')
        r.intent_reader.identify=lambda *a:{'known':[{'id':'E1','concept':'URL','value':'u'}], 'wanted':{'concept':'Host_Name'},'constraints':[],'retrieval_query':'q'}
        eid=next(e['id'] for e in r.bundle.em.values() if e['signature']['known']=='URL')
        candidate={'id':'em','episode_id':eid,'known':'URL','wanted':'Account_SID','event':'click'}
        r.step_candidates=lambda *a:[candidate]
        r.select=lambda c,q,i,e,cs:({**cs[0],'values':['u']},{'decision':'match'}) if cs else (None,{'decision':'no_match'})
        class Backend:
            calls=0
            def execute(self,*a):
                self.calls+=1
                return {'status':'empty','outputs':[]}
        b=Backend()
        with patch('src.memory.longterm_memory.path_memory_v1.training.retrieval.available_events',return_value=([],{})):
            result=r.run('c','q',b,disabled=('PM','SM'))
        self.assertEqual(b.calls,1)
        self.assertEqual(result['status'],'no_match')
