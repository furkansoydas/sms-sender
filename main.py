import os
import sys
from dotenv import load_dotenv
from auth import register, login, generate_otp, send_otp_sms, verify_otp, save_session, load_session, logout
from excel_reader import read_excel
from sms_sender import VerimorSMS

load_dotenv()

RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BOLD = "\033[1m"
RESET = "\033[0m"


def auth_flow() -> str:
    session_user = load_session()
    if session_user:
        print(f"{GREEN}Aktif oturum: {session_user}{RESET}")
        return session_user

    print(f"\n{BOLD}=== SMS Sender - Giriş ==={RESET}")
    print("1. Giriş yap")
    print("2. Kayıt ol")
    choice = input("\nSeçim: ").strip()

    if choice == "2":
        username = input("Kullanıcı adı: ").strip()
        password = input("Şifre: ").strip()
        phone = input("Telefon (5XXXXXXXXX): ").strip()
        if not register(username, password, phone):
            sys.exit(1)

    elif choice != "1":
        print("Geçersiz seçim.")
        sys.exit(1)

    if choice == "1":
        username = input("Kullanıcı adı: ").strip()
    password_input = input("Şifre: ").strip()

    if not login(username, password_input):
        sys.exit(1)

    otp = generate_otp(username)
    if not otp:
        print("OTP oluşturulamadı.")
        sys.exit(1)

    sms_client = _get_sms_client()
    from auth import _load_users
    user_phone = _load_users()[username]["phone"]
    send_otp_sms(user_phone, otp, sms_client)

    otp_input = input("OTP kodunu girin: ").strip()
    if not verify_otp(username, otp_input):
        print(f"{RED}OTP doğrulama başarısız.{RESET}")
        sys.exit(1)

    print(f"{GREEN}Giriş başarılı!{RESET}")
    save_session(username)
    return username


def _get_sms_client() -> VerimorSMS | None:
    username = os.getenv("VERIMOR_USERNAME")
    password = os.getenv("VERIMOR_PASSWORD")
    source = os.getenv("VERIMOR_SOURCE_ADDR")
    if all([username, password, source]):
        return VerimorSMS(username, password, source)
    return None


def show_blacklist_warning(blacklisted: list):
    if not blacklisted:
        return
    print(f"\n{RED}{BOLD}{'='*60}")
    print(f"  ⛔ DİKKAT: {len(blacklisted)} KİŞİ SMS İSTEMİYOR (KIRMIZI İŞARETLİ)")
    print(f"  Bu kişiler otomatik olarak gönderim listesinden çıkarıldı.")
    print(f"{'='*60}{RESET}")
    for c in blacklisted[:15]:
        print(f"  {RED}✗ {c.firma} ({c.telefon}){RESET}")
    if len(blacklisted) > 15:
        print(f"  {RED}... ve {len(blacklisted) - 15} kişi daha{RESET}")
    print()


def main():
    user = auth_flow()

    excel_path = sys.argv[1] if len(sys.argv) > 1 else "data.xlsx"
    if not os.path.exists(excel_path):
        print(f"Hata: '{excel_path}' dosyası bulunamadı.")
        sys.exit(1)

    print(f"\nExcel okunuyor: {excel_path}")
    contacts = read_excel(excel_path)
    print(f"Toplam {len(contacts)} kayıt bulundu.")

    blacklisted = [c for c in contacts if c.is_blacklisted]
    sendable = [c for c in contacts if not c.is_blacklisted and c.is_valid_phone]

    show_blacklist_warning(blacklisted)

    valid_contacts = sendable
    invalid_contacts = [c for c in contacts if not c.is_blacklisted and not c.is_valid_phone]
    print(f"{GREEN}Gönderilebilir: {len(valid_contacts)}{RESET}, Geçersiz numara: {len(invalid_contacts)}, {RED}Kara liste: {len(blacklisted)}{RESET}")

    sheet_counts = {}
    for c in valid_contacts:
        sheet_counts[c.sheet] = sheet_counts.get(c.sheet, 0) + 1
    print("\nSheet bazlı dağılım:")
    for sheet, count in sheet_counts.items():
        print(f"  {sheet}: {count} kişi")

    if not valid_contacts:
        print("Gönderilebilir kişi bulunamadı.")
        sys.exit(0)

    print(f"\n{BOLD}--- SMS Mesajı ---{RESET}")
    message = input("Göndermek istediğiniz mesajı yazın: ").strip()
    if not message:
        print("Mesaj boş olamaz.")
        sys.exit(1)

    print("\nHangi sheet'teki kişilere göndermek istiyorsunuz?")
    sheets = list(sheet_counts.keys())
    for i, s in enumerate(sheets, 1):
        print(f"  {i}. {s} ({sheet_counts[s]} kişi)")
    print(f"  {len(sheets) + 1}. Tümü ({len(valid_contacts)} kişi)")

    choice = input("\nSeçiminiz: ").strip()
    try:
        choice = int(choice)
        if choice == len(sheets) + 1:
            selected = valid_contacts
        else:
            selected_sheet = sheets[choice - 1]
            selected = [c for c in valid_contacts if c.sheet == selected_sheet]
    except (ValueError, IndexError):
        print("Geçersiz seçim.")
        sys.exit(1)

    sms_client = _get_sms_client()
    if not sms_client:
        print(f"{RED}Hata: .env dosyasında VERIMOR_USERNAME, VERIMOR_PASSWORD ve VERIMOR_SOURCE_ADDR tanımlı olmalı.{RESET}")
        sys.exit(1)

    print(f"\n{BOLD}[DRY RUN] Önizleme:{RESET}")
    sms_client.send_bulk(selected, message, dry_run=True)

    print(f"\n{YELLOW}Mesaj: \"{message}\"{RESET}")
    print(f"Alıcı sayısı: {len(selected)}")
    print(f"{RED}Kara listedeki kişiler dahil DEĞİL.{RESET}")
    confirm = input(f"\nGöndermek için '{GREEN}EVET{RESET}' yazın: ").strip()

    if confirm != "EVET":
        print("Gönderim iptal edildi.")
        sys.exit(0)

    print("\nSMS gönderiliyor...")
    results = sms_client.send_bulk(selected, message, dry_run=False)
    success = sum(1 for r in results if r["status"] == 200)
    print(f"\n{GREEN}Sonuç: {success}/{len(results)} başarılı{RESET}")

    if results:
        print(f"API yanıtı: {results[0].get('response', 'N/A')}")


if __name__ == "__main__":
    main()
