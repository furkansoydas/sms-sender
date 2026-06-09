import os
from werkzeug.utils import secure_filename
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from dotenv import load_dotenv
from auth import register, login, generate_otp, send_otp_sms, verify_otp
from excel_reader import read_excel
from sms_sender import VerimorSMS
from database import (
    init_db, log_action, get_audit_log,
    enqueue_sms, get_queue, retry_queue_item,
    get_batches, get_batch_detail, get_stats,
    get_conversations, get_conversation_history, get_phone_meta
)
from queue_worker import start_worker, generate_batch_uid

load_dotenv()
init_db()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "sms-sender-secret-key-change-me")

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
EXCEL_PATH = os.path.join(UPLOAD_FOLDER, "data.xlsx")

_contacts_cache = None


def get_contacts():
    global _contacts_cache
    if _contacts_cache is None:
        if not os.path.exists(EXCEL_PATH):
            return []
        _contacts_cache = read_excel(EXCEL_PATH)
    return _contacts_cache


def clear_cache():
    global _contacts_cache
    _contacts_cache = None


def get_sms_client():
    username = os.getenv("VERIMOR_USERNAME")
    password = os.getenv("VERIMOR_PASSWORD")
    source = os.getenv("VERIMOR_SOURCE_ADDR")
    if all([username, password, source]):
        return VerimorSMS(username, password, source)
    return None


def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


# ── Auth Routes ──

@app.route("/")
def index():
    if "user" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login_page"))


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if not login(username, password):
            log_action(username, "login_failed", f"Hatalı şifre denemesi", ip=request.remote_addr, status="error")
            flash("Kullanıcı adı veya şifre hatalı.", "error")
            return redirect(url_for("login_page"))

        # Skip OTP if Verimor not configured (dev mode)
        sms_client = get_sms_client()
        if not sms_client:
            session["user"] = username
            log_action(username, "login_success", "OTP atlandı (dev mode)", ip=request.remote_addr)
            flash(f"Hoş geldin, {username}!", "success")
            return redirect(url_for("dashboard"))

        otp = generate_otp(username)
        from auth import _load_users
        user_phone = _load_users()[username]["phone"]
        send_otp_sms(user_phone, otp, sms_client)

        session["pending_user"] = username
        return redirect(url_for("otp_page"))

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register_page():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        phone = request.form.get("phone", "").strip()

        if not username or not password or not phone:
            flash("Tüm alanları doldurun.", "error")
            return redirect(url_for("register_page"))

        if register(username, password, phone):
            log_action(username, "register", f"Yeni kullanıcı kaydı (tel: {phone})", ip=request.remote_addr)
            flash("Kayıt başarılı! Giriş yapabilirsiniz.", "success")
            return redirect(url_for("login_page"))
        else:
            flash("Bu kullanıcı adı zaten kayıtlı.", "error")
            return redirect(url_for("register_page"))

    return render_template("register.html")


@app.route("/otp", methods=["GET", "POST"])
def otp_page():
    if "pending_user" not in session:
        return redirect(url_for("login_page"))

    if request.method == "POST":
        otp_input = request.form.get("otp", "").strip()
        username = session["pending_user"]

        if verify_otp(username, otp_input):
            session.pop("pending_user", None)
            session["user"] = username
            log_action(username, "login_success", "OTP doğrulandı", ip=request.remote_addr)
            flash(f"Hoş geldin, {username}!", "success")
            return redirect(url_for("dashboard"))
        else:
            log_action(username, "otp_failed", "Hatalı OTP kodu", ip=request.remote_addr, status="error")
            flash("OTP kodu hatalı veya süresi dolmuş.", "error")
            return redirect(url_for("otp_page"))

    return render_template("otp.html")


@app.route("/logout")
def logout_page():
    if session.get("user"):
        log_action(session["user"], "logout", "Oturum kapatıldı", ip=request.remote_addr)
    session.clear()
    flash("Çıkış yapıldı.", "success")
    return redirect(url_for("login_page"))


# ── Excel Import ──

