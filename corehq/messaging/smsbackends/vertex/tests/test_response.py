# -*- coding: utf-8 -*-
from django.test import TestCase

from corehq.apps.sms.models import QueuedSMS
from corehq.messaging.smsbackends.vertex.exceptions import VertexBackendException
from corehq.messaging.smsbackends.vertex.models import VertexBackend
from corehq.apps.sms.models import SMS

TEST_SUCCESSFUL_RESPONSE = "570737298-2017_05_27"
TEST_FAILURE_RESPONSE = "ES1001 Authentication Failed (invalid username/password)"
TEST_NON_CODE_MESSAGES = "Account is Expire"
TEST_INCORRECT_NUMBER_RESPONSE = "ES1009 Sorry unable to process request"
RANDOM_ERROR_MESSAGE = "Bond.. James Bond.."


class TestVertexBackendResponseHandling(TestCase):
    def setUp(self):
        self.vertex_backend = VertexBackend()
        self.queued_sms = QueuedSMS()

    def handle_success(self):
        self.vertex_backend.handle_response(self.queued_sms, 200, TEST_SUCCESSFUL_RESPONSE)
        self.assertEqual(self.queued_sms.backend_message_id, TEST_SUCCESSFUL_RESPONSE)
        self.assertFalse(self.queued_sms.error)

    def handle_failure(self):
        self.assertFalse(self.queued_sms.error)
        self.vertex_backend.handle_response(self.queued_sms, 200, TEST_INCORRECT_NUMBER_RESPONSE)
        self.assertEqual(self.queued_sms.system_error_message, SMS.ERROR_INVALID_DESTINATION_NUMBER)
        self.assertTrue(self.queued_sms.error)

        self.queued_sms.error = False
        with self.assertRaisesMessage(VertexBackendException, TEST_NON_CODE_MESSAGES):
            self.vertex_backend.handle_response(self.queued_sms, 200, TEST_NON_CODE_MESSAGES)
        self.assertEqual(self.queued_sms.system_error_message, SMS.ERROR_TOO_MANY_UNSUCCESSFUL_ATTEMPTS)
        self.assertTrue(self.queued_sms.error)

        self.queued_sms.error = False
        with self.assertRaisesMessage(VertexBackendException, TEST_FAILURE_RESPONSE):
            self.vertex_backend.handle_response(self.queued_sms, 200, TEST_FAILURE_RESPONSE)
        self.assertEqual(self.queued_sms.system_error_message, SMS.ERROR_TOO_MANY_UNSUCCESSFUL_ATTEMPTS)
        self.assertTrue(self.queued_sms.error)

        with self.assertRaisesMessage(
                VertexBackendException,
                "Unrecognized response from Vertex gateway with {response_status_code} "
                "status code, response {response_text}".format(
                    response_status_code=200,
                    response_text=RANDOM_ERROR_MESSAGE)
        ):
            self.vertex_backend.handle_response(self.queued_sms, 200, RANDOM_ERROR_MESSAGE)
