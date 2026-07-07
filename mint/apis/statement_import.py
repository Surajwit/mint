import frappe
import re
from frappe.utils.csvutils import read_csv_content
from frappe.utils.xlsxutils import (
	read_xls_file_from_attached_file,
	read_xlsx_file_from_attached_file,
)
from frappe import _
from frappe.utils import getdate

from datetime import datetime

from mint.apis.bank_account import set_closing_balance_as_per_statement

@frappe.whitelist(methods=["GET"])
def get_statement_details(file_url: str, bank_account: str):
    """
    Given a file path, try to get bank statement details.

    From the data, we want to "guess" the following:
    1. Row index of the header row
    2. Column mapping to standard variables?
    3. Row indices of all rows after the header row that are relevant - hence look like transactions
    4. Opening and Closing dates of the statement and balance
    """

    data = get_data(file_url)

    file_name = file_url.split("/")[-1]

    header_index, max_valid_columns = get_header_row_index(data)

    if max_valid_columns >= 2:
        header_row = data[header_index]
        columns, column_mapping = get_column_mapping(header_row)

        # Fallback for empty header columns using auto_detect on the first data row
        if len(data) > header_index + 1:
            first_data_row = data[header_index + 1]
            auto_columns, auto_mapping = auto_detect_columns(first_data_row)
            
            # For any crucial column missing, see if auto_detect found it
            for crucial in ["Date", "Description", "Reference", "Amount", "Balance"]:
                if crucial not in column_mapping and crucial in auto_mapping:
                    idx = auto_mapping[crucial]
                    column_mapping[crucial] = idx
                    for col in columns:
                        if col["index"] == idx:
                            col["maps_to"] = crucial
                            break
    else:
        # No headers found. Synthesize a header row and auto-detect columns from the first data row (header_index + 1)
        header_index = 0
        header_row = ["Col 1", "Col 2", "Col 3", "Col 4", "Col 5", "Col 6", "Col 7", "Col 8", "Col 9", "Col 10"]
        first_data_row = data[1] if len(data) > 1 else data[0]
        columns, column_mapping = auto_detect_columns(first_data_row)
        header_row = [column["header_text"] for column in columns]

    transaction_rows, transaction_starting_index, transaction_ending_index = get_transaction_rows(data, header_index, column_mapping)

    date_format, amount_format = get_file_properties(transaction_rows)

    char_map = {
        "%d": "DD",
        "%m": "MM",
        "%Y": "YYYY",
        "%y": "YY",
        "%b": "MMM",
        "%B": "MMMM",
    }

    formatted_date_format = date_format
    for char, replacement in char_map.items():
        formatted_date_format = formatted_date_format.replace(char, replacement)

    statement_start_date, statement_end_date, closing_balance = get_closing_balance(transaction_rows, date_format)

    conflicting_transactions = check_for_conflicts(bank_account, statement_start_date, statement_end_date)

    final_transactions = get_final_transactions(transaction_rows, date_format, amount_format)

    account = frappe.get_cached_value("Bank Account", bank_account, "account")
    account_currency = frappe.get_cached_value("Account", account, "account_currency")

    return {
        "file_name": file_name,
        "file_path": file_url,
        "data": data,
        "header_index": header_index,
        "header_row": header_row,
        "columns": columns,
        "column_mapping": column_mapping,
        "transaction_starting_index": transaction_starting_index,
        "transaction_ending_index": transaction_ending_index,
        "transaction_rows": transaction_rows,
        "date_format": formatted_date_format,
        "amount_format": amount_format,
        "statement_start_date": statement_start_date,
        "statement_end_date": statement_end_date,
        "closing_balance": closing_balance,
        "conflicting_transactions": conflicting_transactions,
        "final_transactions": final_transactions,
        "currency": account_currency,
    }

