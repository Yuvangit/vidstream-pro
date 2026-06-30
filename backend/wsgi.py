"""
wsgi.py — Gunicorn / Railway / Render entry point

Local run :  python wsgi.py
Production:  gunicorn wsgi:application
"""
from app import app as application

if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT", 5000))
    application.run(host="0.0.0.0", port=port, debug=False)
