"""Client-facing office telephone numbers, separate from the WhatsApp sender."""
from app.services.franchise_routing import is_northcliff


def branch_contact(subject=None):
    campaign=getattr(subject,'campaign',None)
    policy=getattr(subject,'policy',None)
    company=getattr(subject,'company',None)
    branch=(getattr(subject,'branch',None) or getattr(campaign,'branch',None)
            or getattr(company,'branch',None) or getattr(policy,'branch',None) or '')
    if is_northcliff(branch):
        return {'name':'Northcliff','phone':'010 448 0921','tel':'+27104480921'}
    return {'name':'Brokers','phone':'011 412 4180','tel':'+27114124180'}


def contact_line(subject):
    office=branch_contact(subject)
    return f"Call {office['name']}: {office['phone']}"
