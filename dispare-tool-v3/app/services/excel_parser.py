"""
Гибкий парсер Excel-файлов поставщиков и собственных прайсов.
Автоматически ищет нужные колонки по ключевым словам в заголовках.
Умеет работать с файлами без заголовков.
"""
import re
import pandas as pd
from io import BytesIO
from typing import Optional


COLUMN_HINTS = {
    "part_number": [
        "артикул", "part", "number", "partnumber", "part_number", "парт",
        "номер", "oem", "oe", "каталожный", "код", "code", "item", "sku",
        "артикул производителя", "ref", "reference",
    ],
    "brand": [
        "бренд", "brand", "производитель", "марка", "manufacturer", "make",
        "vendor", "поставщик бренда",
    ],
    "description": [
        "описание", "description", "наименование", "название", "name",
        "title", "товар", "item_name",
    ],
    "price": [
        "цена", "price", "cost", "стоимость", "прайс", "unit_price",
        "цена за шт", "price_usd", "price_eur", "таргет",
    ],
    "quantity": [
        "количество", "qty", "quantity", "наличие", "stock", "остаток",
        "кол-во", "кол",
    ],
}


def _normalize_header(h: str) -> str:
    return str(h).strip().lower().replace("\n", " ")


def _match_column(header: str, hints: list[str]) -> bool:
    h = _normalize_header(header)
    return any(hint in h for hint in hints)


def _looks_like_header(row_values) -> bool:
    """Проверяет, похожа ли строка на заголовок (содержит текст, а не числа)."""
    text_count = 0
    for v in row_values:
        s = str(v).strip()
        if not s or s.lower() == 'nan':
            continue
        try:
            float(s.replace(",", ".").replace(" ", ""))
        except ValueError:
            text_count += 1
    return text_count >= 2


def _is_integer_column(series) -> bool:
    """Проверяет, содержит ли колонка только целые числа (для определения колонки количества)."""
    for v in series.dropna().head(10):
        try:
            f = float(str(v).replace(",", ".").replace(" ", ""))
            if f != int(f) or f > 10000:
                return False
        except (ValueError, TypeError):
            return False
    return True


def detect_columns(df: pd.DataFrame) -> dict[str, Optional[str]]:
    result = {}
    for field, hints in COLUMN_HINTS.items():
        matched = None
        for col in df.columns:
            if _match_column(str(col), hints):
                matched = col
                break
        result[field] = matched
    return result


def parse_supplier_excel(file_bytes: bytes, filename: str = "") -> dict:
    ext = filename.lower().split(".")[-1] if filename else "xlsx"
    try:
        if ext == "csv":
            df = pd.read_csv(BytesIO(file_bytes), dtype=str)
        else:
            df = pd.read_excel(BytesIO(file_bytes), dtype=str, engine="openpyxl")
    except Exception as e:
        return {"rows": [], "columns_detected": {}, "total": 0, "errors": [str(e)]}

    df = df.dropna(how="all").reset_index(drop=True)
    columns = detect_columns(df)

    if not columns.get("part_number"):
        for skip in range(1, 5):
            try:
                if ext == "csv":
                    df2 = pd.read_csv(BytesIO(file_bytes), skiprows=skip, dtype=str)
                else:
                    df2 = pd.read_excel(BytesIO(file_bytes), skiprows=skip, dtype=str, engine="openpyxl")
                df2 = df2.dropna(how="all").reset_index(drop=True)
                columns2 = detect_columns(df2)
                if columns2.get("part_number"):
                    df = df2
                    columns = columns2
                    break
            except Exception:
                continue

    errors = []
    if not columns.get("part_number"):
        errors.append(
            "Не удалось определить колонку с артикулом. "
            f"Заголовки: {list(df.columns[:10])}"
        )
        return {"rows": [], "columns_detected": columns, "total": 0, "errors": errors}

    rows = []
    for _, row in df.iterrows():
        pn_raw = str(row.get(columns["part_number"], "")).strip()
        if not pn_raw or pn_raw.lower() == "nan":
            continue

        price_val = 0.0
        if columns.get("price"):
            try:
                p = str(row[columns["price"]]).replace(",", ".").replace(" ", "")
                price_val = float(p)
            except (ValueError, TypeError):
                pass

        rows.append({
            "part_number": pn_raw,
            "brand": str(row.get(columns.get("brand", ""), "")).strip() if columns.get("brand") else "",
            "description": str(row.get(columns.get("description", ""), "")).strip() if columns.get("description") else "",
            "price": price_val,
        })

    return {
        "rows": rows,
        "columns_detected": {k: v for k, v in columns.items() if v},
        "total": len(rows),
        "errors": errors,
    }


