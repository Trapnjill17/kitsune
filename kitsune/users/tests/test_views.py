import json
from textwrap import dedent

import mock
from django.contrib import messages
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.test.client import RequestFactory
from django.test.utils import override_settings
from josepy import jwa, jwk, jws
from kitsune.questions.models import Answer, Question
from kitsune.questions.tests import AnswerFactory, QuestionFactory
from kitsune.sumo.tests import LocalizingClient, TestCase
from kitsune.sumo.urlresolvers import reverse
from kitsune.users.models import (CONTRIBUTOR_GROUP, AccountEvent,
                                  Deactivation, Profile, Setting)
from kitsune.users.tests import GroupFactory, UserFactory, add_permission
from kitsune.users.views import edit_profile
from nose.tools import eq_
from pyquery import PyQuery as pq


class MakeContributorTests(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.client.login(username=self.user.username, password='testpass')
        GroupFactory(name=CONTRIBUTOR_GROUP)
        super(MakeContributorTests, self).setUp()

    def test_make_contributor(self):
        """Test adding a user to the contributor group"""
        eq_(0, self.user.groups.filter(name=CONTRIBUTOR_GROUP).count())

        response = self.client.post(reverse('users.make_contributor',
                                            force_locale=True))
        eq_(302, response.status_code)

        eq_(1, self.user.groups.filter(name=CONTRIBUTOR_GROUP).count())


class UserSettingsTests(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.profile = self.user.profile
        self.client.login(username=self.user.username, password='testpass')
        super(UserSettingsTests, self).setUp()

    def test_create_setting(self):
        url = reverse('users.edit_settings', locale='en-US')
        eq_(Setting.objects.filter(user=self.user).count(), 0)  # No settings
        res = self.client.get(url, follow=True)
        eq_(200, res.status_code)
        res = self.client.post(url, {'forums_watch_new_thread': True},
                               follow=True)
        eq_(200, res.status_code)
        assert Setting.get_for_user(self.user, 'forums_watch_new_thread')


class UserProfileTests(TestCase):
    def setUp(self):
        self.user = UserFactory()
        self.profile = self.user.profile
        self.userrl = reverse('users.profile', args=[self.user.username], locale='en-US')
        super(UserProfileTests, self).setUp()

    def test_ProfileFactory(self):
        res = self.client.get(self.userrl)
        self.assertContains(res, self.user.username)

    def test_profile_redirect(self):
        """Ensure that old profile URL's get redirected."""
        res = self.client.get(reverse('users.profile', args=[self.user.pk],
                                      locale='en-US'))
        eq_(302, res.status_code)

    def test_profile_inactive(self):
        """Inactive users don't have a public profile."""
        self.user.is_active = False
        self.user.save()
        res = self.client.get(self.userrl)
        eq_(404, res.status_code)

    def test_profile_post(self):
        res = self.client.post(self.userrl)
        eq_(405, res.status_code)

    def test_profile_deactivate(self):
        """Test user deactivation"""
        p = UserFactory().profile

        self.client.login(username=self.user.username, password='testpass')
        res = self.client.post(reverse('users.deactivate', locale='en-US'), {'user_id': p.user.id})

        eq_(403, res.status_code)

        add_permission(self.user, Profile, 'deactivate_users')
        res = self.client.post(reverse('users.deactivate', locale='en-US'), {'user_id': p.user.id})

        eq_(302, res.status_code)

        log = Deactivation.objects.get(user_id=p.user_id)
        eq_(log.moderator_id, self.user.id)

        p = Profile.objects.get(user_id=p.user_id)
        assert not p.user.is_active

    def test_deactivate_and_flag_spam(self):
        self.client.login(username=self.user.username, password='testpass')
        add_permission(self.user, Profile, 'deactivate_users')

        # Verify content is flagged as spam when requested.
        u = UserFactory()
        AnswerFactory(creator=u)
        QuestionFactory(creator=u)
        url = reverse('users.deactivate-spam', locale='en-US')
        res = self.client.post(url, {'user_id': u.id})

        eq_(302, res.status_code)
        eq_(1, Question.objects.filter(creator=u, is_spam=True).count())
        eq_(0, Question.objects.filter(creator=u, is_spam=False).count())
        eq_(1, Answer.objects.filter(creator=u, is_spam=True).count())
        eq_(0, Answer.objects.filter(creator=u, is_spam=False).count())


class ProfileNotificationTests(TestCase):
    """
    These tests confirm that FXA and non-FXA messages render properly.
    We use RequestFactory because the request object from self.client.request
    cannot be passed into messages.info()
    """
    def _get_request(self):
        user = UserFactory()
        request = RequestFactory().get(reverse('users.edit_profile', args=[user.username]))
        request.user = user
        request.LANGUAGE_CODE = 'en'

        middleware = SessionMiddleware()
        middleware.process_request(request)
        request.session.save()

        middleware = MessageMiddleware()
        middleware.process_request(request)
        request.session.save()
        return request

    def test_fxa_notification_updated(self):
        request = self._get_request()
        messages.info(request, 'fxa_notification_updated')
        response = edit_profile(request)
        doc = pq(response.content)
        eq_(1, len(doc('#fxa-notification-updated')))
        eq_(0, len(doc('#fxa-notification-created')))

    def test_fxa_notification_created(self):
        request = self._get_request()
        messages.info(request, 'fxa_notification_created')
        response = edit_profile(request)
        doc = pq(response.content)
        eq_(0, len(doc('#fxa-notification-updated')))
        eq_(1, len(doc('#fxa-notification-created')))

    def test_non_fxa_notification_created(self):
        request = self._get_request()
        text = 'This is a helpful piece of information'
        messages.info(request, text)
        response = edit_profile(request)
        doc = pq(response.content)
        eq_(0, len(doc('#fxa-notification-updated')))
        eq_(0, len(doc('#fxa-notification-created')))
        eq_(1, len(doc('.user-messages li')))
        eq_(doc('.user-messages li').text(), text)


class FXAAuthenticationTests(TestCase):
    client_class = LocalizingClient

    def test_authenticate_does_not_update_session(self):
        self.client.get(reverse('users.fxa_authentication_init'))
        assert not self.client.session.get('is_contributor')

    def test_authenticate_does_update_session(self):
        url = reverse('users.fxa_authentication_init') + '?is_contributor=True'
        self.client.get(url)
        assert self.client.session.get('is_contributor')


class WebhookViewTests(TestCase):

    def _setup_key(test):
        @mock.patch('kitsune.users.views.requests')
        def wrapper(self, mock_requests):
            pem = dedent("""
            -----BEGIN RSA PRIVATE KEY-----
            MIIBOgIBAAJBAKx1c7RR7R/drnBSQ/zfx1vQLHUbFLh1AQQQ5R8DZUXd36efNK79
            vukFhN9HFoHZiUvOjm0c+pVE6K+EdE/twuUCAwEAAQJAMbrEnJCrQe8YqAbw1/Bn
            elAzIamndfE3U8bTavf9sgFpS4HL83rhd6PDbvx81ucaJAT/5x048fM/nFl4fzAc
            mQIhAOF/a9o3EIsDKEmUl+Z1OaOiUxDF3kqWSmALEsmvDhwXAiEAw8ljV5RO/rUp
            Zu2YMDFq3MKpyyMgBIJ8CxmGRc6gCmMCIGRQzkcmhfqBrhOFwkmozrqIBRIKJIjj
            8TRm2LXWZZ2DAiAqVO7PztdNpynugUy4jtbGKKjBrTSNBRGA7OHlUgm0dQIhALQq
            6oGU29Vxlvt3k0vmiRKU4AVfLyNXIGtcWcNG46h/
            -----END RSA PRIVATE KEY-----
            """)
            key = jwk.JWKRSA.load(pem)
            pubkey = {
                "kty": "RSA",
                "alg": "RS256"
            }
            pubkey.update(key.public_key().fields_to_partial_json())

            mock_json = mock.Mock()
            mock_json.json.return_value = {
                "keys": [
                    pubkey
                ]
            }
            mock_requests.get.return_value = mock_json
            test(self, key)

        wrapper.__name__ = test.__name__
        return wrapper

    @_setup_key
    @override_settings(FXA_RP_CLIENT_ID="12345")
    @override_settings(FXA_SET_ISSUER="http://example.com")
    def test_adds_event_to_db(self, key):
        events = {
            "https://schemas.accounts.firefox.com/event/subscription-state-change": {
                "capabilities": ["capability_1", "capability_2"],
                "isActive": True,
                "changeTime": 1565721242227
            }
        }
        payload = json.dumps({
            "iss": "http://example.com",
            "sub": "54321",
            "aud": "12345",
            "iat": 1565720808,
            "jti": "e19ed6c5-4816-4171-aa43-56ffe80dbda1",
            "events": events
        })
        signature = jws.JWS.sign(
            payload=payload,
            key=key,
            alg=jwa.RS256,
            protect=frozenset(['alg'])
        )
        jwt = signature.to_compact()

        eq_(0, AccountEvent.objects.count())

        response = self.client.post(
            reverse('users.fxa_webhook'),
            content_type='application/secevent+jwt',
            HTTP_AUTHORIZATION="Bearer " + jwt
        )

        eq_(202, response.status_code)
        eq_(1, AccountEvent.objects.count())

        account_event = AccountEvent.objects.last()
        eq_(account_event.status, AccountEvent.UNPROCESSED)
        eq_(account_event.events, json.dumps(events, sort_keys=True))
        eq_(account_event.event_type, None)
        eq_(account_event.fxa_uid, "54321")
        eq_(account_event.jwt_id, "e19ed6c5-4816-4171-aa43-56ffe80dbda1")
        eq_(account_event.issued_at, "1565720808")
        eq_(account_event.profile, None)