def process_statement_import_background(final_transactions, bank_account, currency, company, file_url, data, user):
    frappe.set_user(user)
    progress = 0
    success = 0
    errors = 0

    for transaction in final_transactions:
        try:
            # Todas las filas del extracto se importan como transacciones bancarias separadas.
            # Las comisiones emparejadas enriquecen el campo 'commission' de la tx principal,
            # pero también se crean como su propia Bank Transaction (comportamiento esperado).
            # Evitar reinsertar transacciones ya existentes (misma cuenta + referencia + monto).
            # Para depósitos: referencia duplicada = duplicado real (no hay comisiones de depósito).
            # Para retiros: si la referencia existe pero el monto es distinto, es una comisión
            # bancaria legítima y se permite importar (ej: retiro 160.000 + comisión 120).
            ref = transaction.get("reference")

            # Verificar si existe como depósito (referencia es suficiente para detectar duplicado)
            if ref and float(transaction.get("deposit") or 0) > 0:
                if frappe.db.exists("Bank Transaction", {"bank_account": bank_account, "reference_number": ref, "deposit": [">", 0]}):
                    errors += 1
                    continue

            # Verificar si existe como retiro: solo es duplicado si el monto también coincide
            if ref and float(transaction.get("withdrawal") or 0) > 0:
                new_amount = float(transaction.get("withdrawal") or 0)
                existing = frappe.db.get_value(
                    "Bank Transaction",
                    {"bank_account": bank_account, "reference_number": ref, "withdrawal": [">", 0]},
                    "withdrawal"
                )
                if existing is not None and float(existing) == new_amount:
                    errors += 1
                    continue

            bank_tx = frappe.get_doc({
                "doctype": "Bank Transaction",
                "date": transaction.get("date"),
                "status": "Unreconciled",
                "bank_account": bank_account,
                "withdrawal": transaction.get("withdrawal"),
                "deposit": transaction.get("deposit"),
                "description": transaction.get("description"),
                "reference_number": transaction.get("reference"),
                "transaction_type": transaction.get("transaction_type"),
                "currency": currency,
                "company": company,
                "commission": transaction.get("commission", 0.0),
                "equivalent_commission": transaction.get("equivalent_commission", 0.0),
            })
            bank_tx.insert(ignore_permissions=True)
            bank_tx.submit()
            frappe.db.commit()
            success += 1
        except Exception as e:
            frappe.db.rollback()
            frappe.log_error(title="Error en importación de transacción bancaria")
            frappe.db.commit()
            errors += 1
        finally:
            progress += 1
            if progress % 50 == 0:
                frappe.publish_realtime("mint-statement-import-progress", {
                    "progress": round((progress / len(final_transactions)) * 100),
                }, user=user)
    
    frappe.publish_realtime("mint-statement-import-progress", {
        "progress": 100,
        "total": len(final_transactions),
    }, user=user)
    
    # DECIMAL(21,9) en MySQL soporta máximo 12 dígitos antes del punto decimal.
    # Si el saldo parseado supera ese rango (error de formato en el archivo), guardamos None
    # para no lanzar un DataError y permitir que la importación continúe.
    _MAX_DECIMAL_21_9 = 999_999_999_999.999999999
    raw_balance = data.get("closing_balance")
    safe_closing_balance = raw_balance if (raw_balance is None or abs(raw_balance) <= _MAX_DECIMAL_21_9) else None
    if raw_balance is not None and safe_closing_balance is None:
        frappe.log_error(
            f"closing_balance {raw_balance} fuera del rango DECIMAL(21,9) — se omite del log.",
            "Statement Import: closing_balance overflow"
        )

    log = frappe.new_doc("Mint Bank Statement Import Log")
    log.bank_account = bank_account
    log.file = file_url
    log.number_of_transactions = len(final_transactions)
    log.start_date = data.get("statement_start_date")
    log.end_date = data.get("statement_end_date")
    log.closing_balance = safe_closing_balance
    log.insert(ignore_permissions=True)

    if safe_closing_balance is not None and data.get("statement_end_date"):
        set_closing_balance_as_per_statement(bank_account, getdate(data.get("statement_end_date")), safe_closing_balance)
    
    from mint.apis.rules import run_rule_evaluation
    run_rule_evaluation()

    subject = _("Importación finalizada: {0} exitosos, {1} fallidos").format(success, errors)
    message = _("Se procesaron {0} transacciones bancarias nuevas de un total de {1}.").format(success, len(final_transactions))
    if errors > 0:
        message += "<br>" + _("Se omitieron {0} transacciones porque ya existían en el sistema (referencias duplicadas) o tuvieron error.").format(errors)
        
    notification = frappe.new_doc("Notification Log")
    notification.subject = subject
    notification.email_content = message
    notification.for_user = user
    notification.type = "Alert"
    notification.document_type = "Mint Bank Statement Import Log"
    notification.document_name = log.name
    notification.insert(ignore_permissions=True)
    frappe.db.commit()


