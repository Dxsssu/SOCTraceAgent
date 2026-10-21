"""Typed interpretation of official train paths; no workflow integration."""
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent
TRAIN = Path('data/excytin-bench/huggingface/questions/train')
GRAPHS = Path('data/excytin-bench/github/secgym/qagen/graph_files')
# Concept definitions preserve official identifier namespaces, including derived identifiers.
DEFINITIONS = {
 ('account','Name'): ('Account_Name','账户Name属性',['Name'],'identity property; may be display name, not necessarily login name'),
 ('account','Sid'): ('Account_SID','账户SID',['Sid'],'SID namespace, not UPN or AAD ID'),
 ('account','AadUserId'): ('Account_AAD_ID','AAD账户对象ID',['AadUserId'],'tenant-scoped directory object ID'),
 ('account','Email'): ('Account_Derived_Email','账户派生邮件标识',['Name','UPNSuffix'],'official Name@UPNSuffix derivation; not verified UserPrincipalName'),
 ('host','HostName'): ('Host_Name','主机名',['HostName'],'short names can collide; exact full name when known'),
 ('host','AadDeviceId'): ('Device_AAD_ID','AAD设备ID',['AadDeviceId'],'not Defender DeviceId'),
 ('process','ProcessId__CreatedTimeUtc__CommandLine'): ('Process_Identity','进程复合标识',['ProcessId','CreatedTimeUtc','CommandLine'],'PID+created time+command; host scope still required; not globally unique'),
 ('process','ExtractedFileName'): ('Command_File_Mention','命令行文件名线索',['CommandLine'],'file mention extracted by upstream regex; not necessarily executable image'),
 ('ip','Address'): ('IP_Address','告警IP实体',['Address'],'source/destination role must come from evidence, not IP value'),
 ('file','Name'): ('File_Name','文件名',['Name'],'weak identifier, not a file hash or unique file'),
 ('url','Url'): ('URL','URL',['Url'],'retain original URL; official graph lowercases and can lose path case'),
 ('mailbox','MailboxPrimaryAddress'): ('Mailbox_Address','邮箱主地址',['MailboxPrimaryAddress'],'mailbox identity, not necessarily sender'),
 ('mailmessage','Recipient'): ('Mail_Recipient','邮件收件人',['Recipient'],'recipient is not click actor'),
 ('mailmessage','Sender'): ('Mail_Sender','邮件Sender属性',['Sender'],'do not conflate P1Sender/P2Sender'),
 ('mailmessage','SenderIP'): ('Mail_Sender_IP','邮件发送IP',['SenderIP'],'not generic remote IP'),
 ('cloud-application','AppId'): ('Cloud_App_ID','云应用AppId',['AppId'],'not OAuth object ID'),
 ('cloud-application','Name__InstanceName'): ('Cloud_App_Instance','云应用名称及实例',['Name','InstanceName'],'composite namespace'),
 ('oauth-application','OAuthObjectId'): ('OAuth_Object_ID','OAuth对象ID',['OAuthObjectId'],'not OAuth application ID'),
 ('oauth-application','OAuthAppId'): ('OAuth_App_ID','OAuth应用ID',['OAuthAppId'],'not OAuth object ID'),
 ('azure-resource','ResourceId'): ('Azure_Resource_ID','Azure资源ID',['ResourceId'],'full Azure resource identifier'),
 ('azure-resource','SubscriptionId'): ('Azure_Subscription_ID','Azure订阅ID',['ResourceId'],'derive from /subscriptions/{id}; not resource ID'),
 ('azure-resource','ResourceGroup'): ('Azure_Resource_Group','Azure资源组',['ResourceId'],'derive from resourcegroups segment; needs subscription scope'),
}


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    tmp.replace(path)


def concept(node):
    return DEFINITIONS[(node['node_type'],node['identifier_fields'])][0]


