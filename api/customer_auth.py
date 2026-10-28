"""Local customer sign-in. Passwords never pass through the chat agent."""
import argparse
from contextlib import closing
import getpass
import hashlib
import hmac
import re
import secrets
import time

from database.connection import connect, transaction

ITERATIONS = 600000
SESSION_SECONDS = 12 * 60 * 60
LOGIN_ATTEMPTS = 5
LOCK_SECONDS = 3 * 60


def validate_password(password):
    if not isinstance(password, str) or not 10 <= len(password) <= 128 or not password.strip():
        raise ValueError('Use a password of 10 to 128 characters.')


def password_hash(password):
    validate_password(password)
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITERATIONS).hex()
    return f'pbkdf2_sha256${ITERATIONS}${salt}${digest}'


def verify_password(password, encoded):
    try:
        method, rounds, salt, expected = encoded.split('$')
        if method != 'pbkdf2_sha256' or int(rounds) != ITERATIONS:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), ITERATIONS).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, AttributeError):
        return False


def new_session(conn, customer_id):
    now = int(time.time())
    token = secrets.token_urlsafe(32)
    conn.execute('DELETE FROM customer_sessions WHERE expires_at<=?', (now,))
    conn.execute('INSERT INTO customer_sessions(token_hash,customer_id,expires_at) VALUES (?,?,?)',
                 (hashlib.sha256(token.encode()).hexdigest(), customer_id, now + SESSION_SECONDS))
    return {'customer_id': customer_id, 'token': token, 'expires_at': now + SESSION_SECONDS,
            'expires_in': SESSION_SECONDS}


def register(customer_id, name, password, company=None, email=None):
    if not isinstance(customer_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,50}', customer_id):
        raise ValueError('Use a customer ID with letters, numbers, underscores or hyphens.')
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise ValueError('Provide your name, up to 200 characters.')
    for value in (company, email):
        if value is not None and (not isinstance(value, str) or len(value) > 200):
            raise ValueError('Company and email must be text of at most 200 characters.')
    encoded = password_hash(password)
    with transaction() as conn:
        # Existing records require local onboarding. Knowing an ID or email is not proof of ownership.
        if conn.execute('SELECT 1 FROM customers WHERE customer_id=?', (customer_id,)).fetchone():
            raise ValueError('Unable to create this account. Sign in or contact store staff.')
        conn.execute('INSERT INTO customers(customer_id,name,company,email) VALUES (?,?,?,?)',
                     (customer_id, name.strip(), company, email))
        conn.execute('INSERT INTO customer_credentials(customer_id,password_hash) VALUES (?,?)',
                     (customer_id, encoded))
        return new_session(conn, customer_id)


def set_password(customer_id, password):
    """Trusted local onboarding/reset only; deliberately has no HTTP or model tool route."""
    encoded = password_hash(password)
    with transaction() as conn:
        if not conn.execute('SELECT 1 FROM customers WHERE customer_id=?', (customer_id,)).fetchone():
            raise ValueError('Customer not found.')
        conn.execute('INSERT INTO customer_credentials(customer_id,password_hash) VALUES (?,?) '
                     'ON CONFLICT(customer_id) DO UPDATE SET password_hash=excluded.password_hash, '
                     'failed_attempts=0,locked_until=0', (customer_id, encoded))
        conn.execute('DELETE FROM customer_sessions WHERE customer_id=?', (customer_id,))


def login(customer_id, password):
    validate_password(password)
    with closing(connect(readonly=True)) as conn:
        record = conn.execute('SELECT * FROM customer_credentials WHERE customer_id=?', (customer_id,)).fetchone()
    # Also perform a hash for unknown users, so an ID is not disclosed by a quick rejection.
    encoded = record['password_hash'] if record else f'pbkdf2_sha256${ITERATIONS}${"00" * 16}${"00" * 32}'
    matched = verify_password(password, encoded)
    now = int(time.time())
    with transaction() as conn:
        current = conn.execute('SELECT * FROM customer_credentials WHERE customer_id=?', (customer_id,)).fetchone()
        if not current or not record or current['password_hash'] != encoded or current['locked_until'] > now:
            return None
        if not matched:
            failures = current['failed_attempts'] + 1 if current['locked_until'] == 0 else 1
            locked = now + LOCK_SECONDS if failures >= LOGIN_ATTEMPTS else 0
            conn.execute('UPDATE customer_credentials SET failed_attempts=?,locked_until=? WHERE customer_id=?',
                         (failures, locked, customer_id))
            return None
        conn.execute('UPDATE customer_credentials SET failed_attempts=0,locked_until=0 WHERE customer_id=?', (customer_id,))
        return new_session(conn, customer_id)


def session_for_token(token):
    """The browser sends its original token; the stored digest is never a credential."""
    if not isinstance(token, str) or not 40 <= len(token) <= 128:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = int(time.time())
    with closing(connect(readonly=True)) as conn:
        record = conn.execute("SELECT s.customer_id,s.expires_at FROM customer_sessions s "
                              "JOIN customer_credentials c ON c.customer_id=s.customer_id WHERE s.token_hash=? "
                              "AND typeof(s.expires_at)='integer' AND s.expires_at>?",
                              (digest, now)).fetchone()
        if record:
            return {'customer_id': record['customer_id'], 'expires_at': record['expires_at'],
                    'expires_in': record['expires_at'] - now}
        return None


def customer_for_token(token):
    session = session_for_token(token)
    return session['customer_id'] if session else None


def logout(token):
    if isinstance(token, str) and 40 <= len(token) <= 128:
        with transaction() as conn:
            conn.execute('DELETE FROM customer_sessions WHERE token_hash=?', (hashlib.sha256(token.encode()).hexdigest(),))


def main():
    parser = argparse.ArgumentParser(description='Set a password after independently verifying the customer. Run on the API server.')
    parser.add_argument('--customer-id', required=True)
    args = parser.parse_args()
    password = getpass.getpass('New customer password: ')
    if password != getpass.getpass('Repeat password: '):
        parser.error('Passwords do not match.')
    from database.connection import initialize
    initialize()
    try:
        set_password(args.customer_id, password)
    except ValueError as exc:
        parser.error(str(exc))
    print('Customer password saved. Previous sessions were signed out.')


if __name__ == '__main__':
    main()
