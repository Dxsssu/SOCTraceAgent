"""LTM planning adapter for the existing environment-stepped workflow.

The ITT owns entities, plan instances, attempts and task dependencies. This adapter
never runs investigation steps: it returns a SQL action for the external executor.
"""
import json
from copy import deepcopy
from dataclasses import replace
from src.schema.ttt import TracebackTaskTree, TTTNode, TTTNodeStatus as S
from src.schema.ttt_updates import walk, change_node
from src.workflow.event_bootstrap import choose_event
from src.memory.longterm_memory.path_memory_v1.training.retrieval import Retriever, available_events
from src.memory.longterm_memory.path_memory_v1.training.core import new_episode, digest


class LTMWMController:
    def __init__(self, backend, chat, *, retriever=None, disabled=()):
        self.backend=backend
        self.retriever=retriever or Retriever(chat)
        self.disabled=set(disabled)
        self.retrievals=[]
        self.metadata_queries=0

    def initialize(self,event,inputs):
        intent=self.retriever.identify(inputs['context'],inputs['question'])
        self.preparation={'intent':intent,'inputs':inputs,'bootstrap_attempted':[]}
        self.preparation_entities=[{**e,'id':f'E{i+1}'} for i,e in enumerate(intent['known'])]
        self.preparation_records=[]
        self.preparation_node=None
        self.preparation_exhausted=False
        return TracebackTaskTree(event_id=event.event_id,round_id=1,root_nodes=())

    def build_tree(self,event_id):
        """Create the formal G/E tree only after observed bootstrap evidence."""
        intent=self.preparation['intent'];inputs=self.preparation['inputs']
        entities=tuple(TTTNode(node_id=f'E{i+1}',title=f"{e['concept']}: {e['value']}",level=2,status=S.OPEN,
            metadata={'concept':e['concept'],'value':e['value'],'scope':event_id,
                      'sources':[{'kind':'input','field':'question' if e['value'] in inputs['question'] else 'context','quote':e['value']}], 'identity_claim':'identifier_observed_only'})
            for i,e in enumerate(intent['known']))
        goal=TTTNode(node_id='G1',title=inputs['question'],level=1,status=S.OPEN,children=entities,
            metadata={'ltm_wm':True,'intent':intent,'inputs':inputs,'procedure_runs':[], 'attempted':[], 'phase':'retrieval', 'bootstrap_completed':True, 'bootstrap_records':deepcopy(self.preparation_records)})
        tree=TracebackTaskTree(event_id=event_id,round_id=1,root_nodes=(goal,))
        self.validate(tree)
        return tree

    @staticmethod
    def entities(tree):
        return [{'id':n.node_id,'concept':n.metadata['concept'],'value':n.metadata['value']}
                for n,_ in walk(tree) if n.level==2]

    @staticmethod
    def allocate(tree,prefix):
        return prefix+str(1+max([int(n.node_id[len(prefix):]) for n,_ in walk(tree) if n.node_id.startswith(prefix)]+[0]))

    @staticmethod
    def validate(tree):
        nodes={n.node_id:n for n,_ in walk(tree)}
        if len(nodes)!=sum(1 for _ in walk(tree)):raise ValueError('Duplicate WM IDs')
        for n,depth in walk(tree):
            if depth!=n.level:raise ValueError('Invalid G/E/T hierarchy')
            if n.level==2 and not (n.metadata.get('concept') and n.metadata.get('value') and n.metadata.get('sources')):
                raise ValueError('Entity must have observed provenance')
            if n.level==3:
                m=n.metadata
                if not m.get('input_entity_ids'):raise ValueError('Missing task inputs')
                for eid in m['input_entity_ids']:
                    if eid not in nodes or nodes[eid].level!=2:raise ValueError('Unknown task entity')
                    if nodes[eid].metadata['concept']!=m['known']:raise ValueError('Input type mismatch')
                for dep in m.get('depends_on',[]):
                    if dep not in nodes or nodes[dep].level!=3 or nodes[dep].status!=S.DONE:
                        raise ValueError('Dependency is not complete')
                    allowed=nodes[dep].metadata.get('output_entity_ids',[])
                    if not set(m['input_entity_ids'])<=set(allowed):raise ValueError('Input not produced by dependency')
                if any(int(dep[1:])>=int(n.node_id[1:]) for dep in m.get('depends_on',[])):
                    raise ValueError('Non-acyclic dependency')
        return tree

    def _task(self,tree,episode,title,entity_ids,*,run_id=None,step=None,depends=(),layer='EM'):
        nodes={n.node_id:n for n,_ in walk(tree)}
        tid=self.allocate(tree,'T')
        node=TTTNode(node_id=tid,title=f"在告警 {title} 中根据 {episode['signature']['known']} 查询 {episode['signature']['wanted']}",
            level=3,status=S.TODO,metadata={'ltm_task':True,'layer':layer,'episode_id':episode['id'],
            'known':episode['signature']['known'],'wanted':episode['signature']['wanted'],
            'event_title':title,'input_entity_ids':list(entity_ids),'depends_on':list(depends),
            'procedure_run_id':run_id,'procedure_step_id':step,'output_entity_ids':[]})
        parent=nodes[entity_ids[0]]
        tree=change_node(tree,parent.node_id,children=(*parent.children,node))
        tree=replace(tree,next_task_id=tid)
        return self.validate(tree)

    def _bootstrap(self, tree):
        if self.preparation_node or self.preparation_exhausted:
            return tree
        chosen,decision=choose_event(self,meta=self.preparation,known=self.preparation_entities)
        self.retrievals.append({'version':tree.version,'layer':'BOOTSTRAP','decision':decision,'selected':chosen})
        if not chosen:
            self.preparation_exhausted=True
            return tree
        self.preparation['bootstrap_attempted'].append(chosen['event_title'])
        # B identifiers belong to preparation actions, never to the ITT.
        self.preparation_node=TTTNode(node_id=f"B{len(self.preparation['bootstrap_attempted'])}",
            title=f"定位候选告警 {chosen['event_title']} 并提取实体",level=0,status=S.TODO,
            metadata={'ltm_task':True,'layer':'BOOTSTRAP','event_title':chosen['event_title'],
                'output_concepts':chosen['concepts'],'anchor_entity_ids':chosen.get('anchor_entity_ids',[])})
        return tree

    def accept_preparation(self,tree,node,execution,raw_observation):
        records=json.loads(raw_observation) if execution.query_success else []
        if not isinstance(records,list):raise ValueError('Expected structured SQL records')
        for row in records:
            if not {'value','AlertId','SourceRowHash','matched_input','EntityIndex','Concept'}<=row.keys():
                raise ValueError('Missing evidence lineage')
            if row.get('AlertTitle')!=node.metadata['event_title'] or row['matched_input']!=node.metadata['event_title']:
                raise ValueError('Output event mismatch')
            if row['Concept'] not in node.metadata['output_concepts']:
                raise ValueError('Unexpected bootstrap entity concept')
        self.preparation_records.append(execution.to_dict())
        self.preparation_node=None
        if not records:return tree
        tree=self.build_tree(tree.event_id)
        for row in records:
            concept=row['Concept'];value=str(row['value'])
            source={'kind':'bootstrap_query','execution_id':execution.execution_id,
                'alert_id':row['AlertId'],'alert_title':row['AlertTitle'],
                'source_row_hash':row['SourceRowHash'],'entity_index':row['EntityIndex']}
            existing=next((n for n,_ in walk(tree) if n.level==2 and n.metadata['concept']==concept and n.metadata['value']==value),None)
            if existing:
                sources=existing.metadata['sources']
                tree=change_node(tree,existing.node_id,metadata={**existing.metadata,
                    'sources':sources if source in sources else sources+[source]},
                    evidence_refs=tuple(dict.fromkeys((*existing.evidence_refs,execution.execution_id))))
            else:
                goal=tree.root_nodes[0]
                entity=TTTNode(node_id=self.allocate(tree,'E'),title=f'{concept}: {value}',level=2,status=S.OPEN,
                    evidence_refs=(execution.execution_id,),metadata={'concept':concept,'value':value,
                        'scope':tree.event_id,'sources':[source],'identity_claim':'identifier_observed_only',
                        'raw_components':row.get('RawComponents')})
                tree=change_node(tree,goal.node_id,children=(*goal.children,entity))
        return self.validate(tree)

    def plan(self,tree):
        self.validate(tree)
        if not tree.root_nodes:return self._bootstrap(tree)
        if tree.next_task_id:
            n=next(n for n,_ in walk(tree) if n.node_id==tree.next_task_id)
            if n.status==S.TODO:return tree
        goal=tree.root_nodes[0];meta=deepcopy(goal.metadata)
        # A registered procedure continues without another retrieval/Planner call.
        for run in meta['procedure_runs']:
            if run['status']!='active':continue
            last=next(n for n,_ in walk(tree) if n.node_id==run['task_ids'][-1])
            if last.status!=S.DONE or not last.metadata['output_entity_ids']:
                run['status']='paused';continue
            index=len(run['task_ids'])
            if index==len(run['steps']):
                run['status']='completed';continue
            spec=run['steps'][index]
            episode=self.retriever.bundle.em[spec['episode_id']]
            tree=self._task(tree,episode,spec['event_title'],last.metadata['output_entity_ids'],
                run_id=run['id'],step=spec['id'],depends=[last.node_id],layer='PM')
            run['task_ids'].append(tree.next_task_id)
            return change_node(tree,goal.node_id,metadata=meta)
        tree=change_node(tree,goal.node_id,metadata=meta)
        entities=[e for e in self.entities(tree) if e['concept']!='SecurityAlert_Title']
        if not entities:return replace(tree,next_task_id=None)
        before=getattr(self.backend,"query_count",0)
        titles,events=available_events(self.backend,entities)
        self.metadata_queries+=getattr(self.backend,"query_count",0)-before
        intent=meta['intent'];inputs=meta['inputs']
        self.retriever.progress=[{'task':n.node_id,'known':n.metadata['known'],'wanted':n.metadata['wanted'],
            'status':n.status.value,'output_count':len(n.metadata.get('output_entity_ids',[]))}
            for n,_ in walk(tree) if n.level==3]
        for layer in ('PM','EM','SM'):
            if layer in self.disabled:continue
            candidates=(self.retriever.pm_candidates(intent,entities,titles) if layer=='PM' else
                        self.retriever.step_candidates(entities,events,layer))
            def key(c):return digest([c['id'],sorted(e['value'] for e in entities if e['concept']==c['known'])])
            candidates=[c for c in candidates if key(c) not in meta['attempted']]
            chosen,decision=self.retriever.select(inputs['context'],inputs['question'],intent,entities,candidates)
            self.retrievals.append({'version':tree.version,'layer':layer,'decision':decision,'selected':chosen})
            if not chosen:continue
            meta['phase']='executing'
            meta['attempted'].append(key(chosen))
            ids=[e['id'] for e in entities if e['concept']==chosen['known'] and e['value'] in chosen['values']]
            if layer=='PM':
                p=self.retriever.bundle.pm[chosen['pm_id']]
                route=next(r for r in p['variants'] if r['id']==chosen['route_id'])
                steps=[]
                for i,s in enumerate(route['steps']):
                    if i and s['bindings']['entities'].get('step')!=route['steps'][i-1]['id']:
                        raise ValueError('Unsupported PM binding: route must explicitly consume previous step')
                    eid=s['episode_by_input'][chosen['known']] if i==0 else s['episode_id']
                    steps.append({'id':s['id'],'episode_id':eid,'event_title':chosen['events'][s['bindings']['event_title']['scenario_event_index']]})
                run={'id':f"P{len(meta['procedure_runs'])+1}",'pm_id':p['id'],'route_id':chosen['route_id'],
                    'scenario_id':chosen['scenario_id'],'status':'active','steps':steps,'task_ids':[]}
                spec=steps[0]
                tree=self._task(tree,self.retriever.bundle.em[spec['episode_id']],spec['event_title'],ids,
                                 run_id=run['id'],step=spec['id'],layer=layer)
                run['task_ids'].append(tree.next_task_id);meta['procedure_runs'].append(run)
            else:
                episode=self.retriever.bundle.em[chosen['episode_id']] if layer=='EM' else new_episode(chosen['known'],chosen['wanted'])
                tree=self._task(tree,episode,chosen['event'],ids,layer=layer)
            return change_node(tree,goal.node_id,metadata=meta)
        meta['phase']='blocked_no_match'
        tree=change_node(tree,goal.node_id,metadata=meta)
        return replace(tree,next_task_id=None)

    def sql(self,tree,node):
        self.validate(tree)
        entities={e['id']:e for e in (self.entities(tree) if tree.root_nodes else self.preparation_entities)}
        m=node.metadata
        if m['layer']=='BOOTSTRAP':
            template=('SELECT DISTINCT o.AlertId,o.AlertTitle,o.ObservedAt,o.EntityIndex,o.Concept, '
                'o.GraphValue AS value,o.RawComponents,o.Roles,o.SourceRowHash,o.AlertTitle AS matched_input '
                'FROM LTMEntityEvidence o WHERE o.AlertTitle=%s AND o.Concept IN ('+
                ','.join(['%s']*len(m['output_concepts']))+')')
            params=(m['event_title'],*m['output_concepts'])
            anchors=[entities[eid] for eid in m.get('anchor_entity_ids',[])]
            if anchors:
                template += (' AND EXISTS (SELECT 1 FROM LTMInputEvidence a '
                    'WHERE a.SourceRowHash=o.SourceRowHash AND ('+
                    ' OR '.join('(a.Concept=%s AND a.GraphValue=%s)' for _ in anchors)+'))')
                params += tuple(v for e in anchors for v in (e['concept'],e['value']))
            template += ' ORDER BY o.AlertId,o.SourceRowHash,o.EntityIndex,o.Concept,o.GraphValue'
        else:
            values=sorted({entities[e]['value'] for e in m['input_entity_ids']})
            episode=new_episode(m['known'],m['wanted']) if m['layer']=='SM' else self.retriever.bundle.em[m['episode_id']]
            template=episode['sql'].rsplit('LIMIT 501',1)[0]
            template=template.replace(':event_title','%s').replace(':input_values',','.join(['%s']*len(values)))
            params=(m['event_title'],*values)
        connection=self.backend.db.connection
        converter=connection.converter
        def literal(value):
            encoded=converter.to_mysql(value)
            escaped=converter.escape(encoded,sql_mode=connection.sql_mode)
            quoted=converter.quote(escaped)
            return quoted.decode('utf-8') if isinstance(quoted,(bytes,bytearray)) else str(quoted)
        parts=template.split('%s')
        if len(parts)!=len(params)+1:raise ValueError('SQL binding count mismatch')
        return parts[0]+''.join(literal(v)+p for v,p in zip(params,parts[1:]))

    def observe(self,tree,node,execution,raw_observation):
        """Commit entities only from structured, actual SQL records (not model prose)."""
        if not execution.query_success:return tree
        records=json.loads(raw_observation)
        if not isinstance(records,list):raise ValueError('Expected structured SQL records')
        output_ids=[]
        for row in records:
            if not {'value','AlertId','SourceRowHash','matched_input','EntityIndex'}<=row.keys():raise ValueError('Missing evidence lineage')
            inputs={e['value'] for e in self.entities(tree) if e['id'] in node.metadata['input_entity_ids']}
            if row['matched_input'] not in inputs:raise ValueError('Output has unrelated input')
            if row.get('AlertTitle')!=node.metadata['event_title']:raise ValueError('Output event mismatch')
            concept=node.metadata['wanted']
            value=str(row['value'])
            source={'kind':'query','task_id':node.node_id,'execution_id':execution.execution_id,
                'alert_id':row['AlertId'],'source_row_hash':row['SourceRowHash'],'entity_index':row['EntityIndex'],
                'matched_input':row['matched_input']}
            existing=next((n for n,_ in walk(tree) if n.level==2 and n.metadata['concept']==concept and n.metadata['value']==value),None)
            if existing:
                sources=existing.metadata['sources']
                tree=change_node(tree,existing.node_id,metadata={**existing.metadata,'sources':sources if source in sources else sources+[source]},
                    evidence_refs=tuple(dict.fromkeys((*existing.evidence_refs,execution.execution_id))))
                eid=existing.node_id
            else:
                eid=self.allocate(tree,'E');goal=tree.root_nodes[0]
                entity=TTTNode(node_id=eid,title=f'{concept}: {value}',level=2,status=S.OPEN,evidence_refs=(execution.execution_id,),
                    metadata={'concept':concept,'value':value,'scope':tree.event_id,'sources':[source],
                              'identity_claim':'identifier_observed_only','raw_components':row.get('RawComponents')})
                tree=change_node(tree,goal.node_id,children=(*goal.children,entity))
            if eid not in output_ids:output_ids.append(eid)
        current=next(n for n,_ in walk(tree) if n.node_id==node.node_id)
        tree=change_node(tree,node.node_id,metadata={**current.metadata,'output_entity_ids':output_ids},status=S.DONE)
        return self.validate(tree)

    def finish(self,tree):
        goal=tree.root_nodes[0];meta=deepcopy(goal.metadata)
        meta['phase']='completed'
        nodes={n.node_id:n for n,_ in walk(tree)}
        for run in meta['procedure_runs']:
            if run['status']=='active':
                complete=len(run['task_ids'])==len(run['steps']) and all(nodes[t].status==S.DONE and nodes[t].metadata['output_entity_ids'] for t in run['task_ids'])
                run['status']='completed' if complete else 'stopped_goal_answered'
        tree=change_node(tree,goal.node_id,metadata=meta)
        for n,_ in list(walk(tree)):
            if n.level==3 and n.status==S.TODO:tree=change_node(tree,n.node_id,status=S.NOT_APPLICABLE)
        return replace(tree,next_task_id=None)

    def stop(self,tree,reason):
        if not tree.root_nodes:
            self.preparation_exhausted=True
            self.preparation_node=None
            return tree
        goal=tree.root_nodes[0];meta=deepcopy(goal.metadata)
        meta['phase']=reason
        for run in meta['procedure_runs']:
            if run['status']=='active':run['status']='paused_'+reason
        tree=change_node(tree,goal.node_id,metadata=meta)
        for n,_ in list(walk(tree)):
            if n.level==3 and n.status==S.TODO:tree=change_node(tree,n.node_id,status=S.BLOCKED)
        return replace(tree,next_task_id=None)
