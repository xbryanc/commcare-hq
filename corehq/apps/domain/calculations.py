from collections import defaultdict
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta

from django.template.loader import render_to_string
from django.utils.translation import ugettext as _
from corehq.util.dates import iso_string_to_datetime
from corehq.apps.app_manager.models import ApplicationBase
from corehq.apps.users.util import WEIRD_USER_IDS
from corehq.apps.es.sms import SMSES
from corehq.apps.es.forms import FormES
from corehq.apps.hqadmin.reporting.reports import (
    USER_COUNT_UPPER_BOUND,
    get_mobile_users,
)

from dimagi.utils.couch.database import get_db
from corehq.apps.domain.models import Domain
from corehq.apps.reminders.models import CaseReminderHandler
from corehq.apps.reports.util import make_form_couch_key
from corehq.apps.users.models import CouchUser
from corehq.elastic import es_query, ADD_TO_ES_FILTER, ES_URLS
from corehq.pillows.mappings.case_mapping import CASE_INDEX
from corehq.pillows.mappings.xform_mapping import XFORM_INDEX
from dimagi.utils.parsing import json_format_datetime


def num_web_users(domain, *args):
    key = ["active", domain, 'WebUser']
    row = get_db().view('users/by_domain', startkey=key, endkey=key+[{}]).one()
    return row["value"] if row else 0

def num_mobile_users(domain, *args):
    row = get_db().view('users/by_domain', startkey=[domain], endkey=[domain, {}]).one()
    return row["value"] if row else 0

DISPLAY_DATE_FORMAT = '%Y/%m/%d %H:%M:%S'

def active_mobile_users(domain, *args):
    """
    Returns the number of mobile users who have submitted a form or SMS in the
    last 30 days
    """
    now = datetime.utcnow()
    then = (now - timedelta(days=30))

    user_ids = get_mobile_users(domain)

    form_users = {q['term'] for q in (
        FormES()
        .domain(domain)
        .user_facet(size=USER_COUNT_UPPER_BOUND)
        .submitted(gte=then)
        .user_id(user_ids)
        .size(0)
        .run()
        .facets.user.result
    )}

    sms_users = {q['term'] for q in (
        SMSES()
        .user_facet(size=USER_COUNT_UPPER_BOUND)
        .to_commcare_user()
        .domain(domain)
        .received(gte=then)
        .size(0)
        .run()
        .facets.user.result
    )}

    num_users = len(form_users | sms_users)
    return num_users if 'inactive' not in args else len(user_ids) - num_users


def cases(domain, *args):
    row = get_db().view("hqcase/types_by_domain", startkey=[domain], endkey=[domain, {}]).one()
    return row["value"] if row else 0


def cases_in_last(domain, days):
    """
    Returns the number of open cases that have been modified in the last <days> days
    """
    now = datetime.utcnow()
    then = json_format_datetime(now - timedelta(days=int(days)))
    now = json_format_datetime(now)

    q = {"query": {
        "range": {
            "modified_on": {
                "from": then,
                "to": now}}}}
    data = es_query(params={"domain.exact": domain, 'closed': False}, q=q, es_url=CASE_INDEX + '/case/_search', size=1)
    return data['hits']['total'] if data.get('hits') else 0

def inactive_cases_in_last(domain, days):
    """
    Returns the number of open cases that have been modified in the last <days> days
    """
    now = datetime.utcnow()
    then = json_format_datetime(now - timedelta(days=int(days)))
    now = json_format_datetime(now)

    q = {"query":
             {"bool": {
                 "must_not": {
                     "range": {
                         "modified_on": {
                             "from": then,
                             "to": now }}}}}}
    data = es_query(params={"domain.exact": domain, 'closed': False}, q=q, es_url=CASE_INDEX + '/case/_search', size=1)
    return data['hits']['total'] if data.get('hits') else 0

def forms(domain, *args):
    key = make_form_couch_key(domain)
    row = get_db().view("reports_forms/all_forms", startkey=key, endkey=key+[{}]).one()
    return row["value"] if row else 0

def sms(domain, direction):
    key = [domain, "SMSLog", direction]
    row = get_db().view("sms/direction_by_domain",
                        startkey=key,
                        endkey=key + [{}]).one()
    return row["value"] if row else 0


def sms_in_last(domain, days=None):
    query = SMSES().domain(domain).size(0)

    if days:
        query = query.received(date.today() - relativedelta(days=30))

    return query.run().total


def sms_in_last_bool(domain, days=None):
    return sms_in_last(domain, days) > 0


def active(domain, *args):
    now = datetime.utcnow()
    then = json_format_datetime(now - timedelta(days=30))
    now = json_format_datetime(now)

    key = ['submission', domain]
    row = get_db().view(
        "reports_forms/all_forms",
        startkey=key+[then],
        endkey=key+[now],
        limit=1
    ).all()
    return True if row else False

def display_time(row, display=True):
    submission_time = row["key"][2]
    if display:
        return iso_string_to_datetime(submission_time).strftime(DISPLAY_DATE_FORMAT)
    else:
        return submission_time

def first_form_submission(domain, display=True):
    key = make_form_couch_key(domain)
    row = get_db().view(
        "reports_forms/all_forms",
        reduce=False,
        startkey=key,
        endkey=key+[{}],
        limit=1
    ).first()
    return display_time(row, display) if row else "No forms"

def last_form_submission(domain, display=True):
    key = make_form_couch_key(domain)
    row = get_db().view(
        "reports_forms/all_forms",
        reduce=False,
        endkey=key,
        startkey=key+[{}],
        descending=True,
        limit=1
    ).first()
    return display_time(row, display) if row else "No forms"

