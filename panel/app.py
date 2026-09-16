#!/usr/bin/env python3
"""
Admin panel for the Bedrock Dedicated Server.

Runs INSIDE the same container/volume as bedrock_server (started in the
background by start.sh) so it can read/write /data directly and talk to
the running server through a stdin FIFO + log file, without needing a
second Railway service or a shared volume across services.

Auth model
----------
Multiple admin accounts are stored in /data/admins.json (created on the
Railway Volume, so it survives restarts/redeploys). Each account has a
role:

  - owner    : full access, including managing other admin accounts.
  - operator : full access to server/ban/config management, but cannot
               manage admin accounts.
  - member   : read-only access (dashboard, online players, versions,
               logs).
  - custom   : a hand-picked set of permissions chosen when the account
               is created.

The very first owner account is bootstrapped automatically from the
ADMIN_USER / ADMIN_PASSWORD environment variables the first time the
panel starts and /data/admins.json does not exist yet. After that,
admins.json is the source of truth — you manage accounts from inside
the panel (Admins page), and you no longer need to touch the env vars.

If neither admins.json nor ADMIN_PASSWORD exists, the whole panel
refuses to serve anything — better to be unreachable than
unauthenticated.
"""

import io
import json
import os
import re
import time
import traceback
import zipfile
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import (
    Flask,
    Response,
    redirect,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

try:
    import psutil
except ImportError:  # optional dependency — panel still works without it
    psutil = None

app = Flask(__name__)

DATA_DIR = Path("/data")
LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "console.log"
AUDIT_FILE = LOG_DIR / "audit.log"
STDIN_FIFO = DATA_DIR / "bds_stdin"
VERSION_HISTORY_FILE = DATA_DIR / "version_history.json"
STARTED_AT_FILE = DATA_DIR / ".started_at"
SERVER_PROPERTIES = DATA_DIR / "server.properties"
ALLOWLIST_FILE = DATA_DIR / "allowlist.json"
PERMISSIONS_FILE = DATA_DIR / "permissions.json"
BANNED_FILE = DATA_DIR / "banned_players.json"
ADMINS_FILE = DATA_DIR / "admins.json"
FLASK_SECRET_FILE = DATA_DIR / ".flask_secret"
WORLDS_DIR = DATA_DIR / "worlds"

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# ---------------------------------------------------------------------------
# Roles & permissions
# ---------------------------------------------------------------------------
PERMISSIONS = {
    "dashboard_view": "مشاهده داشبورد",
    "users_view": "مشاهده کاربران آنلاین",
    "ban_manage": "بن / آن‌بن کاربران",
    "kick_manage": "اخراج (Kick) کاربران",
    "config_edit": "ویرایش تنظیمات (properties / allowlist / permissions)",
    "versions_view": "مشاهده تاریخچه نسخه‌ها",
    "logs_view": "مشاهده لاگ‌های کنسول",
    "audit_view": "مشاهده گزارش عملکرد ادمین‌ها",
    "restart_server": "ری‌استارت سرور",
    "broadcast": "ارسال پیام همگانی",
    "backup_download": "دانلود بکاپ World",
    "admins_manage": "مدیریت حساب‌های ادمین",
}

ROLE_LABELS = {
    "owner": "مالک (Owner)",
    "operator": "اپراتور (Operator)",
    "member": "عضو (Member)",
    "custom": "سفارشی (Custom)",
}

ROLE_PRESETS = {
    "owner": set(PERMISSIONS.keys()),
    "operator": set(PERMISSIONS.keys()) - {"admins_manage"},
    "member": {"dashboard_view", "users_view", "versions_view", "logs_view"},
}

BASE_CSS = """
<style>
  body{font-family:Tahoma,Vazirmatn,sans-serif;background:#151a1e;color:#e8e8e8;
       max-width:900px;margin:24px auto;padding:0 16px;direction:rtl}
  h1,h2{color:#8ee08e}
  nav a{color:#8ec8ff;margin-inline-end:14px;text-decoration:none;font-size:14px}
  nav a:hover{text-decoration:underline}
  .topbar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
  .card{background:#1e2530;border:1px solid #2c3644;border-radius:10px;
        padding:16px;margin-bottom:16px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
  .ok{color:#8ee08e}.warn{color:#f0c674}.bad{color:#f07178}
  textarea{width:100%;min-height:220px;background:#0f1318;color:#e8e8e8;
           border:1px solid #2c3644;border-radius:6px;padding:8px;font-family:monospace}
  input[type=text],input[type=password],select{
           background:#0f1318;color:#e8e8e8;border:1px solid #2c3644;
           border-radius:6px;padding:6px 8px;width:100%;box-sizing:border-box;margin-bottom:8px}
  label{font-size:13px;color:#9aa5b1}
  table{width:100%;border-collapse:collapse}
  td,th{border-bottom:1px solid #2c3644;padding:6px 8px;text-align:right;font-size:13px}
  button{background:#3a7bd5;color:#fff;border:0;border-radius:6px;
         padding:8px 14px;cursor:pointer;font-size:14px}
  button.danger{background:#c0392b}
  button.warnbtn{background:#c0821b}
  button.small{padding:4px 10px;font-size:12px}
  .badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;background:#2c3644}
  .badge.owner{background:#3a7bd5}
  .badge.operator{background:#2f9e5b}
  .badge.member{background:#555f6e}
  .badge.custom{background:#8e5cc4}
  .inline-form{display:inline}
  .small{color:#9aa5b1;font-size:12px}
  pre{white-space:pre-wrap;background:#0f1318;padding:10px;border-radius:6px;
      max-height:400px;overflow:auto;font-size:12px}
  .perm-list{display:grid;grid-template-columns:1fr 1fr;gap:4px;margin:8px 0}
  .perm-list label{color:#e8e8e8;font-size:13px}
</style>
"""


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


# ---------------------------------------------------------------------------
# Secret key (persisted so logged-in sessions survive a panel restart)
# ---------------------------------------------------------------------------
def _get_secret_key():
    try:
        if FLASK_SECRET_FILE.exists():
            data = FLASK_SECRET_FILE.read_bytes()
            if data:
                return data
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        key = os.urandom(32)
        FLASK_SECRET_FILE.write_bytes(key)
        try:
            os.chmod(FLASK_SECRET_FILE, 0o600)
        except OSError:
            pass
        return key
    except OSError:
        return os.urandom(32)


app.secret_key = _get_secret_key()
app.permanent_session_lifetime = timedelta(hours=12)


# ---------------------------------------------------------------------------
# Admin accounts
# ---------------------------------------------------------------------------
def load_admins():
    if not ADMINS_FILE.exists():
        return []
    try:
        return json.loads(ADMINS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def save_admins(admins):
    ADMINS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ADMINS_FILE.write_text(json.dumps(admins, ensure_ascii=False, indent=2))


def bootstrap_admins_if_needed():
    """Create the first owner account from env vars, once. After
    admins.json exists it is never auto-modified again — accounts are
    managed entirely from inside the panel from that point on."""
    if ADMINS_FILE.exists():
        return
    if not ADMIN_PASSWORD:
        return
    admins = [
        {
            "username": ADMIN_USER,
            "password_hash": generate_password_hash(ADMIN_PASSWORD),
            "role": "owner",
            "permissions": [],
            "created_at": _now_iso(),
            "created_by": "bootstrap",
        }
    ]
    save_admins(admins)


def panel_enabled():
    return ADMINS_FILE.exists()


def admin_permissions(admin):
    role = admin.get("role", "member")
    if role == "custom":
        return set(admin.get("permissions", []))
    return set(ROLE_PRESETS.get(role, ROLE_PRESETS["member"]))


def current_admin():
    username = session.get("username")
    if not username:
        return None
    for a in load_admins():
        if a.get("username") == username:
            return a
    return None


def owners_count(admins):
    return sum(1 for a in admins if a.get("role") == "owner")


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
def append_audit(username, action, details=""):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"time": _now_iso(), "user": username, "action": action, "details": details},
            ensure_ascii=False,
        )
        with open(AUDIT_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def read_audit_tail(n=200):
    if not AUDIT_FILE.exists():
        return []
    try:
        lines = AUDIT_FILE.read_text(errors="ignore").splitlines()[-n:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ---------------------------------------------------------------------------
# Auth decorators
# ---------------------------------------------------------------------------
def require_perm(perm):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            admin = current_admin()
            if admin is None:
                return redirect(url_for("login", next=request.path))
            if perm not in admin_permissions(admin):
                return (
                    render(
                        "دسترسی غیرمجاز",
                        "<div class='card'><p class='bad'>حساب شما دسترسی لازم برای این بخش را ندارد."
                        "</p><p class='small'>اگر فکر می‌کنید این یک اشتباه است، از یک ادمین با نقش"
                        " مالک بخواهید دسترسی لازم را به حساب شما اضافه کند.</p></div>",
                        admin,
                    ),
                    403,
                )
            return f(*args, **kwargs)

        return wrapped

    return decorator


def require_login(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        admin = current_admin()
        if admin is None:
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)

    return wrapped


@app.before_request
def _panel_gate():
    if not panel_enabled():
        return Response(
            "پنل غیرفعال است: هیچ حساب ادمینی تعریف نشده و متغیر محیطی ADMIN_PASSWORD هم "
            "تنظیم نشده. یک مقدار برای ADMIN_PASSWORD در Railway Variables تنظیم کنید و "
            "سرویس را Redeploy کنید تا اولین حساب مالک به‌صورت خودکار ساخته شود.",
            503,
        )
    return None


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------
def nav_html(admin):
    if admin is None:
        return ""
    perms = admin_permissions(admin)
    items = []
    for endpoint, label, perm in [
        ("dashboard", "داشبورد", "dashboard_view"),
        ("users", "کاربران", "users_view"),
        ("versions", "نسخه‌ها", "versions_view"),
        ("properties", "server.properties", "config_edit"),
        ("allowlist", "Allowlist", "config_edit"),
        ("permissions_page", "Permissions", "config_edit"),
        ("logs", "لاگ‌ها", "logs_view"),
        ("audit", "گزارش عملکرد", "audit_view"),
        ("admins", "ادمین‌ها", "admins_manage"),
    ]:
        if perm in perms:
            items.append(f"<a href=\"{url_for(endpoint)}\">{label}</a>")
    items.append(f"<a href=\"{url_for('account')}\">حساب من</a>")
    items.append(f"<a href=\"{url_for('logout')}\">خروج</a>")
    role = admin.get("role", "member")
    badge = f"<span class='badge {role}'>{ROLE_LABELS.get(role, role)}</span>"
    top = (
        f"<div class='topbar'><nav>{''.join(items)}</nav>"
        f"<div class='small'>{admin.get('username','')} {badge}</div></div>"
    )
    return top


def render(title, body, admin=None):
    from flask import render_template_string

    nav = nav_html(admin) if admin else ""
    return render_template_string(
        "<!doctype html><html lang='fa'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{title}</title>{BASE_CSS}</head><body>"
        f"<h1>🧱 {title}</h1>{nav}<hr style='border-color:#2c3644'>{body}</body></html>"
    )


def render_bare(title, body):
    from flask import render_template_string

    return render_template_string(
        "<!doctype html><html lang='fa'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{title}</title>{BASE_CSS}</head><body>"
        f"<h1>🧱 {title}</h1>{body}</body></html>"
    )


# ---------------------------------------------------------------------------
# Server helpers
# ---------------------------------------------------------------------------
def send_command(cmd: str) -> bool:
    """Write a console command to bedrock_server's stdin FIFO."""
    if not STDIN_FIFO.exists():
        return False
    try:
        with open(STDIN_FIFO, "w") as f:
            f.write(cmd.strip() + "\n")
        return True
    except OSError:
        return False


def read_log_tail(max_lines=200):
    if not LOG_FILE.exists():
        return ""
    try:
        with open(LOG_FILE, "r", errors="ignore") as f:
            lines = f.readlines()
        return "".join(lines[-max_lines:])
    except OSError:
        return ""


def query_online_players(wait_seconds=1.5):
    """Send the 'list' console command and parse the response from the log."""
    if not LOG_FILE.exists():
        return None
    try:
        start_pos = LOG_FILE.stat().st_size
    except OSError:
        return None
    if not send_command("list"):
        return None
    time.sleep(wait_seconds)
    try:
        with open(LOG_FILE, "r", errors="ignore") as f:
            f.seek(start_pos)
            new_text = f.read()
    except OSError:
        return None
    m = re.search(r"There are (\d+)/(\d+) players online:?\s*(.*)", new_text)
    if not m:
        return None
    online, maxp, names = m.groups()
    players = [n.strip() for n in names.split(",") if n.strip()]
    return {"online": int(online), "max": int(maxp), "players": players}


def load_json_file(path: Path):
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def get_server_version():
    return os.environ.get("BDS_VERSION", "نامشخص")


def get_uptime_str():
    if not STARTED_AT_FILE.exists():
        return "نامشخص"
    try:
        started = float(STARTED_AT_FILE.read_text().strip())
    except (OSError, ValueError):
        return "نامشخص"
    seconds = int(time.time() - started)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h} ساعت {m} دقیقه"


def get_level_name():
    if not SERVER_PROPERTIES.exists():
        return "؟"
    for line in SERVER_PROPERTIES.read_text().splitlines():
        if line.startswith("level-name="):
            return line.split("=", 1)[1] or "(بدون نام)"
    return "؟"


def load_banned():
    return load_json_file(BANNED_FILE) or []


def save_banned(entries):
    BANNED_FILE.parent.mkdir(parents=True, exist_ok=True)
    BANNED_FILE.write_text(json.dumps(entries, ensure_ascii=False, indent=2))


def is_valid_player_name(name: str) -> bool:
    return bool(name) and 1 <= len(name) <= 32 and "\n" not in name and "\r" not in name


# ---------------------------------------------------------------------------
# Background ban-enforcement loop
# ---------------------------------------------------------------------------
def _ban_enforcement_loop():
    while True:
        try:
            time.sleep(20)
            banned = load_banned()
            if not banned:
                continue
            banned_lower = {b.get("name", "").lower() for b in banned if b.get("name")}
            if not banned_lower:
                continue
            result = query_online_players(wait_seconds=1.2)
            if not result:
                continue
            for p in result["players"]:
                if p.lower() in banned_lower:
                    send_command(f"kick {p} شما بن شده‌اید")
                    append_audit("سیستم (خودکار)", "auto_kick_banned_player", p)
        except Exception:
            traceback.print_exc()


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_admin() is not None:
        return redirect(url_for("dashboard"))
    error = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        match = next((a for a in load_admins() if a.get("username") == username), None)
        if match and check_password_hash(match.get("password_hash", ""), password):
            session.clear()
            session.permanent = True
            session["username"] = username
            append_audit(username, "login")
            nxt = request.args.get("next") or url_for("dashboard")
            return redirect(nxt)
        error = "نام کاربری یا رمز عبور اشتباه است."
        append_audit(username or "؟", "login_failed")
    body = f"""
    <div class="card" style="max-width:360px;margin:60px auto">
      <h2 style="text-align:center">ورود به پنل مدیریت</h2>
      {"<p class='bad'>" + error + "</p>" if error else ""}
      <form method="post">
        <label>نام کاربری</label>
        <input type="text" name="username" required autofocus>
        <label>رمز عبور</label>
        <input type="password" name="password" required>
        <button type="submit" style="width:100%">ورود</button>
      </form>
    </div>
    """
    return render_bare("ورود", body)


@app.route("/logout")
def logout():
    admin = current_admin()
    if admin:
        append_audit(admin["username"], "logout")
    session.clear()
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
@require_login
def account():
    admin = current_admin()
    message = ""
    if request.method == "POST":
        new_password = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if len(new_password) < 6:
            message = "<p class='bad'>رمز عبور جدید باید حداقل ۶ کاراکتر باشد.</p>"
        elif new_password != confirm:
            message = "<p class='bad'>تکرار رمز عبور مطابقت ندارد.</p>"
        else:
            admins = load_admins()
            for a in admins:
                if a["username"] == admin["username"]:
                    a["password_hash"] = generate_password_hash(new_password)
            save_admins(admins)
            append_audit(admin["username"], "change_own_password")
            message = "<p class='ok'>رمز عبور با موفقیت تغییر کرد.</p>"
            admin = current_admin()
    perms = sorted(admin_permissions(admin))
    perm_list = "".join(f"<li>{PERMISSIONS.get(p, p)}</li>" for p in perms) or "<li class='small'>هیچ دسترسی‌ای ندارید.</li>"
    role = admin.get("role", "member")
    body = f"""
    <div class="card">
      <h2>مشخصات حساب</h2>
      <p><b>نام کاربری:</b> {admin.get('username')}</p>
      <p><b>نقش:</b> <span class="badge {role}">{ROLE_LABELS.get(role, role)}</span></p>
      <p><b>دسترسی‌ها:</b></p>
      <ul>{perm_list}</ul>
    </div>
    <div class="card">
      <h2>تغییر رمز عبور</h2>
      {message}
      <form method="post">
        <label>رمز عبور جدید</label>
        <input type="password" name="new_password" required>
        <label>تکرار رمز عبور جدید</label>
        <input type="password" name="confirm_password" required>
        <button type="submit">ذخیره</button>
      </form>
    </div>
    """
    return render("حساب من", body, admin)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@app.route("/")
@require_perm("dashboard_view")
def dashboard():
    admin = current_admin()
    version = get_server_version()
    uptime = get_uptime_str()
    world = get_level_name()
    perms = admin_permissions(admin)

    if psutil is not None:
        try:
            cpu = psutil.cpu_percent(interval=0.2)
            mem = psutil.virtual_memory()
            resource_html = (
                f"<div><b>CPU:</b> {cpu:.0f}%</div>"
                f"<div><b>RAM:</b> {mem.percent:.0f}% ({mem.used // (1024*1024)}MB / {mem.total // (1024*1024)}MB)</div>"
            )
        except Exception:
            resource_html = "<div class='small'>خواندن منابع سیستم ممکن نشد.</div>"
    else:
        resource_html = "<div class='small'>برای نمایش مصرف CPU/RAM باید psutil نصب باشد.</div>"

    restart_block = ""
    if "restart_server" in perms:
        restart_block = f"""
        <form method="post" action="{url_for('restart')}"
              onsubmit="return confirm('سرور Restart می‌شود، مطمئنید؟');" class="inline-form">
          <button class="danger" type="submit">Restart سرور</button>
        </form>
        """

    broadcast_block = ""
    if "broadcast" in perms:
        broadcast_block = f"""
        <div class="card">
          <h2>📢 پیام همگانی</h2>
          <form method="post" action="{url_for('broadcast')}">
            <input type="text" name="message" placeholder="پیام برای همه بازیکنان..." required>
            <button type="submit">ارسال</button>
          </form>
        </div>
        """

    backup_block = ""
    if "backup_download" in perms:
        backup_block = f"""
        <div class="card">
          <h2>💾 بکاپ World</h2>
          <p class="small">یک فایل zip از پوشه worlds برای دانلود بلافاصله ساخته می‌شود.</p>
          <a href="{url_for('backup_download')}"><button type="button" onclick="location.href='{url_for('backup_download')}'">دانلود بکاپ</button></a>
        </div>
        """

    domain_block = """
    <div class="card">
      <h2>🌐 دامنه عمومی</h2>
      <p class="small">
        ساخت دامنه یک عملیات داخل خود Railway است (کد این پنل نمی‌تواند آن را خودکار کند):
        در سرویس <code>bedrock-server</code> برو به
        <b>Settings → Networking → Generate Domain</b>، پورت <b>8080</b>
        (یا مقدار PORT خودت) را وارد کن. بعد از ساخته شدن دامنه، همین پنل
        روی همان آدرس در دسترس است.
      </p>
    </div>
    """

    body = f"""
    <div class="card">
      <div class="grid">
        <div><b>نسخه فعلی BDS:</b> {version}</div>
        <div><b>مدت اجرا:</b> {uptime}</div>
        <div><b>نام World:</b> {world}</div>
        <div><b>پورت بازی:</b> UDP 19132</div>
        {resource_html}
      </div>
    </div>
    {domain_block}
    <div class="card">
      <h2>عملیات سرور</h2>
      {restart_block}
      <p class="small">
        Restart با ارسال دستور <code>stop</code> به سرور انجام می‌شود؛
        چون restartPolicy روی Railway برابر ALWAYS است، Railway کانتینر را
        دوباره بالا می‌آورد. World و تنظیمات دست‌نخورده باقی می‌مانند.
      </p>
    </div>
    {broadcast_block}
    {backup_block}
    """
    return render("داشبورد", body, admin)


@app.route("/restart", methods=["POST"])
@require_perm("restart_server")
def restart():
    admin = current_admin()
    send_command("stop")
    append_audit(admin["username"], "restart_server")
    return redirect(url_for("dashboard"))


@app.route("/broadcast", methods=["POST"])
@require_perm("broadcast")
def broadcast():
    admin = current_admin()
    message = request.form.get("message", "").strip()
    if message and "\n" not in message:
        send_command(f"say {message}")
        append_audit(admin["username"], "broadcast", message)
    return redirect(url_for("dashboard"))


@app.route("/backup/download")
@require_perm("backup_download")
def backup_download():
    admin = current_admin()
    if not WORLDS_DIR.exists():
        return render("بکاپ", "<div class='card'><p class='bad'>پوشه worlds پیدا نشد.</p></div>", admin)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in WORLDS_DIR.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(DATA_DIR))
    buf.seek(0)
    append_audit(admin["username"], "download_backup")
    filename = f"world-backup-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.zip"
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=filename)


