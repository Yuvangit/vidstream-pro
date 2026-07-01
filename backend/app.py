"""
VidStream Pro — Python/Flask Backend
Powered by yt-dlp · Supabase Auth · Stripe · Railway/Render
"""

import os, uuid, threading, time, json, re
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp

# ── Optional dependencies (graceful fallback) ─────────────────
try:
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    STRIPE_OK = bool(stripe.api_key)
except ImportError:
    stripe = None
    STRIPE_OK = False

try:
    from supabase import create_client, Client as SupabaseClient
    _SB_URL  = os.getenv("SUPABASE_URL", "")
    _SB_KEY  = os.getenv("SUPABASE_SERVICE_KEY", "")  # service role key (server-side only)
    sb: SupabaseClient = create_client(_SB_URL, _SB_KEY) if (_SB_URL and _SB_KEY) else None
except Exception:
    sb = None

# ═══════════════════════════════════════════════════════════════
#  APP SETUP
# ═══════════════════════════════════════════════════════════════
app = Flask(__name__)
CORS(app, origins="*", supports_credentials=True)

# In-memory job store  {job_id: {status, progress, speed, eta, filepath, error}}
JOBS: dict = {}
# In-memory credit store {user_id: {date: str, used: int}}
CREDITS: dict = {}

PLANS = {"guest": 10, "free": 20, "pro": 1000}
PRICES = {
    "monthly": {"amount": 799,  "currency": "usd", "interval": "month"},
    "annual":  {"amount": 5748, "currency": "usd", "interval": "year"},   # $47.99/yr
}

# ── Stripe Price IDs (set these in Railway/Render env vars) ───
# WHERE TO GET THEM:
#   1. dashboard.stripe.com → Product catalog → Add product "VidStream Pro"
#   2. Add price: $7.99/month recurring  → copy Price ID → STRIPE_PRICE_MONTHLY
#   3. Add price: $71.88/year recurring  → copy Price ID → STRIPE_PRICE_ANNUAL
#   Price IDs look like: price_1ABC123def456...
# If not set, checkout creates inline price_data (still works for testing)
STRIPE_PRICE_MONTHLY = os.getenv("STRIPE_PRICE_MONTHLY", "")
STRIPE_PRICE_ANNUAL  = os.getenv("STRIPE_PRICE_ANNUAL",  "")

# ═══════════════════════════════════════════════════════════════
#  AUTH HELPERS
# ═══════════════════════════════════════════════════════════════
def _get_bearer() -> str | None:
    h = request.headers.get("Authorization", "")
    return h[7:] if h.startswith("Bearer ") else None

def _verify_token(token: str) -> dict | None:
    """Verify Supabase JWT and return user dict, or None."""
    if not sb:
        return None
    try:
        resp = sb.auth.get_user(token)
        user = resp.user
        if not user:
            return None
        return {"id": user.id, "email": user.email,
                "name": user.user_metadata.get("full_name") or user.email.split("@")[0],
                "plan": _get_user_plan(user.id)}
    except Exception:
        return None

def _get_user_plan(user_id: str) -> str:
    """Look up user plan from Supabase profiles table."""
    if not sb:
        return "free"
    try:
        r = sb.table("profiles").select("plan").eq("id", user_id).single().execute()
        return r.data.get("plan", "free") if r.data else "free"
    except Exception:
        return "free"

def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = _get_bearer()
        if not token:
            return jsonify({"error": "Authentication required"}), 401
        user = _verify_token(token)
        if not user:
            return jsonify({"error": "Invalid or expired token"}), 401
        request.user = user
        return f(*args, **kwargs)
    return wrapper

