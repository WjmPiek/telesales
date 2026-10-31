"""A requested headcount always includes the principal member."""
import json
from werkzeug.datastructures import MultiDict
from app.services.member_benefits import member_limits

RELATIONSHIPS=['Spouse','Child','Daughter','Son','Parent','Mother','Father','Sibling','Grandparent','Other']


def member_form(application):
    saved=json.loads(application.product_dependents_json or '[]')
    if not saved:
        saved=json.loads(application.dependents_json or '[]')+json.loads(application.extended_family_json or '[]')
        if application.spouse_first_names or application.spouse_surname:
            saved.insert(0,dict(full_name=' '.join(filter(None,[application.spouse_first_names,application.spouse_surname])),
                relationship='Spouse',id_or_dob=application.spouse_id_number or application.spouse_date_of_birth))
    requested=max(1,int(application.total_members or len(saved)+1))
    count=max(requested-1,len(saved))
    return count,{f'covered_{i}_{key}':value for i,row in enumerate(saved,1) for key,value in row.items()}


def adapt_answers(application,form):
    if 'covered_count' not in form:
        return form
    expected,_=member_form(application)
    try: count=int(form.get('covered_count',''))
    except ValueError: raise ValueError('Reload the member list and try again.')
    if count!=expected or count>30: raise ValueError('The member count changed. Reload the application before continuing.')
    values=MultiDict(form)
    for field in list(values):
        if any(field.startswith(prefix) for prefix in ['spouse_','child_','extended_','productdep_']):
            values.poplist(field)
    limits=member_limits(application.product)
    counters={kind:0 for kind in ['spouse','child','extended','productdep']}
    for i in range(1,count+1):
        row={key:(form.get(f'covered_{i}_{key}') or '').strip() for key in ['full_name','relationship','id_or_dob']}
        if not any(row.values()): continue
        if not all(row.values()): raise ValueError('Complete the name, relationship and ID or date of birth for each added person.')
        if row['relationship'] not in RELATIONSHIPS: raise ValueError('Select a valid relationship for each person.')
        kind=('productdep' if limits['plan_type']=='member_product' else
              'spouse' if row['relationship']=='Spouse' else
              'child' if row['relationship'] in {'Child','Daughter','Son'} else 'extended')
        counters[kind]+=1
        if counters[kind]>limits[kind]: raise ValueError(f'This product does not have another {kind} space. Check the relationship or ask your agent.')
        for key,value in row.items(): values[f'{kind}_{counters[kind]}_{key}']=value
    return values