# ---------------------------------------------------------------------------
# Users / bans
# ---------------------------------------------------------------------------
@app.route("/users")
@require_perm("users_view")
def users():
    admin = current_admin()
    perms = admin_permissions(admin)
    online = query_online_players()
    allow = load_json_file(ALLOWLIST_FILE)
    perms_json = load_json_file(PERMISSIONS_FILE)
    banned = load_banned()
    banned_lower = {b.get("name", "").lower() for b in banned}

    def player_row(name):
        actions = ""
        if "kick_manage" in perms:
            actions += (
                f"<form class='inline-form' method='post' action='{url_for('kick_player')}'>"
                f"<input type='hidden' name='name' value='{name}'>"
                f"<button class='small warnbtn' type='submit' "
                f"onclick=\"return confirm('اخراج {name}؟');\">Kick</button></form> "
            )
        if "ban_manage" in perms and name.lower() not in banned_lower:
            actions += (
                f"<form class='inline-form' method='post' action='{url_for('ban_player')}'>"
                f"<input type='hidden' name='name' value='{name}'>"
                f"<input type='hidden' name='reason' value='بن شده توسط ادمین'>"
                f"<button class='small danger' type='submit' "
                f"onclick=\"return confirm('بن {name}؟');\">Ban</button></form>"
            )
        return f"<tr><td>{name}</td><td>{actions or '-'}</td></tr>"

    if online is None:
        online_html = "<p class='warn'>پاسخی از سرور دریافت نشد (شاید هنوز کاملاً بالا نیامده).</p>"
    else:
        rows = "".join(player_row(p) for p in online["players"])
        table = f"<table><tr><th>نام</th><th>عملیات</th></tr>{rows}</table>" if rows else "<p class='small'>کسی وصل نیست.</p>"
        online_html = f"<p>{online['online']}/{online['max']} بازیکن آنلاین</p>{table}"

    ban_form = ""
    if "ban_manage" in perms:
        ban_form = f"""
        <form method="post" action="{url_for('ban_player')}">
          <label>نام بازیکن برای بن دستی</label>
          <input type="text" name="name" required maxlength="32">
          <label>دلیل (اختیاری)</label>
          <input type="text" name="reason" maxlength="120">
          <button type="submit" class="danger">بن کردن</button>
        </form>
        """

    if banned:
        ban_rows = "".join(
            f"<tr><td>{b.get('name')}</td><td>{b.get('reason','-')}</td>"
            f"<td>{b.get('banned_by','-')}</td><td>{b.get('banned_at','-')}</td>"
            f"<td>{'' if 'ban_manage' not in perms else f'''<form class=\"inline-form\" method=\"post\" action=\"{url_for('unban_player')}\"><input type=\"hidden\" name=\"name\" value=\"{b.get('name')}\"><button class=\"small\" type=\"submit\">آن‌بن</button></form>'''}</td></tr>"
            for b in banned
        )
        ban_table = f"<table><tr><th>نام</th><th>دلیل</th><th>توسط</th><th>زمان</th><th></th></tr>{ban_rows}</table>"
    else:
        ban_table = "<p class='small'>هیچ بازیکنی بن نشده است.</p>"

    def json_table(items, empty_msg):
        if items is None:
            return "<p class='bad'>فایل قابل خواندن نیست (JSON نامعتبر).</p>"
        if not items:
            return f"<p class='small'>{empty_msg}</p>"
        rows = "".join(f"<tr><td>{json.dumps(i, ensure_ascii=False)}</td></tr>" for i in items)
        return f"<table>{rows}</table>"

    config_links = ""
    if "config_edit" in perms:
        config_links = (
            f"<p class='small'><a href='{url_for('allowlist')}'>ویرایش Allowlist</a> | "
            f"<a href='{url_for('permissions_page')}'>ویرایش Permissions</a></p>"
        )

    body = f"""
    <div class="card">
      <h2>بازیکنان آنلاین (Live)</h2>
      {online_html}
      <p class="small">این بخش با ارسال دستور «list» به کنسول سرور به‌روز می‌شود.</p>
    </div>
    <div class="card">
      <h2>🚫 بن‌ شده‌ها ({len(banned)})</h2>
      {ban_table}
      {ban_form}
      <p class="small">
        سرور Bedrock به‌صورت رسمی سیستم بن ندارد؛ این پنل با نگه‌داشتن یک
        لیست بن (banned_players.json) و اخراج خودکار هر بازیکن بن‌شده‌ای
        که وصل شود (هر ۲۰ ثانیه بررسی می‌شود)، بن را عملاً اجرا می‌کند.
      </p>
    </div>
    <div class="card">
      <h2>Allowlist ({len(allow) if allow else 0})</h2>
      {json_table(allow, "Allowlist خالی است (یا allow-list=false در server.properties).")}
    </div>
    <div class="card">
      <h2>Permissions</h2>
      {json_table(perms_json, "فایل permissions.json خالی است.")}
      {config_links}
    </div>
    """
    return render("کاربران", body, admin)


