import frappe
from frappe.utils import getdate, get_last_day, add_days
from frappe.utils.pdf import get_pdf
from mint.mint.report.advanced_bank_reconciliation.advanced_bank_reconciliation import (
	get_data_and_summary,
	get_account_balance
)

@frappe.whitelist(allow_guest=False)
def download_pdf(company, bank_account, month, year, bank_statement_closing_balance=0):
	months = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
	
	try:
		year = int(year)
		month_idx = months.index(month) + 1
	except Exception:
		frappe.throw("Mes o año inválido")
		
	bank_statement_closing_balance = float(bank_statement_closing_balance or 0)
		
	start_date = getdate(f"{year}-{month_idx:02d}-01")
	end_date = get_last_day(start_date)
	
	bank_name = frappe.db.get_value("Bank Account", bank_account, "bank")
	account_currency = frappe.db.get_value("Account", frappe.db.get_value("Bank Account", bank_account, "account"), "account_currency")
	
	filters = frappe._dict({
		"company": company,
		"account": bank_account,
		"from_date": start_date,
		"to_date": end_date,
		"include_reconciled": 1
	})
	
	data, summary = get_data_and_summary(filters)
	
	cleared_deposits = 0
	cleared_withdrawals = 0
	for row in data:
		if row.get("classification") == "Conciliado":
			cleared_deposits += row.get("deposit", 0)
			cleared_withdrawals += row.get("withdrawal", 0)
			
	dep_transit = 0
	with_transit = 0
	abonos_no_reg = 0
	cargos_bancarios = 0
	for s in summary:
		if s.get("label") == "Depósitos en Tránsito":
			dep_transit = s.get("value")
		elif s.get("label") == "Cheques en Circulación":
			with_transit = s.get("value")
		elif s.get("label") == "Abonos no Registrados":
			abonos_no_reg = s.get("value")
		elif s.get("label") == "Cargos Bancarios":
			cargos_bancarios = s.get("value")
			
	prev_end_date = add_days(start_date, -1)
	prev_filters = frappe._dict({
		"company": company,
		"account": bank_account,
		"from_date": "1900-01-01",
		"to_date": prev_end_date,
		"include_reconciled": 1
	})
	
	cache_key_balance = f"bank_recon_prev_bal_{company}_{bank_account}_{prev_end_date}"
	prev_gl_balance = frappe.cache().get_value(cache_key_balance)
	if prev_gl_balance is None:
		prev_gl_balance = get_account_balance(prev_filters)
		frappe.cache().set_value(cache_key_balance, prev_gl_balance, expires_in_sec=3600)
	
	cache_key_summary = f"bank_recon_prev_sum_{company}_{bank_account}_{prev_end_date}"
	prev_summary = frappe.cache().get_value(cache_key_summary)
	if prev_summary is None:
		_, prev_summary = get_data_and_summary(prev_filters)
		frappe.cache().set_value(cache_key_summary, prev_summary, expires_in_sec=3600)
	
	prev_dep_transit = 0
	prev_with_transit = 0
	prev_abonos_no_reg = 0
	prev_cargos_bancarios = 0
	for s in prev_summary:
		if s.get("label") == "Depósitos en Tránsito":
			prev_dep_transit = s.get("value")
		elif s.get("label") == "Cheques en Circulación":
			prev_with_transit = s.get("value")
		elif s.get("label") == "Abonos no Registrados":
			prev_abonos_no_reg = s.get("value")
		elif s.get("label") == "Cargos Bancarios":
			prev_cargos_bancarios = s.get("value")
			
	initial_reconciled_balance = prev_gl_balance - prev_dep_transit + prev_with_transit + prev_abonos_no_reg - prev_cargos_bancarios
	final_reconciled_balance = initial_reconciled_balance + cleared_deposits - cleared_withdrawals
	final_book_balance = get_account_balance(filters)
	difference = bank_statement_closing_balance - final_reconciled_balance
	
	# Prepare dictionary for Jinja
	doc = frappe._dict({
		"company": company,
		"bank_account": bank_account,
		"bank_name": bank_name,
		"month": month,
		"year": year,
		"currency": account_currency,
		"bank_statement_closing_balance": bank_statement_closing_balance,
		"initial_reconciled_balance": initial_reconciled_balance,
		"cleared_deposits": cleared_deposits,
		"cleared_withdrawals": cleared_withdrawals,
		"final_reconciled_balance": final_reconciled_balance,
		"difference": difference,
		"deposits_in_transit": dep_transit,
		"withdrawals_in_transit": with_transit,
		"final_book_balance": final_book_balance,
	})
	
	html = """
<style>
	.brs-header { margin-bottom: 20px; font-family: monospace; }
	.brs-title { text-align: center; font-weight: bold; margin: 10px 0; }
	.brs-table { width: 100%; border-collapse: collapse; margin-bottom: 20px; font-family: monospace; }
	.brs-table th, .brs-table td { border-bottom: 1px solid #000; padding: 5px; text-align: left; }
	.brs-table th { border-top: 1px solid #000; }
	.brs-values { width: 100%; max-width: 500px; margin-left: 20px; font-family: monospace; }
	.brs-values td { padding: 3px 10px; }
	.brs-values .text-right { text-align: right; }
	.brs-divider { border-top: 1px solid #000; margin: 20px 0; }
</style>

<div class="brs-header">
	<div>{{ doc.company }}</div>
	<div>Cajas y Bancos</div>
	<div class="brs-title">RESUMEN CONCILIACIÓN BANCARIA</div>
	<div>Rangos: Cuenta: {{ doc.bank_account }}; Mes: {{ doc.month }}; Año: {{ doc.year }}; Saldo Estado Cuenta: {{ frappe.utils.fmt_money(doc.bank_statement_closing_balance, currency=doc.currency) }}</div>
</div>

<table class="brs-table">
	<thead>
		<tr>
			<th>Cód. Cuenta</th>
			<th>Nro. Cuenta</th>
			<th>Descripción Banco</th>
			<th>Moneda</th>
		</tr>
	</thead>
	<tbody>
		<tr>
			<td>{{ doc.bank_account }}</td>
			<td>{{ frappe.db.get_value("Bank Account", doc.bank_account, "bank_account_no") or "" }}</td>
			<td>{{ doc.bank_name }}</td>
			<td>{{ doc.currency }}</td>
		</tr>
	</tbody>
</table>

<table class="brs-values">
	<tr>
		<td>Saldo inicial conciliado</td>
		<td class="text-right">{{ frappe.utils.fmt_money(doc.initial_reconciled_balance) }}</td>
	</tr>
	<tr>
		<td>Abonos</td>
		<td class="text-right">{{ frappe.utils.fmt_money(doc.cleared_deposits) }}</td>
	</tr>
	<tr>
		<td>Cargos</td>
		<td class="text-right">{{ frappe.utils.fmt_money(doc.cleared_withdrawals) }}</td>
	</tr>
	<tr>
		<td>Saldo final conciliado</td>
		<td class="text-right">{{ frappe.utils.fmt_money(doc.final_reconciled_balance) }}</td>
	</tr>
	<tr>
		<td>Saldo final estado de cuenta</td>
		<td class="text-right">{{ frappe.utils.fmt_money(doc.bank_statement_closing_balance) }}</td>
	</tr>
	<tr>
		<td>Diferencia</td>
		<td class="text-right">{{ frappe.utils.fmt_money(doc.difference) }}</td>
	</tr>
</table>

<div class="brs-divider"></div>
<div style="margin-left: 20px; font-weight: bold; font-family: monospace;">Movimientos en tránsito</div>
<table class="brs-values" style="margin-top: 10px;">
	<tr>
		<td>Abonos</td>
		<td class="text-right">{{ frappe.utils.fmt_money(doc.deposits_in_transit) }}</td>
	</tr>
	<tr>
		<td>Cargos</td>
		<td class="text-right">{{ frappe.utils.fmt_money(doc.withdrawals_in_transit) }}</td>
	</tr>
	<tr>
		<td>Saldo final en libros</td>
		<td class="text-right">{{ frappe.utils.fmt_money(doc.final_book_balance) }}</td>
	</tr>
</table>
<div class="brs-divider"></div>
"""

	# To include standard Frappe Letterhead, we can wrap it in the standard print template
	letterhead = frappe.db.get_value("Company", company, "default_letter_head")
	
	if letterhead:
		# we need to get the letterhead HTML
		lh_doc = frappe.get_doc("Letter Head", letterhead)
		html = f"<div class='letter-head'>{lh_doc.content}</div>\n{html}"
		
	# Render the jinja inside
	rendered_html = frappe.render_template(html, {"doc": doc, "frappe": frappe})
	
	frappe.local.response.filename = f"Resumen_Conciliacion_{month}_{year}.pdf"
	frappe.local.response.filecontent = get_pdf(rendered_html)
	frappe.local.response.type = "download"
