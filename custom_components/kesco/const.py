DOMAIN = "kesco"

BASE_URL = "https://fatura.kesco-energy.com/api/"
AUTH_URL = f"{BASE_URL}TokenAuth"
INVOICES_URL = f"{BASE_URL}Account/debitor-invoices/"
BALANCE_URL = f"{BASE_URL}Account/debitor-balance/"
NOTIFICATIONS_URL = f"{BASE_URL}Account/get-notifications"

TOKEN_TTL_SECONDS = 604800  # 7 days

CONF_DEBITOR_ID = "debitor_id"
CONF_AGENCY_ID = "agency_id"

DEFAULT_AGENCY_ID = "DPR"
SCAN_INTERVAL_HOURS = 6