@app.route("/import", methods=["GET", "POST"])
@login_required
def import_page():
    has_file = os.path.exists(EXCEL_PATH)
    file_info = None
    if has_file:
        import datetime
        stat = os.stat(EXCEL_PATH)
        size_kb = round(stat.st_size / 1024, 1)
        modified = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%d.%m.%Y %H:%M")
        contacts = get_contacts()
        file_info = {
            "size": f"{size_kb} KB",
            "modified": modified,
            "total": len(contacts),
            "sheets": len(set(c.sheet for c in contacts)) if contacts else 0,
        }

    if request.method == "POST":
        if "file" not in request.files:
            flash("Dosya seçilmedi.", "error")
            return redirect(url_for("import_page"))

        file = request.files["file"]
        if file.filename == "":
            flash("Dosya seçilmedi.", "error")
            return redirect(url_for("import_page"))

        if not file.filename.lower().endswith((".xlsx", ".xls")):
            flash("Sadece Excel dosyaları (.xlsx, .xls) desteklenir.", "error")
            return redirect(url_for("import_page"))

        file.save(EXCEL_PATH)
        clear_cache()

        # Validate the file
        try:
            contacts = get_contacts()
            flash(f"Excel başarıyla yüklendi! {len(contacts)} kayıt bulundu.", "success")
        except Exception as e:
            os.remove(EXCEL_PATH)
            clear_cache()
            flash(f"Excel dosyası okunamadı: {str(e)}", "error")

        return redirect(url_for("import_page"))

    return render_template("import.html", has_file=has_file, file_info=file_info)


# ── Dashboard ──

@app.route("/dashboard")
@login_required
def dashboard():
    contacts = get_contacts()
    blacklisted = [c for c in contacts if c.is_blacklisted]
    sendable = [c for c in contacts if not c.is_blacklisted and c.is_valid_phone]
    invalid = [c for c in contacts if not c.is_blacklisted and not c.is_valid_phone]

    sheets = {}
    for c in sendable:
        sheets[c.sheet] = sheets.get(c.sheet, 0) + 1

    # dedup phone count
    seen = set()
    unique = 0
    for c in sendable:
        for p in c.valid_phones:
            if p not in seen:
                seen.add(p)
                unique += 1

    return render_template("dashboard.html",
        user=session["user"],
        total=len(contacts),
        sendable=len(sendable),
        blacklisted_count=len(blacklisted),
        invalid=len(invalid),
        unique_phones=unique,
        sheets=sheets,
        blacklisted=blacklisted,
    )


# ── Contacts List ──

@app.route("/contacts")
@login_required
def contacts_page():
    contacts = get_contacts()
    sheet_filter = request.args.get("sheet", "all")
    show = request.args.get("show", "sendable")

    if show == "blacklisted":
        filtered = [c for c in contacts if c.is_blacklisted]
    elif show == "invalid":
        filtered = [c for c in contacts if not c.is_blacklisted and not c.is_valid_phone]
    else:
        filtered = [c for c in contacts if not c.is_blacklisted and c.is_valid_phone]

    if sheet_filter != "all":
        filtered = [c for c in filtered if c.sheet == sheet_filter]

    sheets = list(set(c.sheet for c in contacts))
    return render_template("contacts.html", contacts=filtered, sheets=sheets, current_sheet=sheet_filter, show=show)


# ── SMS Send ──

