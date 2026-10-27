import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
except ImportError:
    pass


def path_setting(name: str, default: str) -> Path:
    path = Path(os.getenv(name, default))
    return path if path.is_absolute() else ROOT / path


BILLING_DB = path_setting('BILLING_DB', 'database/billing.db')
CONTEXT_DB = path_setting('CONTEXT_DB', 'database/context.db')
INVOICE_DIR = path_setting('INVOICE_DIR', 'database/invoices')
AZURE_OPENAI_ENDPOINT = os.getenv('AZURE_OPENAI_ENDPOINT', '').strip().rstrip('/')
AZURE_OPENAI_API_KEY = os.getenv('AZURE_OPENAI_API_KEY', '').strip()
AZURE_OPENAI_DEPLOYMENT = os.getenv('AZURE_OPENAI_DEPLOYMENT', '').strip()
AZURE_OPENAI_MODEL = os.getenv('AZURE_OPENAI_MODEL', '').strip()
AZURE_OPENAI_API_VERSION = os.getenv('AZURE_OPENAI_API_VERSION', '').strip()
AZURE_REASONING_EFFORT = os.getenv('AZURE_OPENAI_REASONING_EFFORT', '').strip().lower()
API_KEY = os.getenv('BILLING_API_KEY', '')
ADMIN_API_KEY = os.getenv('BILLING_ADMIN_API_KEY', '')
CASHIER_API_KEY = os.getenv('BILLING_CASHIER_API_KEY', '')
RETURN_WINDOW_DAYS = os.getenv('RETURN_WINDOW_DAYS', '30').strip()
CASHIER_REFUND_LIMIT = os.getenv('CASHIER_REFUND_LIMIT', '100.00')
CASHIER_REFUND_CURRENCY = os.getenv('CASHIER_REFUND_CURRENCY', 'USD').strip().upper()
SQL_MAX_ROWS = max(1, min(int(os.getenv('SQL_MAX_ROWS', '200')), 1000))
CHAT_TIMEOUT = int(os.getenv('CHAT_TIMEOUT_SECONDS', '180'))
MAX_TOOL_ITERATIONS = max(2, min(int(os.getenv('MAX_TOOL_ITERATIONS', '12')), 20))


AZURE_FIELDS = (
    'AZURE_OPENAI_ENDPOINT', 'AZURE_OPENAI_API_KEY', 'AZURE_OPENAI_DEPLOYMENT',
    'AZURE_OPENAI_MODEL', 'AZURE_OPENAI_API_VERSION',
)


def azure_configuration_error():
    """Describe incomplete local settings without exposing their values or calling Azure."""
    from urllib.parse import urlsplit
    if AZURE_REASONING_EFFORT not in ('', 'none', 'low', 'medium', 'high', 'xhigh'):
        return 'AZURE_OPENAI_REASONING_EFFORT must be blank, none, low, medium, high or xhigh.'
    missing = [name for name in AZURE_FIELDS if not globals()[name]]
    if missing:
        return 'Set these fields in .env and restart the API: ' + ', '.join(missing)
    try:
        endpoint = urlsplit(AZURE_OPENAI_ENDPOINT)
    except ValueError:
        return 'AZURE_OPENAI_ENDPOINT must be a valid HTTPS resource URL.'
    if (endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username
            or endpoint.password or endpoint.query or endpoint.fragment
            or endpoint.path not in ('', '/')):
        return 'AZURE_OPENAI_ENDPOINT must be an HTTPS resource URL without a path, query or credentials.'
    return None