def parse_our_prices_excel(file_bytes: bytes, filename: str = "") -> dict:
    """
    Парсит Excel с нашими ценами. Поддерживает:
    - 4 колонки: артикул, цена1, цена2, цена3
    - 5 колонок: артикул, количество, цена1, цена2, цена3 (пропускает количество)
    - С заголовками и без заголовков
    """
    ext = filename.lower().split(".")[-1] if filename else "xlsx"
    try:
        if ext == "csv":
            df = pd.read_csv(BytesIO(file_bytes), dtype=str)
        else:
            df = pd.read_excel(BytesIO(file_bytes), dtype=str, engine="openpyxl")
    except Exception as e:
        return {"rows": [], "total": 0, "errors": [str(e)]}

    df = df.dropna(how="all").reset_index(drop=True)

    # Проверяем, есть ли заголовки
    first_row = [str(df.columns[i]) for i in range(min(5, len(df.columns)))]
    has_headers = _looks_like_header(first_row)

    if not has_headers:
        # Нет заголовков — перечитываем без header
        try:
            if ext == "csv":
                df = pd.read_csv(BytesIO(file_bytes), dtype=str, header=None)
            else:
                df = pd.read_excel(BytesIO(file_bytes), dtype=str, engine="openpyxl", header=None)
            df = df.dropna(how="all").reset_index(drop=True)
        except Exception as e:
            return {"rows": [], "total": 0, "errors": [str(e)]}

    ncols = len(df.columns)

    # Определяем колонки
    if ncols >= 5:
        # 5+ колонок: проверяем, вторая — это количество?
        col_b = df.iloc[:, 1]
        if _is_integer_column(col_b):
            # Формат: артикул, количество, цена1, цена2, цена3
            pn_idx, price_indices = 0, [2, 3, 4]
        else:
            # Берём первую как артикул, следующие 3 как цены
            pn_idx, price_indices = 0, [1, 2, 3]
    elif ncols >= 4:
        pn_idx, price_indices = 0, [1, 2, 3]
    elif ncols >= 2:
        pn_idx = 0
        price_indices = list(range(1, min(4, ncols)))
    else:
        return {"rows": [], "total": 0, "errors": ["Слишком мало колонок в файле"]}

    rows = []
    for _, row in df.iterrows():
        pn = str(row.iloc[pn_idx]).strip()
        if not pn or pn.lower() == "nan":
            continue

        # Пропускаем строку заголовков если она попала в данные
        try:
            float(str(row.iloc[price_indices[0]]).replace(",", ".").replace(" ", ""))
        except (ValueError, TypeError):
            continue

        prices = []
        for idx in price_indices:
            try:
                v = str(row.iloc[idx]).replace(",", ".").replace(" ", "")
                prices.append(float(v))
            except (ValueError, TypeError):
                prices.append(0.0)

        while len(prices) < 3:
            prices.append(0.0)

        rows.append({
            "part_number": pn,
            "price_order": round(prices[0], 2),
            "price_3pl": round(prices[1], 2),
            "price_3pl_emex": round(prices[2], 2),
        })

    return {"rows": rows, "total": len(rows), "errors": []}