@app.route("/send", methods=["GET", "POST"])
@login_required
def send_page():
    contacts = get_contacts()
    sendable = [c for c in contacts if not c.is_blacklisted and c.is_valid_phone]
    sheets = list(set(c.sheet for c in sendable))

    # Build indexed contact list for selection
    indexed = []
    for i, c in enumerate(sendable):
        indexed.append({
            "idx": i,
            "firma": c.firma,
            "telefon": c.telefon,
            "ilgili_kisi": c.ilgili_kisi or "",
            "lokasyon": c.lokasyon or "",
            "sheet": c.sheet,
            "phones": ", ".join(c.valid_phones),
            "phone_count": len(c.valid_phones),
        })

    if request.method == "POST":
        message = request.form.get("message", "").strip()
        confirmed = request.form.get("confirmed") == "true"
        selected_indices = request.form.getlist("selected")

        if not message:
            flash("Mesaj boş olamaz.", "error")
            return redirect(url_for("send_page"))

        if not selected_indices:
            flash("En az bir alıcı seçmelisiniz.", "error")
            return redirect(url_for("send_page"))

        # Map indices back to contacts
        try:
            sel_set = set(int(x) for x in selected_indices)
        except ValueError:
            flash("Geçersiz seçim.", "error")
            return redirect(url_for("send_page"))

        targets = [sendable[i] for i in sel_set if i < len(sendable)]
        # Double-check: no blacklisted
        targets = [c for c in targets if not c.is_blacklisted]

        if not confirmed:
            # Preview — dedup phones
            seen = set()
            unique_phones = []
            for c in targets:
                for p in c.valid_phones:
                    if p not in seen:
                        seen.add(p)
                        unique_phones.append({"phone": p, "firma": c.firma})

            return render_template("send_preview.html",
                message=message,
                targets=unique_phones,
                count=len(unique_phones),
                selected_indices=",".join(selected_indices),
            )

        # Queue the SMS — worker will process it
        seen = set()
        phones = []
        firmas = []
        for c in targets:
            for p in c.valid_phones:
                if p not in seen:
                    seen.add(p)
                    phones.append(p)
                    firmas.append(c.firma)

        batch_uid = generate_batch_uid()
        enqueue_sms(batch_uid, session["user"], message, phones, firmas, "bulk")
        log_action(session["user"], "sms_queued",
                   f"Toplu SMS kuyruğa alındı: {len(phones)} numara (batch: {batch_uid})")
        flash(f"{len(phones)} numaralık SMS kuyruğa alındı. Queue sayfasından durumu izleyebilirsiniz.", "success")
        return redirect(url_for("queue_page"))

    return render_template("send.html", sheets=sheets, contacts=indexed, sendable_count=len(sendable))


# ── Manual SMS ──

