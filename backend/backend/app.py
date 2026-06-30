"""
VidStream Pro — Flask Backend
yt-dlp · Supabase Auth · Stripe · Railway / Render / localhost
"""

import os, uuid, threading, json
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import yt_dlp

# ── Optional: Stripe ──────────────────────────────────────────
try:
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    STRIPE_OK = bool(stripe.api_key and not stripe.api_key.startswith("sk_YOUR"))
except ImportError:
    stripe    = None
    STRIPE_OK = False

# ── Optional: Supabase ────────────────────────────────────────
try:
    from supabase import create_client
    _SB_URL = os.getenv("SUPABASE_URL", "")
    _SB_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
    sb = create_client(_SB_URL, _SB_KEY) if (_SB_URL and _SB_KEY
         and not _SB_URL.startswith("https://YOUR")) else None
except Exception:
    sb = None

# ═══════════════════════════════════════════════════════════════
#  APP
# ═══════════════════════════════════════════════════════════════
app  = Flask(__name__)
CORS(app, origins="*", supports_credentials=True)

JOBS:    dict = {}   # job_id → {status, progress, speed, eta, filepath, error}
CREDITS: dict = {}   # "uid:date" → used_count

PLANS  = {"guest": 10, "free": 20, "pro": 1000}
PRICES = {
    "monthly": {"amount": 799,  "currency": "usd", "interval": "month"},
    "annual":  {"amount": 5748, "currency": "usd", "interval": "year"},
}

# ═══════════════════════════════════════════════════════════════
#  AUTH HELPERS
# ═══════════════════════════════════════════════════════════════
def _bearer():
    h = request.headers.get("Authorization", "")
    return h[7:] if h.startswith("Bearer ") else None

def _verify(token):
    if not sb or not token:
        return None
    try:
        u = sb.auth.get_user(token).user
        if not u:
            return None
        plan = "free"
        try:
            r = sb.table("profiles").select("plan").eq("id", u.id).single().execute()
            plan = (r.data or {}).get("plan", "free")
        except Exception:
            pass
        return {
            "id":    u.id,
            "email": u.email,
            "name":  u.user_metadata.get("full_name") or u.email.split("@")[0],
            "plan":  plan,
        }
    except Exception:
        return None

def require_auth(f):
    @wraps(f)
    def w(*a, **kw):
        u = _verify(_bearer())
        if not u:
            return jsonify({"error": "Unauthorized"}), 401
        request.user = u
        return f(*a, **kw)
    return w

def optional_auth(f):
    @wraps(f)
    def w(*a, **kw):
        request.user = _verify(_bearer())
        return f(*a, **kw)
    return w

# ═══════════════════════════════════════════════════════════════
#  CREDITS
# ═══════════════════════════════════════════════════════════════
def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

def _credits(uid, plan):
    key  = f"{uid}:{_today()}"
    used = CREDITS.get(key, 0)
    lim  = PLANS.get(plan, 10)
    return {"used": used, "remaining": max(0, lim - used), "limit": lim}

def _consume(uid, plan):
    key  = f"{uid}:{_today()}"
    used = CREDITS.get(key, 0)
    lim  = PLANS.get(plan, 10)
    if used >= lim:
        return False
    CREDITS[key] = used + 1
    return True

# ═══════════════════════════════════════════════════════════════
#  HEALTH  ← frontend pings this to detect backend
# ═══════════════════════════════════════════════════════════════
@app.route("/api/health")
def health():
    return jsonify({
        "status":   "ok",
        "supabase": sb is not None,
        "stripe":   STRIPE_OK,
        "version":  "1.0.0",
        "time":     datetime.now(timezone.utc).isoformat(),
    })

# ═══════════════════════════════════════════════════════════════
#  AUTH ROUTES
# ═══════════════════════════════════════════════════════════════
@app.route("/api/auth/me")
@require_auth
def auth_me():
    u = request.user
    return jsonify({"user": u, "credits": _credits(u["id"], u["plan"]), "plan": u["plan"]})

@app.route("/api/auth/refresh", methods=["POST"])
def auth_refresh():
    token = _bearer()
    if not token or not sb:
        return jsonify({"error": "No token"}), 401
    try:
        s = sb.auth.refresh_session(token)
        return jsonify({"access_token": s.session.access_token,
                        "refresh_token": s.session.refresh_token})
    except Exception as e:
        return jsonify({"error": str(e)}), 401

