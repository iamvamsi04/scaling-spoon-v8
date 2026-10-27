import os
import uvicorn
from config import settings

if __name__ == "__main__":
    host = os.getenv("API_HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1") and not settings.API_KEY:
        raise SystemExit("Set BILLING_API_KEY before listening outside localhost.")
    uvicorn.run("api.main:app", host=host, port=int(os.getenv("API_PORT", "8000")), workers=1)
