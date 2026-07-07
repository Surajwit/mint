frappe.pages['resumen-conciliacion'].on_page_load = function(wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: 'Resumen de Conciliación Bancaria',
		single_column: true
	});

	let fieldgroup = new frappe.ui.FieldGroup({
		fields: [
			{fieldname: 'company', label: 'Compañía', fieldtype: 'Link', options: 'Company', reqd: 1},
			{fieldname: 'bank_account', label: 'Cuenta Bancaria', fieldtype: 'Link', options: 'Bank Account', reqd: 1},
			{fieldname: 'month', label: 'Mes', fieldtype: 'Select', options: 'Enero\nFebrero\nMarzo\nAbril\nMayo\nJunio\nJulio\nAgosto\nSeptiembre\nOctubre\nNoviembre\nDiciembre', reqd: 1},
			{fieldname: 'year', label: 'Año', fieldtype: 'Int', reqd: 1, default: new Date().getFullYear()},
			{fieldname: 'bank_statement_closing_balance', label: 'Saldo final estado de cuenta', fieldtype: 'Currency'}
		],
		body: page.body,
	});
	fieldgroup.make();

	page.set_primary_action('Generar PDF', () => {
		let values = fieldgroup.get_values();
		if(values) {
			// Add a nice message
			frappe.msgprint(__("Generando PDF, por favor espere..."));
			
			// Build URL for download
			let url = "/api/method/mint.mint.page.resumen_conciliacion.resumen_conciliacion.download_pdf?" + new URLSearchParams(values).toString();
			window.open(url);
		}
	});
};