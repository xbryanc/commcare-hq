from __future__ import absolute_import
from functools import wraps

from django.db import transaction
from django.http import (
    Http404,
    HttpResponseRedirect,
)
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.functional import cached_property
from django.utils.translation import ugettext_lazy as _

from corehq import privileges
from corehq.apps.accounting.decorators import requires_privilege_with_fallback
from corehq.apps.domain.models import Domain
from corehq.apps.sms.views import BaseMessagingSectionView
from corehq.apps.hqwebapp.async_handler import AsyncHandlerMixin
from corehq.apps.hqwebapp.decorators import use_datatables, use_select2, use_jquery_ui, use_timepicker
from corehq.apps.hqwebapp.views import DataTablesAJAXPaginationMixin
from corehq.apps.translations.models import StandaloneTranslationDoc
from corehq.apps.users.models import CommCareUser
from corehq.messaging.scheduling.async_handlers import MessagingRecipientHandler
from corehq.messaging.scheduling.forms import ScheduleForm
from corehq.messaging.scheduling.models import (
    AlertSchedule,
    TimedSchedule,
    ImmediateBroadcast,
    ScheduledBroadcast,
    SMSContent,
)
from corehq.messaging.scheduling.exceptions import ImmediateMessageEditAttempt
from corehq.messaging.scheduling.tasks import refresh_alert_schedule_instances, refresh_timed_schedule_instances
from corehq.const import SERVER_DATETIME_FORMAT
from corehq.util.timezones.conversions import ServerTime
from corehq.util.timezones.utils import get_timezone_for_user


def _requires_new_reminder_framework():
    def decorate(fn):
        @wraps(fn)
        def wrapped(request, *args, **kwargs):
            if not hasattr(request, 'project'):
                request.project = Domain.get_by_name(request.domain)
            if request.project.uses_new_reminders:
                return fn(request, *args, **kwargs)
            raise Http404()
        return wrapped
    return decorate


class BroadcastListView(BaseMessagingSectionView, DataTablesAJAXPaginationMixin):
    template_name = 'scheduling/broadcasts_list.html'
    urlname = 'new_list_broadcasts'
    page_title = _('Schedule a Message')

    LIST_SCHEDULED = 'list_scheduled'
    LIST_IMMEDIATE = 'list_immediate'

    @method_decorator(_requires_new_reminder_framework())
    @method_decorator(requires_privilege_with_fallback(privileges.OUTBOUND_SMS))
    @use_datatables
    def dispatch(self, *args, **kwargs):
        return super(BroadcastListView, self).dispatch(*args, **kwargs)

    @cached_property
    def project_timezone(self):
        return get_timezone_for_user(None, self.domain)

    def _format_time(self, time):
        if not time:
            return ''

        user_time = ServerTime(time).user_time(self.project_timezone)
        return user_time.ui_string(SERVER_DATETIME_FORMAT)

    def get_scheduled_ajax_response(self):
        query = (
            ScheduledBroadcast.objects
            .filter(domain=self.domain)
            .order_by('-last_sent_timestamp')
        )
        total_records = query.count()
        query = query.select_related('schedule')
        broadcasts = query[self.display_start:self.display_start + self.display_length]

        data = []
        for broadcast in broadcasts:
            data.append([
                broadcast.id,
                broadcast.name,
                self._format_time(broadcast.last_sent_timestamp),
                broadcast.schedule.active,
            ])
        return self.datatables_ajax_response(data, total_records)

    def get_immediate_ajax_response(self):
        query = (
            ImmediateBroadcast.objects
            .filter(domain=self.domain)
            .order_by('-last_sent_timestamp')
        )
        total_records = query.count()
        broadcasts = query[self.display_start:self.display_start + self.display_length]

        data = []
        for broadcast in broadcasts:
            data.append([
                broadcast.name,
                self._format_time(broadcast.last_sent_timestamp),
                broadcast.id,
            ])
        return self.datatables_ajax_response(data, total_records)

    def get(self, request, *args, **kwargs):
        action = request.GET.get('action')
        if action == self.LIST_SCHEDULED:
            return self.get_scheduled_ajax_response()
        elif action == self.LIST_IMMEDIATE:
            return self.get_immediate_ajax_response()

        return super(BroadcastListView, self).get(*args, **kwargs)


