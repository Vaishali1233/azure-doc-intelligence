import streamlit as st
import json
import re
from typing import Dict, List, Any, Optional
from azure.core.credentials import AzureKeyCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest

st.set_page_config(page_title="Doc Intel", layout="wide")
st.title("Azure AI Document Intelligence – Smart Invoice Parser")


with st.sidebar:
    st.header("Azure Credentials")
    endpoint = st.text_input("Endpoint", type="password")
    api_key = st.text_input("API Key", type="password")


col1, col2 = st.columns([3, 1])
with col1:
    uploaded_file = st.file_uploader("Upload PDF invoice", type="pdf")
with col2:
    field_name = st.text_input("Field Name", placeholder="Enter a field to search")


def get_center_x(polygon):
    if not polygon:
        return None
    xs = [p.x for p in polygon] if hasattr(polygon[0], 'x') else polygon[0::2]
    return sum(xs) / len(xs)


def extract_tables_with_key_as_header(result) -> List[Dict[str, Any]]:
    tables_data = []
    for table in getattr(result, "tables", []):
        if table.row_count < 2:
            continue

        header_cells = sorted(
            [c for c in table.cells if c.row_index == 0],
            key=lambda x: x.column_index
        )
        headers = []
        for cell in header_cells:
            poly = cell.bounding_regions[0].polygon if cell.bounding_regions else None
            headers.append({
                "name": (cell.content or "").strip(),
                "cx": get_center_x(poly),
                "col_idx": cell.column_index
            })

        data_rows = []
        for row_idx in range(1, table.row_count):
            row_cells = sorted(
                [c for c in table.cells if c.row_index == row_idx],
                key=lambda x: x.column_index
            )
            row_dict = {}
            for cell in row_cells:
                poly = cell.bounding_regions[0].polygon if cell.bounding_regions else None
                cx = get_center_x(poly)
                if cx is None:
                    continue
                closest = min(headers, key=lambda h: abs(h["cx"] - cx) if h["cx"] else 999)
                key = closest["name"]
                value = (cell.content or "").strip()
                if value:
                    row_dict[key] = value
            if row_dict:
                data_rows.append(row_dict)

        if data_rows:
            tables_data.append({
                "headers": [h["name"] for h in headers],
                "rows": data_rows
            })
    return tables_data


def extract_transaction_fees(result) -> Dict[str, float]:
    fees = {}
    pattern = re.compile(r"Transaction Fee [TG][1-6]", re.IGNORECASE)
    
    for table in getattr(result, "tables", []):
        if table.row_count < 2:
            continue
        header_row = sorted([c for c in table.cells if c.row_index == 0], key=lambda x: x.column_index)
        value_row = sorted([c for c in table.cells if c.row_index == 1], key=lambda x: x.column_index)
        
        for h_cell, v_cell in zip(header_row, value_row):
            header_text = (h_cell.content or "").strip()
            value_text = (v_cell.content or "").strip()
            if pattern.match(header_text) and value_text.replace("€", "").strip():
                try:
                    fee_val = float(value_text.replace("€", "").replace(",", "").strip())
                except:
                    fee_val = 0.0
                fees[header_text] = fee_val
    return fees


def extract_totals_section(result) -> Dict[str, str]:
    totals = {}
    lines = [line.strip() for line in (result.content or "").split("\n") if line.strip()]
    
    vat_line_idx = None
    for i, line in enumerate(lines):
        if "VAT 19%" in line or "MwSt. 19%" in line:
            vat_line_idx = i
            break
    
    if vat_line_idx is not None and vat_line_idx + 1 < len(lines):
        gross_line = lines[vat_line_idx + 1]
        if any(kw in gross_line for kw in ["Gross Amount", "Betrag inkl.", "incl. VAT"]):
            vat_val = lines[vat_line_idx].split()[-1]
            gross_val = gross_line.split()[-1]
            totals["VAT 19%"] = vat_val
            totals["Gross Amount incl. VAT"] = gross_val

    for line in lines:
        if line.startswith("Total") or "Summe" in line:
            totals["Total"] = line.split()[-1]
            break

    return totals