def _normalize_phone(raw: str) -> str | None:
    """Normalize a phone to 90XXXXXXXXXX format. Returns None if invalid."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return None
    # Strip leading zeros / +90 prefix
    if digits.startswith("90") and len(digits) == 12:
        pass
    elif digits.startswith("0") and len(digits) == 11:
        digits = "90" + digits[1:]
    elif len(digits) == 10 and digits.startswith("5"):
        digits = "90" + digits
    else:
        return None
    # Validate Turkish mobile: must be 90 + 5XX + 7 digits = 12 total
    if len(digits) == 12 and digits.startswith("905"):
        return digits
    return None


@app.route("/manual", methods=["GET", "POST"])
@login_required
def manual_send_page():
    if request.method == "POST":
        raw_phones = request.form.get("phones", "").strip()
        message = request.form.get("message", "").strip()

        if not raw_phones:
            flash("En az bir telefon numarası girin.", "error")
            return redirect(url_for("manual_send_page"))

        if not message:
            flash("Mesaj boş olamaz.", "error")
            return redirect(url_for("manual_send_page"))

        # Split by newline, comma or semicolon
        import re
        candidates = [p.strip() for p in re.split(r"[\n,;]+", raw_phones) if p.strip()]

        valid, invalid = [], []
        for raw in candidates:
            norm = _normalize_phone(raw)
            if norm:
                valid.append(norm)
            else:
                invalid.append(raw)

        # Dedup
        seen = set()
        unique = []
        for p in valid:
            if p not in seen:
                seen.add(p)
                unique.append(p)

        # Check blacklist
        contacts = get_contacts()
        blacklist_phones = set()
        for c in contacts:
            if c.is_blacklisted:
                for p in c.valid_phones:
                    blacklist_phones.add(p)

        blocked = [p for p in unique if p in blacklist_phones]
        sendable_phones = [p for p in unique if p not in blacklist_phones]

        if not sendable_phones:
            flash("Geçerli numara bulunamadı veya tümü kara listede.", "error")
            return redirect(url_for("manual_send_page"))

        # Queue
        batch_uid = generate_batch_uid()
        firmas = ["Manuel"] * len(sendable_phones)
        enqueue_sms(batch_uid, session["user"], message, sendable_phones, firmas, "manual")

        details = f"Manuel SMS kuyruğa alındı: {len(sendable_phones)} numara"
        if blocked:
            details += f", {len(blocked)} kara liste atlandı"
        if invalid:
            details += f", {len(invalid)} geçersiz atlandı"
        log_action(session["user"], "sms_queued_manual", f"{details} (batch: {batch_uid})")

        msg = f"{len(sendable_phones)} numaralık SMS kuyruğa alındı"
        if blocked:
            msg += f" (Kara liste: {len(blocked)} atlandı)"
        if invalid:
            msg += f" (Geçersiz: {len(invalid)} atlandı)"
        flash(msg, "success")
        return redirect(url_for("queue_page"))

    return render_template("manual_send.html")


# ── Queue Page ──

@app.route("/queue")
@login_required
def queue_page():
    queue_items = get_queue(limit=200)
    stats = get_stats()
    return render_template("queue.html", items=queue_items, stats=stats)


@app.route("/queue/retry/<int:queue_id>")
@login_required
def queue_retry(queue_id):
    retry_queue_item(queue_id)
    log_action(session["user"], "queue_retry", f"Queue ID {queue_id} yeniden denemeye alındı")
    flash("Queue işi yeniden denenecek.", "success")
    return redirect(url_for("queue_page"))


# ── SMS History ──

@app.route("/history")
@login_required
def history_page():
    batches = get_batches(limit=200)
    stats = get_stats()
    return render_template("history.html", batches=batches, stats=stats)


@app.route("/history/<batch_uid>")
@login_required
def history_detail(batch_uid):
    batch, logs = get_batch_detail(batch_uid)
    if not batch:
        flash("Batch bulunamadı.", "error")
        return redirect(url_for("history_page"))
    return render_template("history_detail.html", batch=batch, logs=logs)


# ── Messages (WhatsApp-style chat history) ──

@app.route("/messages")
@login_required
def messages_page():
    search = request.args.get("q", "").strip()
    selected_phone = request.args.get("phone", "")

    conversations = get_conversations(search=search)

    conversation = None
    phone_meta = None
    if selected_phone:
        conversation = get_conversation_history(selected_phone)
        phone_meta = get_phone_meta(selected_phone)

    return render_template("messages.html",
        conversations=conversations,
        conversation=conversation,
        selected_phone=selected_phone,
        phone_meta=phone_meta,
        search=search,
    )


@app.route("/messages/send", methods=["POST"])
@login_required
def messages_send():
    phone = request.form.get("phone", "").strip()
    message = request.form.get("message", "").strip()

    if not phone or not message:
        flash("Telefon ve mesaj zorunlu.", "error")
        return redirect(url_for("messages_page", phone=phone))

    norm = _normalize_phone(phone)
    if not norm:
        flash("Geçersiz telefon numarası.", "error")
        return redirect(url_for("messages_page", phone=phone))

    # Check blacklist
    contacts = get_contacts()
    for c in contacts:
        if c.is_blacklisted and norm in c.valid_phones:
            flash("Bu numara kara listede, mesaj gönderilemez.", "error")
            return redirect(url_for("messages_page", phone=phone))

    # Get firma from meta or contacts
    meta = get_phone_meta(norm)
    firma = (meta and meta.get("firma")) or "-"
    for c in contacts:
        if norm in c.valid_phones:
            firma = c.firma
            break

    batch_uid = generate_batch_uid()
    enqueue_sms(batch_uid, session["user"], message, [norm], [firma], "chat")
    log_action(session["user"], "sms_queued_chat",
               f"Sohbet üzerinden SMS kuyruğa alındı: {norm} (batch: {batch_uid})")

    flash("Mesaj kuyruğa alındı, gönderiliyor...", "success")
    return redirect(url_for("messages_page", phone=norm))


# ── Audit Log ──

@app.route("/audit")
@login_required
def audit_page():
    user_filter = request.args.get("user", "")
    action_filter = request.args.get("action", "")
    logs = get_audit_log(limit=300,
                         username=user_filter or None,
                         action=action_filter or None)
    return render_template("audit.html", logs=logs, user_filter=user_filter, action_filter=action_filter)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5050))
    # Start background queue worker
    start_worker(get_sms_client)
    print(f"\n  SMS Sender çalışıyor: http://localhost:{port}\n")
    app.run(debug=True, port=port, host="0.0.0.0", use_reloader=False)