@frappe.whitelist(methods=["POST"])
def import_statement(file_url: str, bank_account: str):
    """
    Given a file path and bank account, try to import the statement
    """

    if not frappe.has_permission("Bank Transaction", "write"):
        frappe.throw(_("You do not have permission to import bank transactions"), title="Permission Denied")
    
    if not frappe.has_permission("Bank Transaction", "create"):
        frappe.throw(_("You do not have permission to import bank transactions"), title="Permission Denied")
    
    if not frappe.has_permission("Bank Transaction", "submit"):
        frappe.throw(_("You do not have permission to import and submit bank transactions"), title="Permission Denied")

    

    company, account, is_company_account, disabled = frappe.get_value("Bank Account", bank_account, ["company", "account", "is_company_account", "disabled"])
    if not is_company_account:
        frappe.throw(_("The bank account is not a company account. Please select a company account"), title="Invalid Bank Account")
    
    if disabled:
        frappe.throw(_("The bank account is disabled. Please enable it"), title="Disabled Bank Account")
    
    currency = frappe.get_value("Account", account, "account_currency")
    # Create the bank transactions, submit them and then store the closing balance if any

    data = get_statement_details(file_url, bank_account)

    final_transactions = data.get("final_transactions", [])

    # 1. Limpiar referencias
    for tx in final_transactions:
        ref = str(tx.get("reference") or "").strip()
        if ref.endswith(".0"):
            ref = ref[:-2]
        if ref.startswith("'"):
            ref = ref[1:]
        tx["cleaned_reference"] = ref

    # 2. Emparejar comisiones
    try:
        from l10n_ve.utils.utils import get_exchange_rate
    except ImportError:
        get_exchange_rate = None

    # Keywords largos: suficientemente específicos para buscar en cualquier parte
    COMMISSION_SUBSTRINGS = ("comision", "comisión", "commission", "comis")
    # Keywords cortos: solo como palabra completa para evitar falsos positivos
    # ("com" en "comercial" no es comisión, pero "com" solo sí)
    COMMISSION_WORDS = {"com", "com."}

    def _is_commission_desc(desc: str) -> bool:
        d = desc.lower().strip()
        # Substrings específicos largos (comision, comis, commission...)
        if any(kw in d for kw in COMMISSION_SUBSTRINGS):
            return True
        # Palabras exactas: "com" o "com." solas
        words = d.split()
        if any(w in COMMISSION_WORDS for w in words):
            return True
        # Abreviaturas con punto como prefijo: "com.op.pag.movil", "com.p2p", etc.
        # Cualquier token que empiece con "com." es una abreviatura de comisión
        if any(w.startswith("com.") for w in words):
            return True
        return False

    commissions_map = {}
    for tx in final_transactions:
        c_wth = float(tx.get("withdrawal") or 0)
        if c_wth > 0:
            c_desc = str(tx.get("description") or "")
            if _is_commission_desc(c_desc):
                c_ref = tx.get("cleaned_reference")
                c_date = tx.get("date")
                if c_ref and c_date:
                    key = (c_ref, c_date)
                    if key not in commissions_map:
                        commissions_map[key] = []
                    commissions_map[key].append(tx)

    for tx in final_transactions:
        tx_desc = str(tx.get("description") or "")
        is_commission = _is_commission_desc(tx_desc)
        
        if not is_commission:
            tx_ref = tx.get("cleaned_reference")
            tx_date = tx.get("date")
            
            if not tx_ref or not tx_date:
                continue
                
            key = (tx_ref, tx_date)
            if key in commissions_map:
                for comm_tx in commissions_map[key]:
                    if comm_tx.get("is_paired"):
                        continue
                        
                    c_wth = float(comm_tx.get("withdrawal") or 0)
                    tx["commission"] = tx.get("commission", 0.0) + c_wth
                    comm_tx["is_paired"] = True
                    
                    if get_exchange_rate:
                        try:
                            try:
                                rate = get_exchange_rate(tx_date, "USD")
                            except TypeError:
                                rate = get_exchange_rate(tx_date, "USD", "VES")
                        except Exception:
                            rate = 0.0
                        if rate and rate > 0:
                            tx["equivalent_commission"] = tx.get("equivalent_commission", 0.0) + (c_wth / rate)

    if len(final_transactions) > 100:
        frappe.enqueue(
            "mint.apis.statement_import.process_statement_import_background",
            queue="long",
            timeout=7200,
            final_transactions=final_transactions,
            bank_account=bank_account,
            currency=currency,
            company=company,
            file_url=file_url,
            data={
                "statement_start_date": data.get("statement_start_date"),
                "statement_end_date": data.get("statement_end_date"),
                "closing_balance": data.get("closing_balance")
            },
            user=frappe.session.user
        )
    else:
        process_statement_import_background(
            final_transactions=final_transactions,
            bank_account=bank_account,
            currency=currency,
            company=company,
            file_url=file_url,
            data={
                "statement_start_date": data.get("statement_start_date"),
                "statement_end_date": data.get("statement_end_date"),
                "closing_balance": data.get("closing_balance")
            },
            user=frappe.session.user
        )

    return {
        "success": True,
        "message": _("Bank statement imported successfully."),
        "start_date": data.get("statement_start_date"),
        "end_date": data.get("statement_end_date"),
    }