def optional_auth(f):
    """Attach user if token present, but don't block."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = _get_bearer()
        request.user = _verify_token(token) if token else None
        return f(*args, **kwargs)
    return wrapper

# ═══════════════════════════════════════════════════════════════
#  CREDIT HELPERS
# ═══════════════════════════════════════════════════════════════
def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def _get_credits(user_id: str, plan: str) -> dict:
    today = _today()
    key   = f"{user_id}:{today}"
    used  = CREDITS.get(key, 0)
    limit = PLANS.get(plan, 10)
    return {"used": used, "remaining": max(0, limit - used), "limit": limit}

def _consume_credit(user_id: str, plan: str) -> bool:
    today = _today()
    key   = f"{user_id}:{today}"
    used  = CREDITS.get(key, 0)
    limit = PLANS.get(plan, 10)
    if used >= limit:
        return False
    CREDITS[key] = used + 1
    return True

# ═══════════════════════════════════════════════════════════════
#  HEALTH
# ═══════════════════════════════════════════════════════════════
@app.route("/api/health")
def health():
    return jsonify({
        "status": "ok",
        "supabase": sb is not None,
        "stripe": STRIPE_OK,
        "version": "1.0.0",
        "time": datetime.now(timezone.utc).isoformat()
    })

# ═══════════════════════════════════════════════════════════════
#  AUTH ROUTES
# ═══════════════════════════════════════════════════════════════
@app.route("/api/auth/me")
@require_auth
def auth_me():
    u = request.user
    credits = _get_credits(u["id"], u["plan"])
    return jsonify({"user": u, "credits": credits, "plan": u["plan"]})

@app.route("/api/auth/refresh", methods=["POST"])
def auth_refresh():
    token = _get_bearer()
    if not token or not sb:
        return jsonify({"error": "No refresh token"}), 401
    try:
        resp = sb.auth.refresh_session(token)
        return jsonify({"access_token": resp.session.access_token,
                        "refresh_token": resp.session.refresh_token})
    except Exception as e:
        return jsonify({"error": str(e)}), 401

# ═══════════════════════════════════════════════════════════════
#  USER CREDITS & HISTORY
# ═══════════════════════════════════════════════════════════════
@app.route("/api/user/credits")
@require_auth
def user_credits():
    u = request.user
    return jsonify({"credits": _get_credits(u["id"], u["plan"]), "plan": u["plan"]})

@app.route("/api/user/history")
@require_auth
def user_history():
    u = request.user
    fmt = request.args.get("format", "all")
    if not sb:
        return jsonify({"items": []})
    try:
        q = sb.table("download_logs").select("*").eq("user_id", u["id"]).order("created_at", desc=True).limit(50)
        if fmt != "all":
            q = q.eq("format", fmt)
        r = q.execute()
        return jsonify({"items": r.data or []})
    except Exception as e:
        return jsonify({"error": str(e), "items": []}), 500

@app.route("/api/user/history", methods=["DELETE"])
@require_auth
def clear_history():
    u = request.user
    if sb:
        try:
            sb.table("download_logs").delete().eq("user_id", u["id"]).execute()
        except Exception:
            pass
    return jsonify({"success": True})

# ═══════════════════════════════════════════════════════════════
#  VIDEO INFO  (yt-dlp)
# ═══════════════════════════════════════════════════════════════
@app.route("/api/video/info", methods=["POST"])
@optional_auth
def video_info():
    data = request.get_json(silent=True) or {}
    url  = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    # ── Rate-limit guests ──────────────────────────────────────
    user   = getattr(request, "user", None)
    plan   = user["plan"] if user else "guest"
    uid    = user["id"]   if user else request.remote_addr

    if not _consume_credit(uid, plan):
        credits = _get_credits(uid, plan)
        return jsonify({"error": "Daily credit limit reached", "credits": credits}), 429

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "bestvideo+bestaudio/best",
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = _parse_formats(info.get("formats", []))
        log_id  = _log_request(user, url, info.get("title",""), info.get("extractor",""))

        payload = {
            "title":       info.get("title", ""),
            "thumbnail":   info.get("thumbnail", ""),
            "duration":    info.get("duration", 0),
            "uploader":    info.get("uploader", info.get("channel", "")),
            "view_count":  info.get("view_count", 0),
            "like_count":  info.get("like_count", 0),
            "upload_date": info.get("upload_date", ""),
            "description": (info.get("description") or "")[:300],
            "platform":    info.get("extractor_key", "Unknown"),
            "webpage_url": info.get("webpage_url", url),
            "formats":     formats,
            "has_subtitles": bool(info.get("subtitles")),
        }
        credits = _get_credits(uid, plan)
        return jsonify({"success": True, "data": payload, "log_id": log_id, "credits": credits})

    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": f"Could not fetch video: {str(e)[:200]}"}), 422
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500

def _parse_formats(raw: list) -> list:
    seen, out = set(), []
    for f in raw:
        fid = f.get("format_id", "")
        if fid in seen:
            continue
        seen.add(fid)
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        h = f.get("height") or 0
        abr = f.get("abr") or 0

        if vcodec != "none" and h >= 144:
            quality = f"{h}p"
            if h >= 2160: quality = "4K UHD"
            elif h >= 1440: quality = "2K QHD"
            elif h == 1080: quality = "1080p FHD"
            elif h == 720:  quality = "720p HD"
            out.append({"format_id": fid, "type": "video", "quality": quality,
                        "height": h, "ext": f.get("ext","mp4"),
                        "filesize_mb": round((f.get("filesize") or 0) / 1048576, 1),
                        "recommended": h == 1080})
        elif vcodec == "none" and acodec != "none":
            quality = f"{int(abr)}kbps" if abr else "audio"
            out.append({"format_id": fid, "type": "audio", "quality": quality,
                        "abr": int(abr), "ext": f.get("ext","m4a"),
                        "filesize_mb": round((f.get("filesize") or 0) / 1048576, 1),
                        "recommended": 128 <= abr <= 192})

    out.sort(key=lambda x: (x["type"] == "audio", -(x.get("height") or x.get("abr") or 0)))
    return out[:20]

def _log_request(user, url, title, platform) -> str | None:
    if not sb or not user:
        return None
    try:
        r = sb.table("download_logs").insert({
            "user_id": user["id"],
            "url": url,
            "title": title,
            "platform": platform,
            "created_at": datetime.now(timezone.utc).isoformat()
        }).execute()
        return r.data[0]["id"] if r.data else None
    except Exception:
        return None

# ═══════════════════════════════════════════════════════════════
#  DOWNLOAD ENGINE  (background thread + SSE progress)
# ═══════════════════════════════════════════════════════════════
@app.route("/api/download/start", methods=["POST"])
@optional_auth
def download_start():
    data      = request.get_json(silent=True) or {}
    url       = (data.get("url") or "").strip()
    format_id = data.get("format_id", "bestvideo+bestaudio/best")
    if not url:
        return jsonify({"error": "URL is required"}), 400

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"status": "pending", "progress": 0, "speed": "", "eta": "", "filepath": None, "error": None}

    thread = threading.Thread(target=_run_download, args=(job_id, url, format_id), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id, "status": "started"})

def _run_download(job_id: str, url: str, format_id: str):
    out_dir = "/tmp/vidstream"
    os.makedirs(out_dir, exist_ok=True)
    out_tmpl = os.path.join(out_dir, f"{job_id}.%(ext)s")

    def _progress_hook(d):
        if d["status"] == "downloading":
            pct = d.get("_percent_str", "0%").strip().replace("%", "")
            try:
                JOBS[job_id]["progress"] = float(pct)
            except ValueError:
                pass
            JOBS[job_id]["speed"] = d.get("_speed_str", "").strip()
            JOBS[job_id]["eta"]   = d.get("_eta_str", "").strip()
            JOBS[job_id]["status"] = "downloading"
        elif d["status"] == "finished":
            JOBS[job_id]["progress"] = 100
            JOBS[job_id]["filepath"] = d.get("filename")
            JOBS[job_id]["status"]   = "done"

    ydl_opts = {
        "format":           format_id,
        "outtmpl":          out_tmpl,
        "progress_hooks":   [_progress_hook],
        "quiet":            True,
        "no_warnings":      True,
        "merge_output_format": "mp4",
        "postprocessors": [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }],
    }

    # Audio-only → convert to mp3
    if "audio" in format_id.lower() or format_id in ("bestaudio", "140", "251", "250"):
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320",
        }]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        JOBS[job_id]["status"] = "done"
        JOBS[job_id]["progress"] = 100
    except Exception as e:
        JOBS[job_id]["status"] = "failed"
        JOBS[job_id]["error"]  = str(e)[:300]

@app.route("/api/download/progress/<job_id>")
def download_progress(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)

@app.route("/api/download/stream")
def download_stream():
    job_id = request.args.get("job_id", "")
    job    = JOBS.get(job_id)
    if not job or not job.get("filepath"):
        return jsonify({"error": "File not ready"}), 404

    filepath = job["filepath"]
    if not os.path.exists(filepath):
        # Try common extensions
        for ext in ["mp4", "mp3", "webm", "m4a"]:
            candidate = os.path.join("/tmp/vidstream", f"{job_id}.{ext}")
            if os.path.exists(candidate):
                filepath = candidate
                break

    if not os.path.exists(filepath):
        return jsonify({"error": "File not found on disk"}), 404

    filename = os.path.basename(filepath)
    ext = filename.rsplit(".", 1)[-1].lower()
    mime = {"mp4": "video/mp4", "mp3": "audio/mpeg", "webm": "video/webm",
            "m4a": "audio/mp4", "ogg": "audio/ogg"}.get(ext, "application/octet-stream")

    def generate():
        with open(filepath, "rb") as f:
            while chunk := f.read(65536):
                yield chunk
        # cleanup after serving
        try:
            os.remove(filepath)
            del JOBS[job_id]
        except Exception:
            pass

    return Response(stream_with_context(generate()), mimetype=mime,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})

# ═══════════════════════════════════════════════════════════════
#  SEARCH (yt-dlp)
# ═══════════════════════════════════════════════════════════════
@app.route("/api/search/")
@optional_auth
def search():
    q           = request.args.get("q", "").strip()
    max_results = min(int(request.args.get("max_results", 10)), 20)
    if not q:
        return jsonify({"error": "Query is required"}), 400

    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                "extract_flat": True, "default_search": f"ytsearch{max_results}"}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(q, download=False)
        items = []
        for entry in (info.get("entries") or []):
            if not entry:
                continue
            items.append({
                "video_id":    entry.get("id", ""),
                "title":       entry.get("title", ""),
                "thumbnail":   entry.get("thumbnail", f"https://i.ytimg.com/vi/{entry.get('id','')}/hqdefault.jpg"),
                "channel":     entry.get("channel") or entry.get("uploader", ""),
                "duration":    entry.get("duration", 0),
                "view_count":  entry.get("view_count", 0),
                "published_at": entry.get("upload_date", ""),
                "url":         entry.get("url") or f"https://www.youtube.com/watch?v={entry.get('id','')}",
            })
        return jsonify({"success": True, "items": items, "query": q})
    except Exception as e:
        return jsonify({"error": str(e)[:200], "items": []}), 500

# ═══════════════════════════════════════════════════════════════
#  TRENDING (yt-dlp)
# ═══════════════════════════════════════════════════════════════
@app.route("/api/trending/")
def trending():
    category = request.args.get("category", "").strip()
    CAT_MAP  = {"music": "music", "gaming": "gaming", "sports": "sports",
                "tech": "science", "news": "news", "comedy": "comedy"}
    yt_cat = CAT_MAP.get(category, "")
    url = f"https://www.youtube.com/feed/trending" + (f"?bp={yt_cat}" if yt_cat else "")

    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True,
                "extract_flat": True, "playlistend": 16}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        items = []
        for entry in (info.get("entries") or []):
            if not entry:
                continue
            vid_id = entry.get("id", "")
            items.append({
                "video_id":   vid_id,
                "title":      entry.get("title", ""),
                "thumbnail":  entry.get("thumbnail", f"https://i.ytimg.com/vi/{vid_id}/hqdefault.jpg"),
                "channel":    entry.get("channel") or entry.get("uploader", ""),
                "view_count": entry.get("view_count", 0),
                "duration":   entry.get("duration", 0),
                "url":        f"https://www.youtube.com/watch?v={vid_id}",
            })
        return jsonify({"success": True, "items": items})
    except Exception as e:
        return jsonify({"error": str(e)[:200], "items": []}), 500

# ═══════════════════════════════════════════════════════════════
#  BILLING (Stripe)
# ═══════════════════════════════════════════════════════════════
@app.route("/api/billing/create-checkout", methods=["POST"])
@require_auth
def billing_checkout():
    if not STRIPE_OK:
        return jsonify({
            "error": "Stripe not configured. Add STRIPE_SECRET_KEY to your env vars."
        }), 503

    data    = request.get_json(silent=True) or {}
    billing = data.get("billing", "monthly")   # "monthly" | "annual"
    user    = request.user

    # ── Resolve Price ID ──────────────────────────────────────
    # Check frontend-sent ID first, then fall back to server env var.
    # Both are set from the same Stripe product — just two ways to pass it.
    frontend_price_id = (data.get("price_id") or "").strip()
    server_price_id   = STRIPE_PRICE_MONTHLY if billing == "monthly" else STRIPE_PRICE_ANNUAL

    price_id = (
        frontend_price_id if (frontend_price_id and frontend_price_id.startswith("price_"))
        else server_price_id if (server_price_id and server_price_id.startswith("price_"))
        else None
    )

    if not price_id:
        # ── Tell the developer exactly what to do ─────────────
        env_key = "STRIPE_PRICE_MONTHLY" if billing == "monthly" else "STRIPE_PRICE_ANNUAL"
        return jsonify({
            "error": (
                f"No Stripe Price ID found for '{billing}' plan. "
                f"Steps to fix: "
                f"1) Go to dashboard.stripe.com → Product catalog → Add product 'VidStream Pro'. "
                f"2) Add a {'monthly' if billing=='monthly' else 'yearly'} recurring price. "
                f"3) Copy the Price ID (starts with price_...). "
                f"4) Add it to Railway/Render env vars as: {env_key}=price_... "
                f"5) Also paste it in index.html CFG.{'STRIPE_PRICE_MONTHLY' if billing=='monthly' else 'STRIPE_PRICE_ANNUAL'}."
            ),
            "fix": {
                "env_var":  f"{env_key}=price_...",
                "cfg_key":  f"CFG.{'STRIPE_PRICE_MONTHLY' if billing=='monthly' else 'STRIPE_PRICE_ANNUAL'}",
                "docs_url": "https://dashboard.stripe.com/products",
            }
        }), 422

    try:
        # ── Create or retrieve Stripe customer ────────────────
        customers = stripe.Customer.list(email=user["email"], limit=1)
        customer  = customers.data[0] if customers.data else stripe.Customer.create(
            email=user["email"],
            metadata={"user_id": user["id"]}
        )

        # ── Create checkout session with Price ID ─────────────
        session = stripe.checkout.Session.create(
            customer=customer.id,
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=data.get("success_url",
                os.getenv("FRONTEND_URL", "http://localhost:3000") + "?payment=success"),
            cancel_url=data.get("cancel_url",
                os.getenv("FRONTEND_URL", "http://localhost:3000") + "?payment=canceled"),
            metadata={"user_id": user["id"], "billing": billing, "price_id": price_id},
        )
        return jsonify({
            "checkout_url": session.url,
            "session_id":   session.id,
            "billing":      billing,
            "price_id":     price_id,
        })

    except stripe.StripeError as e:
        # ── Stripe-specific errors (invalid price ID, card declined, etc.) ──
        err = str(e)
        hint = ""
        if "No such price" in err:
            hint = " — The Price ID does not exist in your Stripe account. Make sure you copied it from the correct Stripe account (test vs live)."
        elif "live mode" in err.lower() or "test mode" in err.lower():
            hint = " — You are mixing test and live mode keys. Use pk_test_ with sk_test_, or pk_live_ with sk_live_."
        return jsonify({"error": err + hint}), 400

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/billing/webhook", methods=["POST"])
def billing_webhook():
    """Stripe sends events here — upgrades user to Pro on payment."""
    if not STRIPE_OK:
        return jsonify({"error": "Stripe not configured"}), 503

    payload  = request.get_data()
    sig      = request.headers.get("Stripe-Signature", "")
    wh_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, wh_secret)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    if event["type"] in ("checkout.session.completed", "invoice.payment_succeeded"):
        obj     = event["data"]["object"]
        user_id = obj.get("metadata", {}).get("user_id")
        if user_id and sb:
            try:
                sb.table("profiles").upsert({
                    "id": user_id,
                    "plan": "pro",
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }).execute()
            except Exception:
                pass

    elif event["type"] in ("customer.subscription.deleted", "invoice.payment_failed"):
        obj     = event["data"]["object"]
        cust_id = obj.get("customer")
        if cust_id and sb:
            try:
                customers = stripe.Customer.retrieve(cust_id)
                user_id   = customers.metadata.get("user_id")
                if user_id:
                    sb.table("profiles").update({"plan": "free"}).eq("id", user_id).execute()
            except Exception:
                pass

    return jsonify({"received": True})

# ═══════════════════════════════════════════════════════════════
#  GLOBAL ERROR HANDLERS — always return JSON, never empty body
# ═══════════════════════════════════════════════════════════════
@app.errorhandler(400)
def bad_request(e):
    return jsonify({"error": "Bad request", "detail": str(e)}), 400

@app.errorhandler(401)
def unauthorized(e):
    return jsonify({"error": "Unauthorized"}), 401

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Endpoint not found"}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "Method not allowed"}), 405

@app.errorhandler(500)
def internal_error(e):
    import traceback
    return jsonify({"error": "Internal server error", "detail": str(e),
                    "trace": traceback.format_exc()[-500:]}), 500

@app.errorhandler(Exception)
def unhandled(e):
    import traceback
    print("UNHANDLED EXCEPTION:", traceback.format_exc())
    return jsonify({"error": str(e)[:300]}), 500

@app.after_request
def add_cors_and_json(response):
    """Force JSON content-type on all /api/ routes so browsers never get empty HTML."""
    if request.path.startswith("/api/") and "download/stream" not in request.path:
        if not response.content_type.startswith("application/json"):
            response.headers["Content-Type"] = "application/json"
    response.headers.setdefault("Access-Control-Allow-Origin", "*")
    response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type, Authorization")
    response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
    return response

@app.route("/api/<path:path>", methods=["OPTIONS"])
def options_handler(path):
    return jsonify({"ok": True}), 200

# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