@app.route("/users/kick", methods=["POST"])
@require_perm("kick_manage")
def kick_player():
    admin = current_admin()
    name = request.form.get("name", "").strip()
    if is_valid_player_name(name):
        send_command(f"kick {name}")
        append_audit(admin["username"], "kick_player", name)
    return redirect(url_for("users"))


@app.route("/users/ban", methods=["POST"])
@require_perm("ban_manage")
def ban_player():
    admin = current_admin()
    name = request.form.get("name", "").strip()
    reason = request.form.get("reason", "").strip() or "بدون دلیل ذکرشده"
    if is_valid_player_name(name):
        banned = load_banned()
        if not any(b.get("name", "").lower() == name.lower() for b in banned):
            banned.append(
                {
                    "name": name,
                    "reason": reason,
                    "banned_by": admin["username"],
                    "banned_at": _now_iso(),
                }
            )
            save_banned(banned)
            append_audit(admin["username"], "ban_player", f"{name} ({reason})")
        send_command(f"kick {name} {reason}")
    return redirect(url_for("users"))


@app.route("/users/unban", methods=["POST"])
@require_perm("ban_manage")
def unban_player():
    admin = current_admin()
    name = request.form.get("name", "").strip()
    banned = load_banned()
    new_list = [b for b in banned if b.get("name", "").lower() != name.lower()]
    if len(new_list) != len(banned):
        save_banned(new_list)
        append_audit(admin["username"], "unban_player", name)
    return redirect(url_for("users"))


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------
@app.route("/versions")
@require_perm("versions_view")
def versions():
    admin = current_admin()
    history = load_json_file(VERSION_HISTORY_FILE) or []
    current = get_server_version()
    options = "".join(
        f"<option value='{h.get('version')}' {'selected' if h.get('version') == current else ''}>"
        f"{h.get('version')} — اولین اجرا: {h.get('first_seen', '؟')}</option>"
        for h in history
    )
    rows = "".join(
        f"<tr><td>{h.get('version')}</td><td>{h.get('first_seen','؟')}</td>"
        f"<td>{h.get('last_seen','؟')}</td></tr>"
        for h in history
    )
    body = f"""
    <div class="card">
      <h2>نسخه‌ی در حال اجرا</h2>
      <p><b>{current}</b></p>
      <h2>همه نسخه‌هایی که تا امروز روی این Volume اجرا شده‌اند</h2>
      <select disabled>{options or "<option>هنوز تاریخچه‌ای ثبت نشده</option>"}</select>
      <table style="margin-top:10px">
        <tr><th>نسخه</th><th>اولین اجرا</th><th>آخرین اجرا</th></tr>
        {rows}
      </table>
    </div>
    """
    return render("نسخه‌ها", body, admin)