def has_app(domain, *args):
    return bool(ApplicationBase.get_db().view(
        'app_manager/applications_brief',
        startkey=[domain],
        endkey=[domain, {}],
        limit=1
    ).first())

def app_list(domain, *args):
    domain = Domain.get_by_name(domain)
    apps = domain.applications()
    return render_to_string("domain/partials/app_list.html", {"apps": apps, "domain": domain.name})

def uses_reminders(domain, *args):
    handlers = CaseReminderHandler.get_handlers(domain)
    return len(handlers) > 0

def not_implemented(domain, *args):
    return '<p class="text-error">not implemented</p>'

CALC_ORDER = [
    'num_web_users', 'num_mobile_users', 'forms', 'cases',
    'mobile_users--active', 'mobile_users--inactive', 'active_cases',
    'cases_in_last--30', 'cases_in_last--60', 'cases_in_last--90',
    'cases_in_last--120', 'active', 'first_form_submission',
    'last_form_submission', 'has_app', 'web_users', 'active_apps',
    'uses_reminders', 'sms--I', 'sms--O', 'sms_in_last', 'sms_in_last--30',
    'sms_in_last_bool', 'sms_in_last_bool--30'
]

CALCS = {
    'num_web_users': "# web users",
    'num_mobile_users': "# mobile users",
    'forms': "# forms",
    'sms--I': "# incoming SMS",
    'sms--O': "# outgoing SMS",
    'sms_in_last--30': "# SMS in last 30 days",
    'sms_in_last': "# SMS ever",
    'sms_in_last_bool--30': "used messaging in last 30 days",
    'sms_in_last_bool': "used messaging ever",
    'cases': "# cases",
    'mobile_users--active': "# active mobile users",
    'mobile_users--inactive': "# inactive mobile users",
    'active_cases': "# active cases",
    'cases_in_last--30': "# cases seen last 30 days",
    'cases_in_last--60': "# cases seen last 60 days",
    'cases_in_last--90': "# cases seen last 90 days",
    'cases_in_last--120': "# cases seen last 120 days",
    'active': "Active",
    'first_form_submission': "Date of first form submission",
    'last_form_submission': "Date of last form submission",
    'has_app': "Has App",
    'web_users': "list of web users",
    'active_apps': "list of active apps",
    'uses_reminders': "uses reminders",
}

CALC_FNS = {
    'num_web_users': num_web_users,
    "num_mobile_users": num_mobile_users,
    "forms": forms,
    "sms": sms,
    "sms_in_last": sms_in_last,
    "sms_in_last_bool": sms_in_last_bool,
    "cases": cases,
    "mobile_users": active_mobile_users,
    "active_cases": not_implemented,
    "cases_in_last": cases_in_last,
    "inactive_cases_in_last": inactive_cases_in_last,
    "active": active,
    "first_form_submission": first_form_submission,
    "last_form_submission": last_form_submission,
    "has_app": has_app,
    "web_users": not_implemented,
    "active_apps": app_list,
    'uses_reminders': uses_reminders,
}

def dom_calc(calc_tag, dom, extra_arg=''):
    ans = CALC_FNS[calc_tag](dom, extra_arg) if extra_arg else CALC_FNS[calc_tag](dom)
    if ans is True:
        return _('yes')
    elif ans is False:
        return _('no')
    return ans

def _all_domain_stats():
    webuser_counts = defaultdict(lambda: 0)
    commcare_counts = defaultdict(lambda: 0)
    form_counts = defaultdict(lambda: 0)
    case_counts = defaultdict(lambda: 0)

    for row in get_db().view('users/by_domain', startkey=["active"],
                             endkey=["active", {}], group_level=3).all():
        _, domain, doc_type = row['key']
        value = row['value']
        {
            'WebUser': webuser_counts,
            'CommCareUser': commcare_counts
        }[doc_type][domain] = value

    key = make_form_couch_key(None)
    form_counts.update(dict([(row["key"][1], row["value"]) for row in \
                                get_db().view("reports_forms/all_forms",
                                    group=True,
                                    group_level=2,
                                    startkey=key,
                                    endkey=key+[{}]
                             ).all()]))

    case_counts.update(dict([(row["key"][0], row["value"]) for row in \
                             get_db().view("hqcase/types_by_domain",
                                           group=True,group_level=1).all()]))

    return {"web_users": webuser_counts,
            "commcare_users": commcare_counts,
            "forms": form_counts,
            "cases": case_counts}

ES_CALCED_PROPS = ["cp_n_web_users", "cp_n_active_cc_users", "cp_n_cc_users",
                   "cp_n_active_cases", "cp_n_cases", "cp_n_forms",
                   "cp_first_form", "cp_last_form", "cp_is_active",
                   'cp_has_app', "cp_n_in_sms", "cp_n_out_sms", "cp_n_sms_ever",
                   "cp_n_sms_30_d", "cp_sms_ever", "cp_sms_30_d"]

def total_distinct_users(domains=None):
    """
    Get total number of users who've ever submitted a form.
    """
    query = {"in": {"domain.exact": domains}} if domains is not None else {"match_all": {}}
    q = {
        "query": query,
        "filter": {"and": ADD_TO_ES_FILTER["forms"][:]},
    }

    res = es_query(q=q, facets=["form.meta.userID"], es_url=ES_URLS["forms"], size=0)

    user_ids = reduce(list.__add__, [CouchUser.ids_by_domain(d) for d in domains], [])
    terms = [t.get('term') for t in res["facets"]["form.meta.userID"]["terms"]]
    return len(filter(lambda t: t and t not in WEIRD_USER_IDS and t in user_ids, terms))