def sm_definition(node):
    key=(node['node_type'],node['identifier_fields'])
    cid,label,fields,caution=DEFINITIONS[key]
    derived=key[1] in ('Email','ProcessId__CreatedTimeUtc__CommandLine','ExtractedFileName','Name__InstanceName','SubscriptionId','ResourceGroup')
    return {'id':cid,'name':label,'kind':cid,'source_entity_type':key[0],
      'identifier_fields':key[1], 'mapping':{'table':'SecurityAlert','field':'Entities','storage_type':'TEXT',
        'json_entity_type':key[0],'json_paths':['$[*].'+f for f in fields],
        'extraction':'typed postprocessor; derived' if derived else 'typed postprocessor; direct'},
      'normalization': 'preserve raw_components; graph_value follows official identifier rules only for graph replay',
      'cautions':[caution,'Entity co-occurrence proves association only, not causality or actor identity.']}


def extract(payload):
    """Strict JSON only; mimic the documented upstream graph identifiers, retaining raw fields.

    Never repair malformed source data or invent values. Only top-level entities count,
    as in the official graph builder. Graph IDs are NOT database IDs.
    """
    entities=json.loads(payload) if isinstance(payload,str) else payload
    if not isinstance(entities,list): raise ValueError('Entities must be JSON array')
    result=[]
    for index,e in enumerate(entities):
        if not isinstance(e,dict): continue
        typ=str(e.get('Type','')).lower()
        for (etype,field),(cid,label,fields,caution) in DEFINITIONS.items():
            if typ!=etype: continue
            if any(f not in e or e[f] is None for f in fields): continue
            raw={f:e[f] for f in fields}
            values=[]
            if field=='Email':
                if e['Name'] not in ('root','system','guest','admin','administrator','user'):
                    values=[str(e['Name'])+'@'+str(e['UPNSuffix'])]
            elif field=='ExtractedFileName':
                values=re.findall(r'''([^\\/\s"']*\.(?:exe|pdf|dll|xlsx|docx|zip|png|txt|ps1|html|png|tmp))''',str(e['CommandLine']).lower())
            elif '__' in field:
                values=['__'.join(str(e[f]).lower() for f in fields)]
            elif field=='SubscriptionId':
                values=re.findall(r'(?<=subscriptions/)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',str(e['ResourceId']).lower())[:1]
            elif field=='ResourceGroup':
                values=re.findall(r'resourcegroups/([^/]*)/',str(e['ResourceId']).lower())[:1]
            else:
                value=str(e[fields[0]])
                # Upstream preserves these secondary identifiers; primary identifiers are lowercased.
                preserve=(etype,field) in {('account','Name'),('account','Sid'),('host','HostName'),('mailmessage','Recipient'),('mailmessage','SenderIP'),('oauth-application','OAuthAppId')}
                if field=='Name' and typ=='account' and value in ('root','system','guest','admin','administrator','user'):continue
                if field=='Sid' and value=='S-1-5-18':continue
                if field in ('Address','SenderIP') and value in ('0.0.0.0','127.0.0.1','8.8.8.8'):continue
                values=[value if preserve else value.lower()]
            for value in dict.fromkeys(values):
                if value: result.append({'concept':cid,'graph_value':value,'raw_components':raw,
                    'entity_index':index,'entity_ref':e.get('$id'), 'roles':e.get('Roles',[])})
    return result


SQL = """SELECT DISTINCT o.AlertId, o.AlertTitle, o.ObservedAt, o.EntityIndex,
       o.GraphValue AS value, o.RawComponents, o.Roles, o.SourceRowHash,
       i.GraphValue AS matched_input
FROM LTMInputEvidence i
JOIN LTMEntityEvidence o ON o.SourceRowHash = i.SourceRowHash
WHERE i.AlertTitle = :event_title
  AND i.Concept = '{known}' AND o.Concept = '{wanted}'
  AND i.GraphValue IN (:input_values)
ORDER BY o.AlertId, o.SourceRowHash, o.EntityIndex, o.GraphValue, i.GraphValue
LIMIT 501"""