# Firmas con las que xlrd falla cuando el archivo .xls es en realidad un HTML
# (tabla disfrazada de Excel que entregan algunos bancos venezolanos).
HTML_LIKE_XLS_ERRORS = (
    "Expected BOF record; found b'<table",
    "found b'<html",
    "startswith first arg must be str",
)


def _is_html_like_xls_error(exc: Exception) -> bool:
    """True si el error de xlrd indica que el .xls es realmente HTML."""
    error_msg = str(exc)
    return any(err in error_msg for err in HTML_LIKE_XLS_ERRORS)


def _read_csv_content_robust(content) -> list[list]:
    import csv
    if not isinstance(content, str):
        decoded = False
        for encoding in ["utf-8", "windows-1250", "windows-1252", "latin-1"]:
            try:
                content = str(content, encoding)
                decoded = True
                break
            except UnicodeDecodeError:
                continue
    
    lines = content.splitlines()
    if not lines:
        return []

    # Auto-detect delimiter (comma or semicolon)
    delimiter = ','
    try:
        sniffer = csv.Sniffer()
        dialect = sniffer.sniff(lines[0])
        delimiter = dialect.delimiter
    except Exception:
        if lines[0].count(';') > lines[0].count(','):
            delimiter = ';'

    rows = []
    for row in csv.reader(lines, delimiter=delimiter):
        rows.append([val.strip() for val in row])
    return rows


def _parse_corrupt_xlsx(content) -> list[list]:
    import zipfile
    from io import BytesIO
    import xml.etree.ElementTree as ET
    import re

    def col2num(col):
        num = 0
        for c in col:
            num = num * 26 + (ord(c.upper()) - ord('A')) + 1
        return num - 1

    with zipfile.ZipFile(BytesIO(content)) as z:
        shared_strings = []
        if 'xl/sharedStrings.xml' in z.namelist():
            ss_xml = z.read('xl/sharedStrings.xml')
            root = ET.fromstring(ss_xml)
            for elem in root.iter():
                if elem.tag.endswith('}t'):
                    shared_strings.append(elem.text)
        
        sheet_filename = None
        for name in z.namelist():
            if name.startswith('xl/worksheets/sheet') and name.endswith('.xml'):
                sheet_filename = name
                break
        
        if not sheet_filename:
            return []

        sheet_xml = z.read(sheet_filename)
        root = ET.fromstring(sheet_xml)
        
        data = []
        for row in root.iter():
            if row.tag.endswith('}row'):
                row_data = []
                for cell in row:
                    if cell.tag.endswith('}c'):
                        val = ""
                        for child in cell:
                            if child.tag.endswith('}v'):
                                val = child.text
                                t = cell.get('t')
                                if t == 's' and val is not None:
                                    try:
                                        val = shared_strings[int(val)]
                                    except (ValueError, IndexError):
                                        pass
                            elif child.tag.endswith('}is'):
                                for t_node in child:
                                    if t_node.tag.endswith('}t'):
                                        val = t_node.text
                        
                        r_attr = cell.get('r')
                        if r_attr:
                            match = re.match(r"([A-Z]+)[0-9]+", r_attr)
                            if match:
                                col_idx = col2num(match.group(1))
                                while len(row_data) <= col_idx:
                                    row_data.append("")
                                row_data[col_idx] = val
                            else:
                                row_data.append(val)
                        else:
                            row_data.append(val)
                data.append(row_data)
        return data

def _parse_html_as_table(content) -> list[list]:
    """Parsea un archivo .xls que en realidad contiene una tabla HTML.

    Devuelve la misma estructura ``list[list[str]]`` que producen los lectores
    de Excel/CSV, de modo que el resto del flujo (detección de encabezados y
    mapeo de columnas) no necesita cambios.
    """
    from bs4 import BeautifulSoup

    if isinstance(content, bytes):
        text = content.decode("utf-8", errors="ignore")
    else:
        text = content

    soup = BeautifulSoup(text, "html.parser")
    table = soup.find("table")
    if not table:
        frappe.throw(
            _("No se pudo leer el archivo .xls: parece HTML pero no contiene una tabla."),
            title=_("Invalid File Type"),
        )

    data = []
    for row in table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        data.append([cell.get_text(strip=True) for cell in cells])
    return data


