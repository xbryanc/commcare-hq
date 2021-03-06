from __future__ import absolute_import
from corehq.apps.smsforms.util import process_sms_form_complete
from touchforms.formplayer.signals import sms_form_complete


def handle_sms_form_complete(sender, session_id, form, **kwargs):
    from corehq.apps.smsforms.models import SQLXFormsSession
    session = SQLXFormsSession.by_session_id(session_id)
    if session:
        process_sms_form_complete(session, form, completed=True)

sms_form_complete.connect(handle_sms_form_complete)