# ═══════════════════════════════════════════════════════════════
#  USER — CREDITS & HISTORY
# ═══════════════════════════════════════════════════════════════
@app.route("/api/user/credits")
@require_auth
def user_credits():
    u = request.user
    return jsonify({"credits": _credits(u["id"], u["plan"]), "plan": u["plan"]})

@app.route("/api/user/history")
@require_auth
def user_history():
    u   = request.user
    fmt = request.args.get("format", "all")
    if not sb:
        return jsonify({"items": []})
    try:
        q = (sb.table("download_logs")
               .select("*")
               .eq("user_id", u["id"])
               .order("created_at", desc=True)
               .limit(50))
        if fmt != "all":
            q = q.eq("format", fmt)
        return jsonify({"items": q.execute().data or []})
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
#  VIDEO INFO  (yt-dlp extract, no download)
# ═══════════════════════════════════════════════════════════════
@app.route("/api/video/info", methods=["POST"])
@optional_auth
def video_info():
    data = request.get_json(silent=True) or {}
    url  = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400

    user  = getattr(request, "user", None)
    plan  = user["plan"] if user else "guest"
    uid   = user["id"]   if user else request.remote_addr

    if not _consume(uid, plan):
        return jsonify({"error": "Daily credit limit reached",
                        "credits": _credits(uid, plan)}), 429

    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        fmts   = _parse_formats(info.get("formats", []))
        log_id = _log(user, url, info.get("title", ""), info.get("extractor", ""))

        return jsonify({
            "success": True,
            "log_id":  log_id,
            "credits": _credits(uid, plan),
            "data": {
                "title":        info.get("title", ""),
                "thumbnail":    info.get("thumbnail", ""),
                "duration":     info.get("duration", 0),
                "uploader":     info.get("uploader") or info.get("channel", ""),
                "view_count":   info.get("view_count", 0),
                "like_count":   info.get("like_count", 0),
                "upload_date":  info.get("upload_date", ""),
                "description":  (info.get("description") or "")[:300],
                "platform":     info.get("extractor_key", "Unknown"),
                "webpage_url":  info.get("webpage_url", url),
                "formats":      fmts,
                "has_subtitles": bool(info.get("subtitles")),
            },
        })
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": str(e)[:200]}), 422
    except Exception as e:
        return jsonify({"error": str(e)[:200]}), 500

def _parse_formats(raw):
    seen, out = set(), []
    for f in raw:
        fid = f.get("format_id", "")
        if fid in seen:
            continue
        seen.add(fid)
        vc = f.get("vcodec", "none")
        ac = f.get("acodec", "none")
        h  = f.get("height") or 0
        abr = f.get("abr") or 0
        size = round((f.get("filesize") or f.get("filesize_approx") or 0) / 1_048_576, 1)

        if vc != "none" and h >= 144:
            label = (
                "4K UHD"   if h >= 2160 else
                "2K QHD"   if h >= 1440 else
                "1080p FHD" if h >= 1080 else
                "720p HD"  if h >= 720  else
                f"{h}p"
            )
            out.append({
                "format_id":   fid,
                "type":        "video",
                "quality":     label,
                "height":      h,
                "ext":         f.get("ext", "mp4"),
                "filesize_mb": size,
                "recommended": h == 1080,
            })
        elif vc == "none" and ac != "none" and abr:
            out.append({
                "format_id":   fid,
                "type":        "audio",
                "quality":     f"{int(abr)}kbps",
                "abr":         int(abr),
                "ext":         f.get("ext", "m4a"),
                "filesize_mb": size,
                "recommended": 128 <= abr <= 192,
            })

    out.sort(key=lambda x: (x["type"] == "audio",
                             -(x.get("height") or x.get("abr") or 0)))
    return out[:20]

def _log(user, url, title, platform):
    if not sb or not user:
        return None
    try:
        r = sb.table("download_logs").insert({
            "user_id":    user["id"],
            "url":        url,
            "title":      title,
            "platform":   platform,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        return r.data[0]["id"] if r.data else None
    except Exception:
        return None

# ═══════════════════════════════════════════════════════════════
#  DOWNLOAD  (background thread · progress poll · stream)
# ═══════════════════════════════════════════════════════════════
@app.route("/api/download/start", methods=["POST"])
@optional_auth
def download_start():
    data      = request.get_json(silent=True) or {}
    url       = (data.get("url") or "").strip()
    format_id = data.get("format_id", "bestvideo+bestaudio/best")
    if not url:
        return jsonify({"error": "URL required"}), 400

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "status": "pending", "progress": 0,
        "speed": "", "eta": "", "filepath": None, "error": None,
    }
    threading.Thread(
        target=_run_download,
        args=(job_id, url, format_id),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})