def find_field_smart(result, field_name: str, field_index: Dict[str, str]):
    target = field_name.strip().lower()

    for k, v in field_index.items():
        if k == target:
            return k, v

    for k, v in field_index.items():
        if target.replace(" ", "") in k.replace(" ", ""):
            return k, v

    doc = result.documents[0] if result.documents else None

    if doc and doc.fields:
        for fname, fval in doc.fields.items():
            if fname.lower() == target:
                val = getattr(fval, "value", None)
                if val is not None:
                    return fname, str(val) if not isinstance(val, (dict, list)) else json.dumps(val)


    all_kvs = (getattr(doc, "key_value_pairs", []) or []) + (getattr(result, "key_value_pairs", []) or [])
    for kv in all_kvs:
        if not kv.key or not kv.value:
            continue
        key = (kv.key.content or "").strip().lower().rstrip(":").strip()
        val = (kv.value.content or "").strip()
        if key == target or target in key:
            return kv.key.content.strip(), val

    for table in getattr(result, "tables", []):
        if table.row_count < 2:
            continue
        for row_idx in range(table.row_count - 1):
            key_cells = sorted([c for c in table.cells if c.row_index == row_idx], key=lambda x: x.column_index)
            val_cells = sorted([c for c in table.cells if c.row_index == row_idx + 1], key=lambda x: x.column_index)
            for col_idx, key_cell in enumerate(key_cells):
                key_str = (key_cell.content or "").strip().lower().rstrip(":").strip()
                if key_str == target:
                    if col_idx < len(val_cells):
                        return key_cell.content.strip(), val_cells[col_idx].content.strip()

    if "transaction fee" in target:
        fees = extract_transaction_fees(result)
        for k, v in fees.items():
            if target.replace(" ", "") in k.lower().replace(" ", ""):
                return k, f"{v:.2f} €"

    if any(x in target for x in ["vat", "gross", "total"]):
        totals = extract_totals_section(result)
        if "vat" in target and "VAT 19%" in totals:
            return "VAT 19%", totals["VAT 19%"]
        if "gross" in target and "Gross Amount incl. VAT" in totals:
            return "Gross Amount incl. VAT", totals["Gross Amount incl. VAT"]
        if "total" in target and "Total" in totals:
            return "Total", totals["Total"]

    lines = [l.strip() for l in (result.content or "").split("\n") if l.strip()]
    for i, line in enumerate(lines):
        if target in line.lower():
            if ":" in line:
                key_part, val_part = line.split(":", 1)
                if target in key_part.lower():
                    return key_part.strip(), val_part.strip()
            else:
                if i + 1 < len(lines):
                    next_line = lines[i + 1]
                    if not any(kw in next_line.lower() for kw in ["invoice", "date", "total", "vat"]):
                        return line.strip(), next_line

    return None, None


def build_field_index(result) -> Dict[str, str]:

    from collections import defaultdict

    candidates: Dict[str, List[str]] = defaultdict(list)

    doc = result.documents[0] if result.documents else None

    if doc and doc.fields:
        for fname, fval in doc.fields.items():
            val = getattr(fval, "value", None)
            if val is not None:
                v = str(val) if not isinstance(val, (dict, list)) else json.dumps(val)
                candidates[fname.lower()].append(v)

    all_kvs = (getattr(doc, "key_value_pairs", []) or []) + (getattr(result, "key_value_pairs", []) or [])
    for kv in all_kvs:
        if not kv.key or not kv.value:
            continue
        k = (kv.key.content or "").strip().rstrip(":").strip()
        v = (kv.value.content or "").strip()
        if k:
            candidates[k.lower()].append(v)

    for table in getattr(result, "tables", []):
        for rec in table_to_records(table):
            for raw_key, raw_val in rec.items():
                key = str(raw_key).strip().rstrip(":").strip()

                if isinstance(raw_val, (dict, list)):
                    val = json.dumps(raw_val)
                elif isinstance(raw_val, str):
                    val = raw_val.strip()
                else:
                    val = str(raw_val)

                if key and val:
                    candidates[key.lower()].append(val)

    index: Dict[str, str] = {}
    for key, values in candidates.items():
        clean_vals = [v for v in values if v and v not in [":", ":", "T1:", "T2:", "..."]]

        if not clean_vals:
            continue

        currency_vals = [v for v in clean_vals if "€" in v]
        number_vals = [v for v in clean_vals if re.search(r'\d', v)]

        best = None
        if currency_vals:
            best = currency_vals[0]
        elif number_vals:
            best = number_vals[0]
        else:
            best = clean_vals[0] 

        index[key] = best

    return index

