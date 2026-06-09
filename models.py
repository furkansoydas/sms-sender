from dataclasses import dataclass


@dataclass
class Contact:
    firma: str
    telefon: str
    departman: str = ""
    lokasyon: str = ""
    ilgili_kisi: str = ""
    email: str = ""
    sheet: str = ""
    is_blacklisted: bool = False

    @property
    def all_phones(self) -> list[str]:
        parts = self.telefon.replace("/", ",").split(",")
        result = []
        for part in parts:
            raw = part.strip().replace(" ", "").replace("(", "").replace(")", "").replace("-", "").replace("+", "")
            if not raw:
                continue
            if raw.startswith("90") and len(raw) == 12:
                result.append(raw)
            elif raw.startswith("0") and len(raw) == 11:
                result.append("90" + raw[1:])
            elif raw.startswith("5") and len(raw) == 10:
                result.append("90" + raw)
            else:
                result.append(raw)
        return result

    @property
    def formatted_phone(self) -> str:
        valid = [p for p in self.all_phones if self._is_mobile(p)]
        return valid[0] if valid else (self.all_phones[0] if self.all_phones else "")

    @property
    def valid_phones(self) -> list[str]:
        return [p for p in self.all_phones if self._is_mobile(p)]

    @property
    def is_valid_phone(self) -> bool:
        return len(self.valid_phones) > 0

    @staticmethod
    def _is_mobile(phone: str) -> bool:
        return phone.startswith("905") and len(phone) == 12