def get_data(file_path: str):

    file_doc = frappe.get_doc("File", {"file_url": file_path})

    parts = file_doc.get_extension()
    extension = parts[1]
    content = file_doc.get_content()

    if extension.lower() not in (".csv", ".xlsx", ".xls"):
        frappe.throw(_("Import template should be of type .csv, .xlsx or .xls"), title="Invalid File Type")

    if extension.lower() == ".csv":
        data = _read_csv_content_robust(content)
    elif extension.lower() == ".xlsx":
        try:
            data = read_xlsx_file_from_attached_file(fcontent=content)
        except Exception as e:
            # Fallback for corrupted xlsx (e.g. Bancamiga) where openpyxl drops worksheets
            # due to "invalid specification for 0"
            try:
                data = _parse_corrupt_xlsx(content)
                if not data:
                    raise Exception("Custom parser returned no data")
            except Exception:
                # Si falla, podría ser un .xls real (o HTML) renombrado a .xlsx
                try:
                    data = read_xls_file_from_attached_file(content)
                except Exception:
                    data = _parse_html_as_table(content)
    elif extension.lower() == ".xls":
        try:
            data = read_xls_file_from_attached_file(content)
        except Exception as e:
            # Algunos bancos (p. ej. Bancamiga) entregan un HTML con extensión
            # .xls; xlrd falla con "Expected BOF record; found b'<table". En ese
            # caso lo parseamos como tabla HTML en lugar de propagar un 500.
            if _is_html_like_xls_error(e):
                data = _parse_html_as_table(content)
            else:
                raise

    # Limitar decimales a 2 para evitar errores de precisión de punto flotante en la UI
    if data:
        for row_idx in range(len(data)):
            for col_idx in range(len(data[row_idx])):
                cell_val = data[row_idx][col_idx]
                if isinstance(cell_val, float):
                    data[row_idx][col_idx] = round(cell_val, 2)
                elif isinstance(cell_val, str) and "." in cell_val:
                    if cell_val.replace(".", "", 1).replace("-", "", 1).isdigit():
                        if len(cell_val.split(".")[1]) > 2:
                            try:
                                data[row_idx][col_idx] = str(round(float(cell_val), 2))
                            except ValueError:
                                pass

    return data

def get_header_row_index(data: list[list[str]]):
    """
    Given the data, try to get the row index of the header row.
    """

    row_index = 0
    max_valid_columns = 0

    # Loop over rows and find the first row that has the most number of "valid" column headers
    # Valid columns is based on keywords present in each cell

    for idx, row in enumerate(data):
        valid_columns = 0
        for cell in row:
            if not cell:
                continue

            # If cell is a string, then we need to check if it contains any of the keywords
            if not isinstance(cell, str):
                continue
#buscamos todo el diccionario contable si es posible en español, ingles, chino
            if any(keyword in cell.lower() for keyword in ["date", "amount", "description", "reference", "transaction", "type", "cr", "dr", "deposit", "withdrawal", "balance", "fecha", "concepto", "referencia", "débito", "debito", "crédito", "credito", "saldo", "cargo", "cargos", "abono", "abonos"]):
                valid_columns += 1
        if valid_columns > max_valid_columns:
            max_valid_columns = valid_columns
            row_index = idx

    return row_index, max_valid_columns