# ---------------------------------------------------------------------------
# Config editors
# ---------------------------------------------------------------------------
@app.route("/properties", methods=["GET", "POST"])
@require_perm("config_edit")
def properties():
    admin = current_admin()
    message = ""
    if request.method == "POST":
        new_content = request.form.get("content", "")
        if new_content.strip():
            SERVER_PROPERTIES.write_text(new_content)
            append_audit(admin["username"], "edit_server_properties")
            message = "<p class='ok'>ذخیره شد. برای اعمال، سرور را Restart کنید.</p>"
    content = SERVER_PROPERTIES.read_text() if SERVER_PROPERTIES.exists() else ""
    body = f"""
    <div class="card">
      <h2>ویرایش server.properties</h2>
      {message}
      <form method="post">
        <textarea name="content">{content}</textarea><br><br>
        <button type="submit">ذخیره</button>
      </form>
      <p class="small">تغییرات فقط بعد از Restart سرور اعمال می‌شوند.</p>
    </div>
    """
    return render("server.properties", body, admin)


def _json_editor(path: Path, title, admin):
    message = ""
    if request.method == "POST":
        raw = request.form.get("content", "")
        try:
            parsed = json.loads(raw)
            path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2))
            append_audit(admin["username"], "edit_json_config", title)
            message = "<p class='ok'>ذخیره شد. برای اعمال، سرور را Restart کنید.</p>"
        except json.JSONDecodeError as e:
            message = f"<p class='bad'>JSON نامعتبر: {e}</p>"
    content = path.read_text() if path.exists() else "[]"
    body = f"""
    <div class="card">
      <h2>ویرایش {title}</h2>
      {message}
      <form method="post">
        <textarea name="content">{content}</textarea><br><br>
        <button type="submit">ذخیره</button>
      </form>
      <p class="small">باید JSON معتبر باشد. تغییرات بعد از Restart اعمال می‌شوند.</p>
    </div>
    """
    return render(title, body, admin)


