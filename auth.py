import hashlib
import json
import os
import random
import time

USERS_FILE = "users.json"
SESSION_FILE = ".session"
OTP_EXPIRY = 300  # 5 dakika


def _load_users() -> dict:
    if not os.path.exists(USERS_FILE):
        return {}
    with open(USERS_FILE, "r") as f:
        return json.load(f)


def _save_users(users: dict):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def register(username: str, password: str, phone: str, role: str = "user", active: bool = True) -> bool:
    users = _load_users()
    if username in users:
        print("Bu kullanıcı adı zaten kayıtlı.")
        return False
    users[username] = {
        "password": _hash_password(password),
        "phone": phone,
        "role": role,
        "active": active,
    }
    _save_users(users)
    print(f"Kayıt başarılı: {username}")
    return True


# ── Admin / kullanıcı yönetimi ──

def ensure_admin(password: str, phone: str = "") -> None:
    """admin kullanıcısı yoksa oluşturur; varsa şifresini .env ile senkron tutar."""
    users = _load_users()
    admin = users.get("admin")
    hashed = _hash_password(password)
    if admin is None:
        users["admin"] = {
            "password": hashed,
            "phone": phone,
            "role": "admin",
            "active": True,
        }
    else:
        admin["password"] = hashed
        admin["role"] = "admin"
        admin["active"] = True
    _save_users(users)


def list_users() -> list[dict]:
    users = _load_users()
    result = []
    for name, data in users.items():
        result.append({
            "username": name,
            "phone": data.get("phone", ""),
            "role": data.get("role", "user"),
            "active": data.get("active", True),
        })
    return result


def get_user(username: str) -> dict | None:
    return _load_users().get(username)


def set_active(username: str, active: bool) -> bool:
    users = _load_users()
    if username not in users or users[username].get("role") == "admin":
        return False
    users[username]["active"] = active
    _save_users(users)
    return True


def delete_user(username: str) -> bool:
    users = _load_users()
    if username not in users or users[username].get("role") == "admin":
        return False
    del users[username]
    _save_users(users)
    return True


def generate_otp(username: str) -> str | None:
    users = _load_users()
    if username not in users:
        return None
    otp = str(random.randint(100000, 999999))
    users[username]["otp"] = otp
    users[username]["otp_time"] = time.time()
    _save_users(users)
    return otp


def send_otp_sms(phone: str, otp: str, sms_client=None) -> bool:
    if sms_client is None:
        print(f"\n{'='*40}")
        print(f"  OTP KODU: {otp}")
        print(f"  (Gerçek SMS gönderimi .env ayarlandığında aktif olacak)")
        print(f"{'='*40}\n")
        return True

    from models import Contact
    temp_contact = Contact(firma="OTP", telefon=phone)
    result = sms_client.send_single(temp_contact, f"Dogrulama kodunuz: {otp}")
    return result.get("status") == 200


def verify_otp(username: str, otp_input: str) -> bool:
    users = _load_users()
    user = users.get(username)
    if not user or "otp" not in user:
        return False
    if time.time() - user.get("otp_time", 0) > OTP_EXPIRY:
        print("OTP süresi dolmuş. Yeni kod isteyin.")
        return False
    if user["otp"] != otp_input:
        return False
    del users[username]["otp"]
    del users[username]["otp_time"]
    _save_users(users)
    return True


def login(username: str, password: str) -> bool:
    users = _load_users()
    user = users.get(username)
    if not user:
        print("Kullanıcı bulunamadı.")
        return False
    if user["password"] != _hash_password(password):
        print("Şifre hatalı.")
        return False
    if not user.get("active", True):
        print("Bu hesabın erişimi kapalı.")
        return False
    return True


def save_session(username: str):
    with open(SESSION_FILE, "w") as f:
        json.dump({"username": username, "time": time.time()}, f)


def load_session() -> str | None:
    if not os.path.exists(SESSION_FILE):
        return None
    with open(SESSION_FILE, "r") as f:
        data = json.load(f)
    if time.time() - data.get("time", 0) > 86400:  # 24 saat
        os.remove(SESSION_FILE)
        return None
    return data.get("username")


def logout():
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
        print("Çıkış yapıldı.")
