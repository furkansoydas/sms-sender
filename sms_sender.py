import json
import requests
from models import Contact


class VerimorSMS:
    API_URL = "http://sms.verimor.com.tr/v2/send.json"

    def __init__(self, username: str, password: str, source_addr: str):
        self.username = username
        self.password = password
        self.source_addr = source_addr

    def send_single(self, contact: Contact, message: str) -> dict:
        payload = {
            "username": self.username,
            "password": self.password,
            "source_addr": self.source_addr,
            "messages": [
                {
                    "msg": message,
                    "dest": contact.formatted_phone,
                }
            ],
        }
        resp = requests.post(self.API_URL, json=payload, timeout=30)
        return {"phone": contact.formatted_phone, "firma": contact.firma, "status": resp.status_code, "response": resp.text}

    def send_bulk(self, contacts: list[Contact], message: str, dry_run: bool = True) -> list[dict]:
        results = []
        all_phones = []
        for c in contacts:
            for phone in c.valid_phones:
                all_phones.append({"phone": phone, "firma": c.firma})

        # Tekrarlayan numaraları kaldır
        seen = set()
        unique_phones = []
        for entry in all_phones:
            if entry["phone"] not in seen:
                seen.add(entry["phone"])
                unique_phones.append(entry)

        if dry_run:
            print(f"\n[DRY RUN] {len(unique_phones)} benzersiz numaraya SMS gönderilecek (gönderim yapılmadı)")
            for entry in unique_phones[:10]:
                print(f"  {entry['firma']} -> {entry['phone']}")
            if len(unique_phones) > 10:
                print(f"  ... ve {len(unique_phones) - 10} numara daha")
            return [{"phone": e["phone"], "firma": e["firma"], "status": "dry_run"} for e in unique_phones]

        dest_list = [e["phone"] for e in unique_phones]
        payload = {
            "username": self.username,
            "password": self.password,
            "source_addr": self.source_addr,
            "messages": [
                {
                    "msg": message,
                    "dest": ",".join(dest_list),
                }
            ],
        }
        resp = requests.post(self.API_URL, json=payload, timeout=60)

        for entry in unique_phones:
            results.append({
                "phone": entry["phone"],
                "firma": entry["firma"],
                "status": resp.status_code,
                "response": resp.text,
            })

        return results