def _run_download(job_id, url, format_id):
    out_dir  = "/tmp/vidstream"
    os.makedirs(out_dir, exist_ok=True)
    out_tmpl = os.path.join(out_dir, f"{job_id}.%(ext)s")

    def hook(d):
        if d["status"] == "downloading":
            pct = d.get("_percent_str", "0%").strip().replace("%", "")
            try:
                JOBS[job_id]["progress"] = min(99, float(pct))
            except ValueError:
                pass
            JOBS[job_id]["speed"]  = d.get("_speed_str", "").strip()
            JOBS[job_id]["eta"]    = d.get("_eta_str",   "").strip()
            JOBS[job_id]["status"] = "downloading"
        elif d["status"] == "finished":
            JOBS[job_id]["progress"] = 100
            JOBS[job_id]["filepath"] = d.get("filename", "")
            JOBS[job_id]["status"]   = "done"

    is_audio = (
        "audio" in format_id.lower()
        or format_id in ("bestaudio", "140", "251", "250", "bestaudio/best")
    )

    if is_audio:
        opts = {
            "format":         "bestaudio/best",
            "outtmpl":        out_tmpl,
            "progress_hooks": [hook],
            "quiet":          True,
            "no_warnings":    True,
            "postprocessors": [{
                "key":              "FFmpegExtractAudio",
                "preferredcodec":   "mp3",
                "preferredquality": "320",
            }],
        }
    else:
        opts = {
            "format":               format_id,
            "outtmpl":              out_tmpl,
            "progress_hooks":       [hook],
            "quiet":                True,
            "no_warnings":          True,
            "merge_output_format":  "mp4",
        }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        JOBS[job_id]["status"]   = "done"
        JOBS[job_id]["progress"] = 100
    except Exception as e:
        JOBS[job_id]["status"] = "failed"
        JOBS[job_id]["error"]  = str(e)[:300]

@app.route("/api/download/progress/<job_id>")
def download_progress(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)

@app.route("/api/download/stream")
def download_stream():
    job_id = request.args.get("job_id", "")
    job    = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    filepath = job.get("filepath", "")

    # If exact path missing, scan for any file with this job_id
    if not filepath or not os.path.exists(filepath):
        base = "/tmp/vidstream"
        for f in os.listdir(base) if os.path.isdir(base) else []:
            if f.startswith(job_id):
                filepath = os.path.join(base, f)
                break

    if not filepath or not os.path.exists(filepath):
        return jsonify({"error": "File not ready yet"}), 404

    ext  = filepath.rsplit(".", 1)[-1].lower()
    mime = {
        "mp4": "video/mp4", "mp3": "audio/mpeg",
        "webm": "video/webm", "m4a": "audio/mp4",
    }.get(ext, "application/octet-stream")
    fname = os.path.basename(filepath)

    def generate():
        with open(filepath, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                yield chunk
        try:
            os.remove(filepath)
            JOBS.pop(job_id, None)
        except Exception:
            pass

    return Response(
        stream_with_context(generate()),
        mimetype=mime,
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Content-Length":      str(os.path.getsize(filepath)),
        },
    )

