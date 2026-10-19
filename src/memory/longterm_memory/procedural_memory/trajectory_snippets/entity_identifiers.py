"""Typed identifiers from observed SecurityAlert entity JSON; never from an answer key."""
import json
import re


def decode_entities(raw):
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError('dependency_entity_json_invalid') from exc


def entity_identifier_values(raw, entity_type, identifier_fields):
    data = decode_entities(raw)
    roots = data if isinstance(data, list) else [data]
    index = {}
    def collect(value):
        if isinstance(value, dict):
            if '$id' in value:
                key = str(value['$id'])
                if key in index and index[key] != value:
                    raise ValueError('dependency_entity_duplicate_id')
                index[key] = value
            for v in value.values(): collect(v)
        elif isinstance(value, list):
            for v in value: collect(v)
    collect(data)
    def resolve(value):
        seen = set()
        while isinstance(value, dict) and '$ref' in value:
            key = str(value['$ref'])
            if key in seen or key not in index:
                raise ValueError('dependency_entity_reference_invalid')
            seen.add(key); value = index[key]
        return value
    values = []
    for value in roots:
        entity = resolve(value)
        if not isinstance(entity, dict) or str(entity.get('Type','')).lower() != entity_type.lower():
            continue
        if identifier_fields == 'ExtractedFileName':
            # Official graph derives filenames mentioned in a command, including script/data files.
            values.extend(re.findall(r'''([^\\/\s"']*\.(?:exe|pdf|dll|xlsx|docx|zip|png|txt|ps1|html|png|tmp))''',
                                     str(entity.get('CommandLine','')).lower()))
        elif entity_type.lower() == 'azure-resource' and identifier_fields in {'ResourceGroup', 'SubscriptionId'}:
            resource = str(entity.get('ResourceId', '')).lower()
            pattern = (r'resourcegroups/([^/]*)/' if identifier_fields == 'ResourceGroup' else
                       r'subscriptions/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})')
            found = re.search(pattern, resource)
            if found:
                values.append(found.group(1))
        elif identifier_fields == 'Email':
            if entity.get('Name') and entity.get('UPNSuffix'):
                values.append(str(entity['Name'])+'@'+str(entity['UPNSuffix']))
        elif '__' in identifier_fields:
            fields = identifier_fields.split('__')
            if all(entity.get(f) not in (None, '') for f in fields):
                values.append('__'.join(str(entity[f]).lower() for f in fields))
        elif entity.get(identifier_fields) not in (None, ''):
            value = entity[identifier_fields]
            if not isinstance(value,(str,int,float)):
                raise ValueError('dependency_entity_identifier_not_scalar')
            values.append(value)
    return list(dict.fromkeys(values))