def get_column_mapping(header_row: list[str]):
    """
    Given the header row, try to map each column index to a standard variable, or set it to "Do not import"
    """
    standard_variables = {
        "Date": ["date", "transaction date", "fecha"], 
        "Withdrawal": ["withdrawal", "debit", "débito", "debito", "cargo", "cargos"],
        "Deposit": ["deposit", "credit", "crédito", "credito", "abono", "abonos"],
        "Amount": ["amount", "monto", "importe"], 
        "Description": ["description", "particulars", "remarks", "narration", "detail", "reference", "concepto", "descripción", "descripcion"], 
        "Reference": ["reference", "ref", "tran id", "transaction id", "cheque", "check", "id", "chq", "referencia", "nro"], 
        "Transaction Type": ["transaction type", "cr/dr", "dr/cr", "debit/credit", "credit/debit", "tipo", "d/c", "c/d", "signo"], 
        "Balance": ["balance", "saldo"],
    }
    # A standard variable can be represented by multiple names

    column_mapping = {}

    # Loop over all columns and check if they contain any of the standard variable names
    # If not, we do not import it
    # If they do, we map the column index to the standard variable

    columns = []

    for idx, cell in enumerate(header_row):
        if not cell or str(cell).strip() == "":
            header_text = f"Columna {idx + 1}"
            column = {
                "index": idx,
                "header_text": header_text,
                "variable": header_text.lower().replace(" ", "_"),
                "maps_to": "Do not import",
            }
            columns.append(column)
            continue

        cell_str = str(cell)
        column = {
            "index": idx,
            "header_text": cell_str,
            "variable": cell_str.strip().lower().replace(" ", "_").replace("?", "").replace(".", ""),
            "maps_to": "Do not import",
        }

        for standard_variable, names in standard_variables.items():
            if any(name in cell_str.lower().replace(".", "") for name in names):

                if not column_mapping.get(standard_variable, None):
                    column["maps_to"] = standard_variable

                    column_mapping[standard_variable] = idx

                    break
        
        columns.append(column)
    

    return columns, column_mapping

def auto_detect_columns(row: list[str]):
    """
    Auto-detect columns based on data types for files without headers.
    """
    columns = []
    column_mapping = {}

    for idx, cell in enumerate(row):
        col_type = "Do not import"
        header_text = f"Columna {idx + 1}"

        if not cell:
            pass
        elif "Date" not in column_mapping and frappe.utils.guess_date_format(str(cell)):
            col_type = "Date"
            header_text = "Fecha"
        elif idx == 1 and "Description" not in column_mapping:
            col_type = "Description"
            header_text = "Descripción"
        elif get_float_amount(cell) is not None:
            # We found a number.
            # Usually: 1st number = Reference/Amount, but Ref is often a string/number.
            # Let's check length or position to guess.
            # For Banco Exterior: Date, Desc, Ref, Amount, FormattedAmount, Sign, Balance
            # Index 2: Reference
            # Index 3: Amount (signed)
            # Index 6: Balance
            if idx == 2 and "Reference" not in column_mapping:
                col_type = "Reference"
                header_text = "Referencia"
            elif idx == 3 and "Amount" not in column_mapping:
                col_type = "Amount"
                header_text = "Monto"
            elif idx == 6 and "Balance" not in column_mapping:
                col_type = "Balance"
                header_text = "Saldo"
            elif "Amount" not in column_mapping and idx > 2:
                # Fallback for Amount
                col_type = "Amount"
                header_text = "Monto"
        elif isinstance(cell, str) and len(cell) > 5 and "Description" not in column_mapping:
            col_type = "Description"
            header_text = "Descripción"
        elif isinstance(cell, str) and str(cell).strip().upper() in ["C", "D", "CR", "DR"] and "Transaction Type" not in column_mapping:
            col_type = "Transaction Type"
            header_text = "Tipo"
        
        column = {
            "index": idx,
            "header_text": header_text,
            "variable": header_text.lower().replace(" ", "_").replace("ó", "o"),
            "maps_to": col_type,
        }
        if col_type != "Do not import":
            column_mapping[col_type] = idx
        columns.append(column)

    return columns, column_mapping