def table_to_records(table) -> List[Dict[str, str]]:
    if table.row_count < 1 or table.column_count < 1:
        return []

    grid = {}
    max_r = table.row_count
    max_c = table.column_count
    for cell in table.cells:
        txt = (cell.content or "").strip()
        col_idx = getattr(cell, "column_index", 0) or 0
        row_idx = getattr(cell, "row_index", 0) or 0
        col_span = getattr(cell, "column_span", 1) or 1
        row_span = getattr(cell, "row_span", 1) or 1

        for rr in range(row_idx, row_idx + row_span):
            for cc in range(col_idx, col_idx + col_span):
                prev = grid.get((rr, cc), "")
                grid[(rr, cc)] = (prev + (" | " + txt) if prev and txt else (txt or prev))

    def cell_text(r, c):
        return grid.get((r, c), "").strip()

    first_row_texts = [cell_text(0, c) for c in range(0, max_c)]
    first_col_texts = [cell_text(r, 0) for r in range(0, max_r)]

    def looks_numeric(s: str):
        s = s or ""
        if "€" in s or "$" in s: 
            return True
        return bool(re.search(r"\d", s))

    row_numeric = sum(1 for t in first_row_texts if looks_numeric(t))
    col_numeric = sum(1 for t in first_col_texts if looks_numeric(t))

    nonempty_row = sum(1 for t in first_row_texts if t)
    nonempty_col = sum(1 for t in first_col_texts if t)

    if nonempty_col > nonempty_row and col_numeric < nonempty_col/2:
        left_is_key = True
    elif nonempty_row >= nonempty_col and row_numeric < nonempty_row/2:
        left_is_key = False
    else:
        if nonempty_row >= nonempty_col:
            left_is_key = False
        else:
            left_is_key = True

    records: List[Dict[str, str]] = []

    if not left_is_key:
        headers = [cell_text(0, c) or f"col_{c}" for c in range(0, max_c)]
        for r in range(1, max_r):
            row = {}
            empty = True
            for c in range(0, max_c):
                val = cell_text(r, c)
                if val:
                    row[headers[c]] = val
                    empty = False
            if not empty:
                records.append(row)
        return records

    has_header_row = any(cell_text(0, c) for c in range(1, max_c))
    value_headers = [cell_text(0, c) or f"col_{c}" for c in range(0, max_c)] if has_header_row else None

    for r in range(0, max_r):
        key = cell_text(r, 0)
        if not key:
            continue
        row = {}
        empty = True
        for c in range(1, max_c):
            v = cell_text(r, c)
            if not v:
                continue
            if value_headers:
                header_name = value_headers[c]
            else:
                header_name = f"col_{c}"
            row[header_name] = v
            empty = False
        if not empty:
            if len(row) == 1:
                single_val = next(iter(row.values()))
                records.append({key: single_val})
            else:
                flat = {k: v for k, v in row.items()}
                flat_key = key
                flat["__row_key"] = flat_key
                records.append({flat_key: flat})
    return records


if st.button("EXTRACT FIELD", type="primary"):
    if not endpoint or not api_key or not uploaded_file or not field_name:
        st.error("Please provide Endpoint, API Key, PDF, and Field Name.")
        st.stop()

    try:
        client = DocumentIntelligenceClient(
            endpoint=endpoint.rstrip("/") + "/",
            credential=AzureKeyCredential(api_key)
        )

        poller = client.begin_analyze_document(
            model_id="prebuilt-invoice",
            analyze_request=AnalyzeDocumentRequest(bytes_source=uploaded_file.read())
        )
        result = poller.result()
        field_index = build_field_index(result)

        found_key, found_value = find_field_smart(result, field_name, field_index)
        if found_key:
            st.success(f"**{found_key}** → `{found_value}`")
        else:
            st.warning(f"Field **'{field_name}'** not found.")

    except Exception as e:
        st.error(f"Error: {e}")
        st.exception(e)