@app.route("/allowlist", methods=["GET", "POST"])
@require_perm("config_edit")
def allowlist():
    return _json_editor(ALLOWLIST_FILE, "allowlist.json", current_admin())


@app.route("/permissions", methods=["GET", "POST"])
@require_perm("config_edit")
def permissions_page():
    return _json_editor(PERMISSIONS_FILE, "permissions.json", current_admin())


# ---------------------------------------------------------------------------
# Logs / audit
# ---------------------------------------------------------------------------
@app.route("/logs")
@require_perm("logs_view")
def logs():
    admin = current_admin()
    tail = read_log_tail(300)
    body = f"""
    <div class="card">
      <h2>آخرین ۳۰۰ خط لاگ سرور</h2>
      <pre>{tail or '(هنوز لاگی نیست)'}</pre>
      <p class="small"><a href="{url_for('logs')}">تازه‌سازی</a></p>
    </div>
    """
    return render("لاگ‌ها", body, admin)


@app.route("/audit")
@require_perm("audit_view")
def audit():
    admin = current_admin()
    entries = read_audit_tail(300)
    rows = "".join(
        f"<tr><td>{e.get('time')}</td><td>{e.get('user')}</td>"
        f"<td>{e.get('action')}</td><td>{e.get('details','')}</td></tr>"
        for e in entries
    )
    table = f"<table><tr><th>زمان</th><th>کاربر</th><th>عملیات</th><th>جزئیات</th></tr>{rows}</table>" if rows else "<p class='small'>هنوز گزارشی ثبت نشده.</p>"
    body = f"""
    <div class="card">
      <h2>گزارش عملکرد ادمین‌ها (۳۰۰ رکورد آخر)</h2>
      {table}
      <p class="small"><a href="{url_for('audit')}">تازه‌سازی</a></p>
    </div>
    """
    return render("گزارش عملکرد", body, admin)