def get_transaction_rows(data: list[list[str]], header_index: int, column_mapping: dict[str, int]):
    """
    Given the data, header index and column mapping, try to get the transaction rows

    For each row after the header row, check if the data makes sense - date column should have a date, 
    amount column should be a number after removing any special charatcers, spaces and "CR/DR" text.
    Balance column should be a number after removing any special charatcers, spaces and "CR/DR" text.
    """

    transaction_rows = []

    transaction_starting_index = None
    transaction_ending_index = None

    valid_rows = data[header_index + 1:]

    column_map_keys = column_mapping.keys()

    for row_index, row in enumerate(valid_rows):

        date = row[column_mapping["Date"]] if "Date" in column_map_keys else None
        amount = row[column_mapping["Amount"]] if "Amount" in column_map_keys else None
        withdrawal = row[column_mapping["Withdrawal"]] if "Withdrawal" in column_map_keys else None
        deposit = row[column_mapping["Deposit"]] if "Deposit" in column_map_keys else None
        balance = row[column_mapping["Balance"]] if "Balance" in column_map_keys else None

        if not date:
            continue

        if isinstance(date, datetime):
            date = date.strftime("%Y-%m-%d")

        if not isinstance(date, str):
            continue

        if not amount and not withdrawal and not deposit:
            continue

        # Check if date column is a valid date
        row_date_format = frappe.utils.guess_date_format(date)

        if not row_date_format:
            continue

        # Check if either the amount, withdrawal or deposit column is a valid number
        amount = get_float_amount(amount)
        withdrawal = get_float_amount(withdrawal)
        deposit = get_float_amount(deposit)
        balance = get_float_amount(balance)
            
        if not amount and not withdrawal and not deposit:
            continue

        if transaction_starting_index is None:
            transaction_starting_index = row_index

        transaction_ending_index = row_index

        transaction_row = {
            "date_format": row_date_format,
        }

        if "Date" in column_map_keys:
            transaction_row["date"] = row[column_mapping["Date"]]
        if "Amount" in column_map_keys:
            transaction_row["amount"] = row[column_mapping["Amount"]]
        if "Withdrawal" in column_map_keys:
            transaction_row["withdrawal"] = row[column_mapping["Withdrawal"]]
        if "Deposit" in column_map_keys:
            transaction_row["deposit"] = row[column_mapping["Deposit"]]
        if "Balance" in column_map_keys:
            transaction_row["balance"] = row[column_mapping["Balance"]]
        if "Reference" in column_map_keys:
            transaction_row["reference"] = row[column_mapping["Reference"]]
        if "Description" in column_map_keys:
            transaction_row["description"] = row[column_mapping["Description"]]
        if "Transaction Type" in column_map_keys:
            transaction_row["transaction_type"] = row[column_mapping["Transaction Type"]]
        
        transaction_rows.append(transaction_row)
    
    base_index = header_index + 1

    if transaction_starting_index is not None:
        transaction_starting_index += base_index
    
    if transaction_ending_index is not None:
        transaction_ending_index += base_index

    return transaction_rows, transaction_starting_index, transaction_ending_index

def get_float_amount(amount):
    if not amount:
        return None

    if isinstance(amount, str):
        # Limpiar texto de espacios y prefijos
        amount = amount.lower().replace(" ", "").replace("cr", "").replace("dr", "")
        # Mantener solo dígitos, punto, coma y guión
        amount = re.sub(r'[^\d.,-]', '', amount)
        if not amount:
            return None

        last_dot = amount.rfind('.')
        last_comma = amount.rfind(',')

        last_sep = max(last_dot, last_comma)
        if last_sep != -1:
            chars_after = len(amount) - last_sep - 1
            # Si solo hay un separador y le siguen 3 dígitos, asumimos separador de miles.
            if chars_after == 3 and (last_dot == -1 or last_comma == -1):
                amount = amount.replace(',', '').replace('.', '')
                try:
                    return float(amount)
                except ValueError:
                    return None

        # Reemplazos finales para float()
        if last_comma > last_dot:
            amount = amount.replace('.', '').replace(',', '.')
        elif last_dot > last_comma:
            amount = amount.replace(',', '')

        try:
            return float(amount)
        except ValueError:
            return None
            
    try:
        return float(amount)
    except (ValueError, TypeError):
        return None

def get_file_properties(transactions: list):
    """
    From the transaction rows, try to figure out the following:
    1. Most common date format
    2. Amount format - does it contain "CR/Dr" text or is it in a separate column (maybe transaction type?). Amount could also be positive and negative.
    """

    date_format_frequency = {
        "%d/%m/%Y": 0,
    }

    amount_format_frequency = {
        "separate_columns_for_withdrawal_and_deposit": 0,
        "dr_cr_in_amount": 0,
        "positive_negative_in_amount": 0,
        "cr_dr_in_transaction_type": 0,
        "deposit_withdrawal_in_transaction_type": 0,
    }

    for transaction in transactions:
        date_format = transaction.get("date_format")

        if date_format:
            date_format_frequency[date_format] = date_format_frequency.get(date_format, 0) + 1
        
        # Check if there's an amount column
        # If there's a separate column for withdrawal and deposit, we can skip this
        if transaction.get("withdrawal", None) or transaction.get("deposit", None):
            amount_format_frequency["separate_columns_for_withdrawal_and_deposit"] += 1
            continue

        amount = transaction.get("amount", None)

        if not amount:
            continue

        if isinstance(amount, str) and ("cr" in amount.lower() or "dr" in amount.lower()):
            amount_format_frequency["dr_cr_in_amount"] += 1
        
        # Check if there's a transaction type column containing "cr"/"dr"
        if transaction.get("transaction_type", None):
            t_type = transaction.get("transaction_type", "").lower().strip()
            if "cr" in t_type or "dr" in t_type or t_type in ["c", "d"] or "credito" in t_type or "debito" in t_type or "crédito" in t_type or "débito" in t_type:
                amount_format_frequency["cr_dr_in_transaction_type"] += 1
            if "deposit" in t_type or "withdrawal" in t_type or "abono" in t_type or "cargo" in t_type:
                amount_format_frequency["deposit_withdrawal_in_transaction_type"] += 1
        
        # Else assume that the amount is expressed as positive/negative value
        else:
            amount_format_frequency["positive_negative_in_amount"] += 1
    
    most_common_date_format = max(date_format_frequency, key=date_format_frequency.get)
    most_common_amount_format = max(amount_format_frequency, key=amount_format_frequency.get)

    return most_common_date_format, most_common_amount_format