def new_episode(known,wanted):
    valid={d[0] for d in DEFINITIONS.values()}
    if known not in valid or wanted not in valid:
        raise ValueError('Unknown entity concept')
    signature={'known':known,'wanted':wanted,'relation':'same_security_alert_entity_cooccurrence',
               'query':'LTMEntityEvidence.TypedSQL.v2','binding':'all_candidates_with_provenance'}
    return {'id':'em_'+digest(signature)[:16], 'signature':signature,
      'name':f'{known} → {wanted}（同事件关联）',
      'known':{'entities':{'concept':known,'kind':known,'cardinality':'many'},
               'event_title':{'concept':'SecurityAlert_Title','kind':'SecurityAlert_Title','cardinality':'one'}},
      'wanted':{'entities':{'concept':wanted,'kind':wanted,'cardinality':'many'}},
      'relation':'筛选指定事件主题，确认输入实体在同一告警中，再返回该告警的目标类型实体；不宣称二者为同一对象或直接因果。',
      'sql':SQL.format(known=known,wanted=wanted),'parameters':['event_title','input_values'],'row_limit':500,
      'postprocessor':{'decoder':'strict JSON from CSV-normalized SQL columns','input_match':'SQL filters concept + graph_value; preserve raw_components and entity_index',
                       'output':'only wanted entity records + SystemAlertId + TimeGenerated + matched input provenance',
                       'malformed_json':'stop and mark blocked; never silently skip','truncated':'paginate the immutable projection; never silently discard'},
      'cautions':['图规范化值不等同原始值；正式部署需检查URL大小写等语义。',
                  '命令行文件名、账户名等弱连接必须继续保留主机/时间/告警范围，不取第一条候选。',
                  '查询临时LTMEntityEvidence投影；从原始CSV按正确引号规则解析，当前SQL只返回所需字段。'],
      'source_refs':[]}


def step_query(rows,known,wanted,inputs):
    if len(rows)>500: return {'status':'truncated','outputs':[]}
    result=[]
    for row in rows:
        try: entities=extract(row['Entities'])
        except (ValueError,TypeError) as exc:
            return {'status':'invalid_json','outputs':[],'alert_id':row['SystemAlertId'],'error':str(exc)}
        hits=[e for e in entities if e['concept']==known and e['graph_value'] in inputs]
        if hits:
            for e in entities:
                if e['concept']==wanted:
                    result.append({**e,'alert_id':row['SystemAlertId'],'time':str(row.get('TimeGenerated','')),
                                   'matched_inputs':sorted({h['graph_value'] for h in hits})})
    return {'status':'ok' if result else 'empty','outputs':result}


def configure_projection(catalog):
    """Keep newly built cards consistent with the session-local physical backend."""
    semantic=catalog['semantic']
    for key,c in semantic['concepts'].items():
        if 'source_mapping' not in c:c['source_mapping']=c['mapping']
        c['mapping']={'table':'LTMEntityEvidence','field':'AlertTitle' if key=='SecurityAlert_Title' else 'GraphValue',
                      'type_filter':None if key=='SecurityAlert_Title' else key,
                      'raw_field':None if key=='SecurityAlert_Title' else 'RawComponents'}
    for e in semantic['events'].values():
        if 'source_mapping' not in e:e['source_mapping']=e['mapping']
        e['mapping']={'table':'LTMEntityEvidence','field':'AlertTitle','value':e['title']}
    table={'description':'原始日志按CSV标准解码后的会话临时实体投影，来源于SecurityIncident.AlertIds及SecurityAlert。',
      'fields':{'SourceRowHash':'CHAR(64)','AlertId':'VARCHAR(255)','AlertTitle':'TEXT','ObservedAt':'TEXT',
                'Concept':'VARCHAR(64)','GraphValue':'LONGTEXT BINARY COLLATION','RawComponents':'JSON','EntityIndex':'INT','Roles':'JSON'},
      'time_field':'ObservedAt','join_key':'SourceRowHash','scope':'temporary table per connection; source tables untouched'}
    semantic['tables']['LTMEntityEvidence']=table
    semantic['tables']['LTMInputEvidence']={**table,'description':'同会话的输入侧实体副本，规避MySQL临时表不能自关联的限制。'}
    semantic['tables']['SecurityIncident']={'description':'事件告警成员关系，仅由原始日志读取。',
       'fields':{'IncidentNumber':'TEXT','AlertIds':'TEXT'},'source':'original global CSV'}
    catalog['physical_revision']=2
