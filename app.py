import os
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from dotenv import load_dotenv
from auth import register, login, generate_otp, send_otp_sms, verify_otp
from excel_reader import read_excel
from sms_sender import VerimorSMS

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "sms-sender-secret-key-change-me")

EXCEL_PATH = os.getenv("EXCEL_PATH", "/Users/mfeteknolojia.s/Desktop/data.xlsx")

_contacts_cache = None


def get_contacts():
    global _contacts_cache
    if _contacts_cache is None:
        _contacts_cache = read_excel(EXCEL_PATH)
    return _contacts_cache


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
            flash("Kullanıcı adı veya şifre hatalı.", "error")
            return redirect(url_for("login_page"))

        otp = generate_otp(username)
        from auth import _load_users
        user_phone = _load_users()[username]["phone"]
        sms_client = get_sms_client()
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
            flash(f"Hoş geldin, {username}!", "success")
            return redirect(url_for("dashboard"))
        else:
            flash("OTP kodu hatalı veya süresi dolmuş.", "error")
            return redirect(url_for("otp_page"))

    return render_template("otp.html")


@app.route("/logout")
def logout_page():
    session.clear()
    flash("Çıkış yapıldı.", "success")
    return redirect(url_for("login_page"))


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

    if request.method == "POST":
        message = request.form.get("message", "").strip()
        selected_sheet = request.form.get("sheet", "all")
        confirmed = request.form.get("confirmed") == "true"

        if not message:
            flash("Mesaj boş olamaz.", "error")
            return redirect(url_for("send_page"))

        if selected_sheet == "all":
            targets = sendable
        else:
            targets = [c for c in sendable if c.sheet == selected_sheet]

        # Double-check: no blacklisted
        targets = [c for c in targets if not c.is_blacklisted]

        if not confirmed:
            # Preview
            seen = set()
            unique_phones = []
            for c in targets:
                for p in c.valid_phones:
                    if p not in seen:
                        seen.add(p)
                        unique_phones.append({"phone": p, "firma": c.firma})

            return render_template("send_preview.html",
                message=message,
                sheet=selected_sheet,
                targets=unique_phones,
                count=len(unique_phones),
            )

        # Actual send
        sms_client = get_sms_client()
        if not sms_client:
            flash("Verimor API ayarları eksik. .env dosyasını kontrol edin.", "error")
            return redirect(url_for("send_page"))

        results = sms_client.send_bulk(targets, message, dry_run=False)
        success = sum(1 for r in results if r["status"] == 200)
        flash(f"SMS gönderildi: {success}/{len(results)} başarılı", "success")
        return redirect(url_for("dashboard"))

    return render_template("send.html", sheets=sheets, sendable_count=len(sendable))


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5050))
    print(f"\n  SMS Sender çalışıyor: http://localhost:{port}\n")
    app.run(debug=True, port=port, host="0.0.0.0")