def get_closing_balance(transactions: list, date_format: str):
    """
    Given the transactions and date format, try to get the statement start date, end date and closing balance
    """

    statement_start_date = None
    statement_end_date = None
    closing_balance = None

    for transaction in transactions:
        date = transaction.get("date")
        if not date:
            continue

        if isinstance(date, datetime):
            tx_date = date
        else:
            tx_date = datetime.strptime(date, date_format)

        if statement_start_date is None or tx_date < statement_start_date:
            statement_start_date = tx_date

        if statement_end_date is None or tx_date >= statement_end_date:
            statement_end_date = tx_date

            closing_balance = transaction.get("balance")

    return getdate(statement_start_date), getdate(statement_end_date), get_float_amount(closing_balance)


def check_for_conflicts(bank_account: str, start_date: str, end_date: str):
    """
    Given a bank account, start date and end date, check if there are any conflicts with existing bank transactions
    """

    conflicts = frappe.get_all("Bank Transaction", filters={
        "bank_account": bank_account,
        "date": ["between", [start_date, end_date]],
        "docstatus": 1,
    }, fields=["name", "date", "withdrawal", "deposit", "description", "reference_number", "currency"],
    order_by="date")

    return conflicts


def get_final_transactions(transactions: list, date_format: str, amount_format: str):
    """
    Given the transactions, date format and amount format, try to get the final transactions
    """

    final_transactions = []

    def parse_amount(transaction_row: dict):
        """
        Given a transaction row, try to parse the amount - returns tuple of (withdrawal, deposit)
        """

        if amount_format == "separate_columns_for_withdrawal_and_deposit":
            return get_float_amount(transaction_row.get("withdrawal")), get_float_amount(transaction_row.get("deposit"))
        
        if amount_format == "dr_cr_in_amount":
            amount = transaction_row.get("amount")
            float_amount = get_float_amount(amount)
            if "cr" in amount.lower():
                return 0, float_amount
            else:
                return float_amount, 0
        
        if amount_format == "positive_negative_in_amount":
            amount = get_float_amount(transaction_row.get("amount", "0"))
            if amount > 0:
                return 0, abs(amount)
            else:
                return abs(amount), 0
        
        if amount_format == "cr_dr_in_transaction_type":
            transaction_type = transaction_row.get("transaction_type", "").lower().strip()
            amount = get_float_amount(transaction_row.get("amount", "0"))
            if "cr" in transaction_type or transaction_type == "c" or "cred" in transaction_type or "créd" in transaction_type:
                return 0, abs(amount)
            else:
                return abs(amount), 0
        
        if amount_format == "deposit_withdrawal_in_transaction_type":
            transaction_type = transaction_row.get("transaction_type", "").lower().strip()
            amount = get_float_amount(transaction_row.get("amount", "0"))
            if "deposit" in transaction_type or "abono" in transaction_type:
                return 0, abs(amount)
            else:
                return abs(amount), 0
        
        return 0, 0
    
    for transaction in transactions:
        date = transaction.get("date")

        if isinstance(date, datetime):
            date = date.strftime("%Y-%m-%d")
        else:
            date = datetime.strptime(date, date_format).strftime("%Y-%m-%d")

        withdrawal, deposit = parse_amount(transaction)
        final_transactions.append({
            "date": date,
            "withdrawal": withdrawal,
            "deposit": deposit,
            "description": transaction.get("description"),
            "reference": transaction.get("reference"),
            "transaction_type": transaction.get("transaction_type"),
        })
    
    return final_transactions