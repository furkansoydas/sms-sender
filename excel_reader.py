import pandas as pd
from openpyxl import load_workbook
from models import Contact

RED_FILL = "FFFF0000"


def _get_red_rows(wb, sheet_name: str) -> set[int]:
    if sheet_name not in wb.sheetnames:
        return set()
    ws = wb[sheet_name]
    red_rows = set()
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row):
        for cell in row:
            if cell.fill and cell.fill.fgColor:
                try:
                    if str(cell.fill.fgColor.rgb) == RED_FILL:
                        red_rows.add(cell.row)
                except (TypeError, AttributeError):
                    pass
    return red_rows


def read_excel(file_path: str) -> list[Contact]:
    wb = load_workbook(file_path)
    sheets = pd.read_excel(file_path, sheet_name=None, dtype=str)
    contacts = []

    for sheet_name, df in sheets.items():
        df.columns = [str(c).strip() for c in df.columns]
        df = df.fillna("")
        red_rows = _get_red_rows(wb, sheet_name)

        if sheet_name == "Günlük ilanlar":
            contacts += _parse_gunluk_ilanlar(df, sheet_name, red_rows)
        elif sheet_name == "Büyük Firmalar":
            contacts += _parse_buyuk_firmalar(df, sheet_name, red_rows)
        elif sheet_name == "İŞİN OLSUN":
            contacts += _parse_isin_olsun(df, sheet_name, red_rows)

    wb.close()
    return contacts


def _is_red(df_index: int, red_rows: set[int]) -> bool:
    excel_row = df_index + 2
    return excel_row in red_rows


def _parse_gunluk_ilanlar(df: pd.DataFrame, sheet_name: str, red_rows: set[int]) -> list[Contact]:
    contacts = []
    cols = df.columns.tolist()
    for idx, row in df.iterrows():
        firma = str(row.get(cols[1], "")).strip()
        telefon = str(row.get(cols[3], "")).strip()
        ilgili_kisi = str(row.get(cols[2], "")).strip()
        departman = str(row.get(cols[4], "")).strip()
        lokasyon = str(row.get(cols[5], "")).strip()

        if not firma or firma == "FİRMA İSMİ" or not telefon:
            continue

        contacts.append(Contact(
            firma=firma,
            telefon=telefon,
            ilgili_kisi=ilgili_kisi,
            departman=departman,
            lokasyon=lokasyon,
            sheet=sheet_name,
            is_blacklisted=_is_red(idx, red_rows),
        ))
    return contacts


def _parse_buyuk_firmalar(df: pd.DataFrame, sheet_name: str, red_rows: set[int]) -> list[Contact]:
    contacts = []
    cols = df.columns.tolist()
    for idx, row in df.iterrows():
        firma = str(row.get(cols[0], "")).strip()
        email = str(row.get(cols[1], "")).strip()
        telefon = str(row.get(cols[2], "")).strip()

        if not firma or firma == "FİRMA İSMİ" or not telefon:
            continue

        contacts.append(Contact(
            firma=firma,
            telefon=telefon,
            email=email,
            sheet=sheet_name,
            is_blacklisted=_is_red(idx, red_rows),
        ))
    return contacts


def _parse_isin_olsun(df: pd.DataFrame, sheet_name: str, red_rows: set[int]) -> list[Contact]:
    contacts = []
    for idx, row in df.iterrows():
        firma = str(row.get("FİRMA İSMİ", "")).strip()
        telefon = str(row.get("İLETİŞİM", "")).strip()

        if not firma or not telefon:
            continue

        contacts.append(Contact(
            firma=firma,
            telefon=telefon,
            sheet=sheet_name,
            is_blacklisted=_is_red(idx, red_rows),
        ))
    return contacts