# ---------------------------------------------------------------------------
# Admin account management (owner / anyone with admins_manage)
# ---------------------------------------------------------------------------
@app.route("/admins", methods=["GET"])
@require_perm("admins_manage")
def admins():
    admin = current_admin()
    all_admins = load_admins()
    rows = ""
    for a in all_admins:
        role = a.get("role", "member")
        can_delete = not (a["username"] == admin["username"] or (role == "owner" and owners_count(all_admins) <= 1))
        delete_btn = (
            f"<form class='inline-form' method='post' action='{url_for('admin_delete')}' "
            f"onsubmit=\"return confirm('حذف {a['username']}؟');\">"
            f"<input type='hidden' name='username' value='{a['username']}'>"
            f"<button class='small danger' type='submit'>حذف</button></form>"
            if can_delete
            else "<span class='small'>-</span>"
        )
        custom_perms = ", ".join(PERMISSIONS.get(p, p) for p in a.get("permissions", [])) if role == "custom" else "-"
        rows += (
            f"<tr><td>{a['username']}</td>"
            f"<td><span class='badge {role}'>{ROLE_LABELS.get(role, role)}</span></td>"
            f"<td class='small'>{custom_perms}</td>"
            f"<td class='small'>{a.get('created_at','؟')}</td>"
            f"<td>{delete_btn}</td></tr>"
        )

    perm_checkboxes = "".join(
        f"<label><input type='checkbox' name='perm' value='{key}'> {label}</label>"
        for key, label in PERMISSIONS.items()
        if key != "admins_manage"
    )

    body = f"""
    <div class="card">
      <h2>حساب‌های ادمین</h2>
      <table>
        <tr><th>نام کاربری</th><th>نقش</th><th>دسترسی سفارشی</th><th>ایجاد شده</th><th></th></tr>
        {rows}
      </table>
    </div>
    <div class="card">
      <h2>افزودن ادمین جدید</h2>
      <form method="post" action="{url_for('admin_create')}">
        <label>نام کاربری</label>
        <input type="text" name="username" required maxlength="40">
        <label>رمز عبور</label>
        <input type="password" name="password" required minlength="6">
        <label>نقش</label>
        <select name="role" onchange="document.getElementById('customPerms').style.display = this.value === 'custom' ? 'block' : 'none';">
          <option value="member">عضو (Member) — فقط مشاهده</option>
          <option value="operator">اپراتور (Operator) — همه چیز جز مدیریت ادمین‌ها</option>
          <option value="custom">سفارشی (Custom) — انتخاب دستی دسترسی‌ها</option>
          <option value="owner">مالک (Owner) — دسترسی کامل</option>
        </select>
        <div id="customPerms" style="display:none">
          <p class="small">دسترسی‌های حساب سفارشی:</p>
          <div class="perm-list">{perm_checkboxes}</div>
        </div>
        <button type="submit">ایجاد حساب</button>
      </form>
      <p class="small">
        نقش «سفارشی» به شما اجازه می‌دهد دقیقاً مشخص کنید این کاربر به کدام
        بخش‌ها (مثلاً فقط بن/آن‌بن کاربران، یا فقط مشاهده لاگ‌ها) دسترسی داشته باشد.
      </p>
    </div>
    """
    return render("مدیریت ادمین‌ها", body, admin)


