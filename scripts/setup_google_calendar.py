"""Create/reuse the real Noor bookings calendar and share it with the owner."""
import argparse
from urllib.parse import quote

from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account


def request(session, method, path, **kwargs):
    response = session.request(method, 'https://www.googleapis.com/calendar/v3' + path, timeout=20, **kwargs)
    if not response.ok:
        raise RuntimeError(f'Google Calendar HTTP {response.status_code}: {response.text[:500]}')
    return response


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--credentials', required=True)
    parser.add_argument('--owner-email', required=True)
    parser.add_argument('--summary', default='Noor Dental Bookings')
    args = parser.parse_args()
    credential = service_account.Credentials.from_service_account_file(
        args.credentials, scopes=['https://www.googleapis.com/auth/calendar']
    )
    session = AuthorizedSession(credential)
    calendars = request(session, 'GET', '/users/me/calendarList', params={'maxResults': 250}).json().get('items', [])
    calendar = next((item for item in calendars if item.get('summary') == args.summary), None)
    created = False
    if not calendar:
        calendar = request(session, 'POST', '/calendars', json={
            'summary': args.summary,
            'description': 'Real appointment events created by the Noor voice booking system.',
            'timeZone': 'Asia/Dubai',
        }).json()
        created = True
    calendar_id = calendar['id']
    encoded = quote(calendar_id, safe='')
    rules = request(session, 'GET', f'/calendars/{encoded}/acl').json().get('items', [])
    owner_rule = next((rule for rule in rules if rule.get('scope', {}).get('value') == args.owner_email), None)
    if not owner_rule:
        request(session, 'POST', f'/calendars/{encoded}/acl', params={'sendNotifications': 'false'}, json={
            'role': 'writer', 'scope': {'type': 'user', 'value': args.owner_email},
        })
    print({'calendar_id': calendar_id, 'created': created, 'shared_with': args.owner_email})


if __name__ == '__main__':
    main()