class CreateScheduleView(BaseMessagingSectionView, AsyncHandlerMixin):
    urlname = 'create_schedule'
    page_title = _('Schedule a Message')
    template_name = 'scheduling/create_schedule.html'
    async_handlers = [MessagingRecipientHandler]

    @method_decorator(_requires_new_reminder_framework())
    @method_decorator(requires_privilege_with_fallback(privileges.OUTBOUND_SMS))
    @use_jquery_ui
    @use_timepicker
    @use_select2
    def dispatch(self, *args, **kwargs):
        return super(CreateScheduleView, self).dispatch(*args, **kwargs)

    @cached_property
    def project_timezone(self):
        return get_timezone_for_user(None, self.domain)

    @property
    def is_editing(self):
        return False

    @property
    def parent_pages(self):
        return [
            {
                'title': BroadcastListView.page_title,
                'url': reverse(BroadcastListView.urlname, args=[self.domain]),
            },
        ]

    @property
    def form_kwargs(self):
        return {
            'domain': self.domain,
        }

    @cached_property
    def schedule_form(self):
        if self.request.method == 'POST':
            return ScheduleForm(self.request.POST, **self.form_kwargs)
        return ScheduleForm(**self.form_kwargs)

    @property
    def page_context(self):
        return {
            'form': self.schedule_form,
        }

    @cached_property
    def project_languages(self):
        doc = StandaloneTranslationDoc.get_obj(self.domain, 'sms')
        return getattr(doc, 'langs', ['en'])

    def enforce_edit_restriction(self, send_frequency):
        if (
            self.is_editing and
            (send_frequency == ScheduleForm.SEND_IMMEDIATELY or isinstance(self.broadcast, ImmediateBroadcast))
        ):
            raise ImmediateMessageEditAttempt("Cannot edit an immediate message")

    def distill_content(self):
        form_data = self.schedule_form.cleaned_data
        if form_data['translate']:
            messages = {}
            for lang in self.project_languages:
                key = 'message_%s' % lang
                if key in form_data:
                    messages[lang] = form_data[key]
            content = SMSContent(message=messages)
        else:
            content = SMSContent(message={'*': form_data['non_translated_message']})

        return content

    def distill_recipients(self):
        form_data = self.schedule_form.cleaned_data
        return [('CommCareUser', r_id) for r_id in form_data['recipients']]

    def process_immediate_schedule(self, content, recipients):
        form_data = self.schedule_form.cleaned_data
        with transaction.atomic():
            schedule = AlertSchedule.create_simple_alert(self.domain, content)
            broadcast = ImmediateBroadcast(
                domain=self.domain,
                name=form_data['schedule_name'],
                schedule=schedule,
                recipients=recipients,
            )
            broadcast.save()
        refresh_alert_schedule_instances.delay(schedule, recipients)

    def process_daily_schedule(self, content, recipients):
        form_data = self.schedule_form.cleaned_data
        with transaction.atomic():
            if form_data['stop_type'] == ScheduleForm.STOP_NEVER:
                total_iterations = TimedSchedule.REPEAT_INDEFINITELY
            else:
                total_iterations = form_data['occurrences']

            if self.is_editing:
                broadcast = self.broadcast
                schedule = broadcast.schedule
                schedule.set_simple_daily_schedule(
                    form_data['send_time'],
                    content,
                    total_iterations=total_iterations
                )
            else:
                schedule = TimedSchedule.create_simple_daily_schedule(
                    self.domain,
                    form_data['send_time'],
                    content,
                    total_iterations=total_iterations
                )
                broadcast = ScheduledBroadcast(
                    domain=self.domain,
                    schedule=schedule,
                )

            broadcast.name = form_data['schedule_name']
            broadcast.start_date = form_data['start_date']
            broadcast.recipients = recipients
            broadcast.save()
        refresh_timed_schedule_instances.delay(schedule, recipients, start_date=form_data['start_date'])

    def post(self, request, *args, **kwargs):
        if self.async_response is not None:
            return self.async_response

        if self.schedule_form.is_valid():
            form_data = self.schedule_form.cleaned_data
            self.enforce_edit_restriction(form_data['send_frequency'])
            content = self.distill_content()
            recipients = self.distill_recipients()

            if form_data['send_frequency'] == ScheduleForm.SEND_IMMEDIATELY:
                self.process_immediate_schedule(content, recipients)
            elif form_data['send_frequency'] == ScheduleForm.SEND_DAILY:
                self.process_daily_schedule(content, recipients)
            return HttpResponseRedirect(reverse(BroadcastListView.urlname, args=[self.domain]))

        return self.get(request, *args, **kwargs)


class EditScheduleView(CreateScheduleView):
    urlname = 'edit_schedule'
    page_title = _('Edit Scheduled Message')

    @property
    def page_url(self):
        return reverse(self.urlname, args=[self.domain, self.broadcast_id])

    @property
    def is_editing(self):
        return True

    @property
    def broadcast_id(self):
        return self.kwargs.get('broadcast_id')

    @cached_property
    def broadcast(self):
        try:
            broadcast = ImmediateBroadcast.objects.prefetch_related('schedule').get(pk=self.broadcast_id)
        except:
            raise Http404()

        if broadcast.domain != self.domain:
            raise Http404()

        return broadcast

    @cached_property
    def schedule_form(self):
        if self.request.method == 'POST':
            return ScheduleForm(self.request.POST, **self.form_kwargs)

        broadcast = self.broadcast
        schedule = broadcast.schedule
        recipients = []
        for doc_type, doc_id in broadcast.recipients:
            user = CommCareUser.get_by_user_id(doc_id, domain=self.domain)
            recipients.append({"id": doc_id, "text": user.raw_username})
        initial = {
            'schedule_name': broadcast.name,
            'send_frequency': ScheduleForm.SEND_IMMEDIATELY,
            'recipients': recipients,
            'content': 'sms',
            # only works for SMS
            'message': schedule.memoized_events[0].content.message,
        }
        return ScheduleForm(initial=initial, **self.form_kwargs)