@app.route("/admins/create", methods=["POST"])
@require_perm("admins_manage")
def admin_create():
    admin = current_admin()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "member")
    perms = request.form.getlist("perm") if role == "custom" else []

    error = None
    if not username or not re.match(r"^[A-Za-z0-9_.\-]{1,40}$", username):
        error = "نام کاربری نامعتبر است (فقط حروف انگلیسی، عدد، نقطه، خط تیره و آندرلاین)."
    elif len(password) < 6:
        error = "رمز عبور باید حداقل ۶ کاراکتر باشد."
    elif role not in ("owner", "operator", "member", "custom"):
        error = "نقش نامعتبر است."

    all_admins = load_admins()
    if error is None and any(a["username"] == username for a in all_admins):
        error = "این نام کاربری قبلاً استفاده شده است."

    if error:
        return render("خطا", f"<div class='card'><p class='bad'>{error}</p>"
                              f"<p><a href='{url_for('admins')}'>بازگشت</a></p></div>", admin), 400

    all_admins.append(
        {
            "username": username,
            "password_hash": generate_password_hash(password),
            "role": role,
            "permissions": [p for p in perms if p in PERMISSIONS],
            "created_at": _now_iso(),
            "created_by": admin["username"],
        }
    )
    save_admins(all_admins)
    append_audit(admin["username"], "create_admin", f"{username} ({role})")
    return redirect(url_for("admins"))