# ═══════════════════════════════════════════════════════════════
#  SEARCH  (yt-dlp ytsearch)
# ═══════════════════════════════════════════════════════════════
@app.route("/api/search/")
@optional_auth
def search():
    q   = request.args.get("q", "").strip()
    n   = min(int(request.args.get("max_results", 10)), 20)
    if not q:
        return jsonify({"error": "Query required"}), 400

    opts = {
        "quiet": True, "no_warnings": True,
        "skip_download": True, "extract_flat": True,
        "default_search": f"ytsearch{n}",
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(q, download=False)
        items = []
        for e in (info.get("entries") or []):
            if not e:
                continue
            vid = e.get("id", "")
            items.append({
                "video_id":    vid,
                "title":       e.get("title", ""),
                "thumbnail":   e.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                "channel":     e.get("channel") or e.get("uploader", ""),
                "duration":    e.get("duration", 0),
                "view_count":  e.get("view_count", 0),
                "published_at":e.get("upload_date", ""),
                "url":         e.get("url") or f"https://www.youtube.com/watch?v={vid}",
            })
        return jsonify({"success": True, "items": items, "query": q})
    except Exception as e:
        return jsonify({"error": str(e)[:200], "items": []}), 500

# ═══════════════════════════════════════════════════════════════
#  TRENDING
# ═══════════════════════════════════════════════════════════════
@app.route("/api/trending/")
def trending():
    cat    = request.args.get("category", "").strip()
    cat_bp = {"music":"10","gaming":"20","sports":"17","tech":"28","news":"25","comedy":"23"}
    url    = "https://www.youtube.com/feed/trending"
    if cat in cat_bp:
        url += f"?bp={cat_bp[cat]}"

    opts = {
        "quiet": True, "no_warnings": True,
        "skip_download": True, "extract_flat": True, "playlistend": 16,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        items = []
        for e in (info.get("entries") or []):
            if not e:
                continue
            vid = e.get("id", "")
            items.append({
                "video_id":   vid,
                "title":      e.get("title", ""),
                "thumbnail":  e.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                "channel":    e.get("channel") or e.get("uploader", ""),
                "view_count": e.get("view_count", 0),
                "duration":   e.get("duration", 0),
                "url":        f"https://www.youtube.com/watch?v={vid}",
            })
        return jsonify({"success": True, "items": items})
    except Exception as e:
        return jsonify({"error": str(e)[:200], "items": []}), 500

# ═══════════════════════════════════════════════════════════════
#  BILLING  (Stripe Checkout)
# ═══════════════════════════════════════════════════════════════
@app.route("/api/billing/create-checkout", methods=["POST"])
@require_auth
def billing_checkout():
    if not STRIPE_OK:
        return jsonify({"error": "Stripe not configured on server"}), 503
    data    = request.get_json(silent=True) or {}
    billing = data.get("billing", "monthly")
    user    = request.user
    pcfg    = PRICES.get(billing, PRICES["monthly"])
    try:
        custs = stripe.Customer.list(email=user["email"], limit=1)
        cust  = custs.data[0] if custs.data else stripe.Customer.create(
            email=user["email"], metadata={"user_id": user["id"]}
        )
        session = stripe.checkout.Session.create(
            customer=cust.id,
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency":    pcfg["currency"],
                    "unit_amount": pcfg["amount"],
                    "recurring":   {"interval": pcfg["interval"]},
                    "product_data":{
                        "name":        "VidStream Pro",
                        "description": "1,000 credits/day · 4K · Zero ads · Priority speed",
                    },
                },
                "quantity": 1,
            }],
            mode="subscription",
            success_url=data.get("success_url",
                os.getenv("FRONTEND_URL","http://localhost:3000")+"?payment=success"),
            cancel_url=data.get("cancel_url",
                os.getenv("FRONTEND_URL","http://localhost:3000")+"?payment=canceled"),
            metadata={"user_id": user["id"], "billing": billing},
        )
        return jsonify({"checkout_url": session.url, "session_id": session.id})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/billing/webhook", methods=["POST"])
def billing_webhook():
    if not STRIPE_OK:
        return jsonify({"error": "Stripe not configured"}), 503
    payload   = request.get_data()
    sig       = request.headers.get("Stripe-Signature", "")
    wh_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, wh_secret)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

    etype = event["type"]
    obj   = event["data"]["object"]

    if etype in ("checkout.session.completed", "invoice.payment_succeeded"):
        uid = obj.get("metadata", {}).get("user_id")
        if uid and sb:
            try:
                sb.table("profiles").upsert({
                    "id": uid, "plan": "pro",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).execute()
            except Exception:
                pass

    elif etype in ("customer.subscription.deleted", "invoice.payment_failed"):
        cid = obj.get("customer")
        if cid and sb:
            try:
                uid = stripe.Customer.retrieve(cid).metadata.get("user_id")
                if uid:
                    sb.table("profiles").update({"plan": "free"}).eq("id", uid).execute()
            except Exception:
                pass

    return jsonify({"received": True})

# ═══════════════════════════════════════════════════════════════
#  RUN
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"▶ VidStream Pro backend starting on port {port}")
    print(f"  Supabase : {'✅ connected' if sb        else '⚠ not configured'}")
    print(f"  Stripe   : {'✅ configured' if STRIPE_OK else '⚠ not configured'}")
    app.run(host="0.0.0.0", port=port, debug=False)