@app.route("/admins/delete", methods=["POST"])
@require_perm("admins_manage")
def admin_delete():
    admin = current_admin()
    username = request.form.get("username", "").strip()
    all_admins = load_admins()
    target = next((a for a in all_admins if a["username"] == username), None)
    if target is None:
        return redirect(url_for("admins"))
    if target["username"] == admin["username"]:
        return render("خطا", "<div class='card'><p class='bad'>نمی‌توانید حساب خودتان را حذف کنید.</p></div>", admin), 400
    if target.get("role") == "owner" and owners_count(all_admins) <= 1:
        return render("خطا", "<div class='card'><p class='bad'>حذف آخرین حساب مالک ممکن نیست.</p></div>", admin), 400
    new_admins = [a for a in all_admins if a["username"] != username]
    save_admins(new_admins)
    append_audit(admin["username"], "delete_admin", username)
    return redirect(url_for("admins"))


# ---------------------------------------------------------------------------
# Error handling — keep the panel from ever showing a raw stack trace
# ---------------------------------------------------------------------------
@app.errorhandler(404)
def not_found(e):
    admin = current_admin()
    return render("پیدا نشد", "<div class='card'><p class='warn'>این صفحه وجود ندارد.</p>"
                               f"<p><a href='{url_for('dashboard') if admin else url_for('login')}'>بازگشت</a></p></div>", admin), 404


@app.errorhandler(Exception)
def handle_exception(e):
    from werkzeug.exceptions import HTTPException

    if isinstance(e, HTTPException):
        return e
    app.logger.exception("Unhandled panel error")
    admin = current_admin()
    return render(
        "خطای غیرمنتظره",
        "<div class='card'><p class='bad'>یک خطای غیرمنتظره رخ داد. جزئیات فنی در panel.log "
        "ثبت شد.</p><p><a href='" + (url_for("dashboard") if admin else url_for("login")) + "'>بازگشت</a></p></div>",
        admin,
    ), 500


@app.route("/favicon.ico")
def favicon():
    return Response(status=204)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
bootstrap_admins_if_needed()

if __name__ == "__main__":
    import threading

    threading.Thread(target=_ban_enforcement_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
