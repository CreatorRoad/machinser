# -*- coding: utf-8 -*-
import base64
import io
from odoo import api, fields, models, _
from odoo.exceptions import UserError

try:
    import xlsxwriter
except ImportError:
    xlsxwriter = None


class RetentionAdvanceReportWizard(models.TransientModel):
    _name = 'retention.advance.report.wizard'
    _description = "Retention and Advance Report"

    date_to = fields.Date(string="As of Date",required=True,default=fields.Date.context_today)
    partner_id = fields.Many2one('res.partner',string="Customer")
    project_sale_order_id = fields.Many2one('sale.order',string="Project / Sales Order",domain="[('partner_id', 'child_of', partner_id)] if partner_id else []",)
    company_id = fields.Many2one('res.company',string="Company",required=True,default=lambda self: self.env.company,)
    currency_id = fields.Many2one(related='company_id.currency_id',string="Currency",)
    excel_file = fields.Binary(string="Excel File",readonly=True,)
    file_name = fields.Char(string="File Name",readonly=True,)
    line_ids = fields.One2many('retention.advance.report.line','wizard_id',string="Report Lines",readonly=True,)

    def _get_report_data(self):
        self.ensure_one()
        date_to = self.date_to
        company = self.company_id
        company_curr = company.currency_id

        # Invoices and credit notes up to report date
        inv_domain = [
            ('move_type', 'in', ('out_invoice', 'out_refund')),
            ('state', '=', 'posted'),
            ('company_id', '=', company.id),
            ('invoice_date', '<=', date_to),
        ]
        if self.partner_id:
            inv_domain.append(('commercial_partner_id', '=', self.partner_id.commercial_partner_id.id))
        if self.project_sale_order_id:
            inv_domain.append(('project_sale_order_id', '=', self.project_sale_order_id.id))

        moves = self.env['account.move'].search(inv_domain, order='invoice_date asc, id asc')

        # Advance payments up to report date
        pay_domain = [
            ('payment_type', '=', 'inbound'),
            ('partner_type', '=', 'customer'),
            ('state', 'in', ('in_process', 'paid')),
            ('company_id', '=', company.id),
            ('date', '<=', date_to),
        ]
        if self.partner_id:
            pay_domain.append(('partner_id', 'child_of', self.partner_id.commercial_partner_id.id))
        if self.project_sale_order_id:
            pay_domain.append(('sale_order_id', '=', self.project_sale_order_id.id))

        payments = self.env['account.payment'].search(pay_domain, order='date asc, id asc')

        data = {}

        def _get_bucket(partner, project):
            key = (partner.id, project.id if project else False)
            if key not in data:
                data[key] = {
                    'partner': partner,
                    'project': project,
                    'total_invoiced_untaxed': 0.0,
                    'total_invoiced': 0.0,
                    'retention_held': 0.0,
                    'retention_released': 0.0,
                    'advance_received': 0.0,
                    'advance_recovered': 0.0,
                }
            return data[key]

        for move in moves:
            partner = move.commercial_partner_id
            project = move.project_sale_order_id
            bucket = _get_bucket(partner, project)

            move_date = move.invoice_date or move.date
            untaxed_comp = move.currency_id._convert(move.amount_untaxed, company_curr, company, move_date)
            total_comp = move.currency_id._convert(move.amount_total, company_curr, company, move_date)
            ret_comp = move.currency_id._convert(move.retention_amount or 0.0, company_curr, company, move_date)
            adv_comp = move.currency_id._convert(move.advance_recovery_amount or 0.0, company_curr, company, move_date)

            if move.move_type == 'out_invoice':
                bucket['total_invoiced_untaxed'] += untaxed_comp
                bucket['total_invoiced'] += total_comp
                bucket['retention_held'] += ret_comp
                bucket['advance_recovered'] += adv_comp

                if move.retention_release_move_ids:
                    ret_account = company.retention_account_id
                    for rel_move in move.retention_release_move_ids:
                        if rel_move.state == 'posted' and rel_move.date <= date_to:
                            for rline in rel_move.line_ids:
                                if rline.account_id == ret_account and rline.credit > 0:
                                    rel_comp = rline.company_currency_id._convert(
                                        rline.credit, company_curr, company, rel_move.date
                                    )
                                    bucket['retention_released'] += rel_comp

            elif move.move_type == 'out_refund':
                bucket['total_invoiced_untaxed'] -= untaxed_comp
                bucket['total_invoiced'] -= total_comp

                orig = move.reversed_entry_id
                if orig and orig.amount_untaxed:
                    ratio = min(1.0, abs(move.amount_untaxed) / abs(orig.amount_untaxed))
                    ret_rev_comp = move.currency_id._convert(
                        orig.retention_amount * ratio, company_curr, company, move_date
                    )
                    adv_rev_comp = move.currency_id._convert(
                        orig.advance_recovery_amount * ratio, company_curr, company, move_date
                    )
                    bucket['retention_held'] -= ret_rev_comp
                    bucket['advance_recovered'] -= adv_rev_comp

        for pay in payments:
            partner = pay.partner_id.commercial_partner_id
            project = pay.sale_order_id
            bucket = _get_bucket(partner, project)

            pay_comp = pay.currency_id._convert(pay.amount, company_curr, company, pay.date)
            bucket['advance_received'] += pay_comp

        results = []
        for key, val in data.items():
            ret_bal = val['retention_held'] - val['retention_released']
            adv_bal = val['advance_received'] - val['advance_recovered']
            if any([
                val['total_invoiced'],
                val['retention_held'],
                val['retention_released'],
                val['advance_received'],
                val['advance_recovered'],
            ]):
                results.append({
                    'partner_id': val['partner'].id,
                    'partner_name': val['partner'].name,
                    'project_id': val['project'].id if val['project'] else False,
                    'project_name': val['project'].name if val['project'] else _("General / No Project"),
                    'total_invoiced_untaxed': val['total_invoiced_untaxed'],
                    'total_invoiced': val['total_invoiced'],
                    'retention_held': val['retention_held'],
                    'retention_released': val['retention_released'],
                    'retention_balance': ret_bal,
                    'advance_received': val['advance_received'],
                    'advance_recovered': val['advance_recovered'],
                    'advance_balance': adv_bal,
                })

        results.sort(key=lambda r: (r['partner_name'] or '', r['project_name'] or ''))
        return results

    def action_view_report(self):
        self.ensure_one()
        data = self._get_report_data()
        lines = [
            (0, 0, {
                'partner_id': row['partner_id'],
                'project_sale_order_id': row['project_id'],
                'total_invoiced_untaxed': row['total_invoiced_untaxed'],
                'total_invoiced': row['total_invoiced'],
                'retention_held': row['retention_held'],
                'retention_released': row['retention_released'],
                'retention_balance': row['retention_balance'],
                'advance_received': row['advance_received'],
                'advance_recovered': row['advance_recovered'],
                'advance_balance': row['advance_balance'],
                'currency_id': self.currency_id.id,
            })
            for row in data
        ]
        self.line_ids = [(5, 0, 0)] + lines
        return {
            'type': 'ir.actions.act_window',
            'name': _("Retention & Advance Report (%s)", self.date_to),
            'res_model': 'retention.advance.report.wizard',
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_export_excel(self):
        self.ensure_one()
        if not xlsxwriter:
            raise UserError(_("Please install xlsxwriter python package to export to Excel."))

        data = self._get_report_data()
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        worksheet = workbook.add_worksheet(_("Retention & Advance"))

        title_format = workbook.add_format({'bold': True,'font_size': 14,'font_name': 'Arial','font_color': '#1f2937',})
        subtitle_format = workbook.add_format({'font_size': 10,'font_name': 'Arial','font_color': '#4b5563',})
        header_format = workbook.add_format({'bold': True,'font_name': 'Arial','font_size': 10,'bg_color': '#1e3a8a','font_color': '#ffffff','align': 'center','valign': 'vcenter','border': 1,'text_wrap': True,})
        text_format = workbook.add_format({'font_name': 'Arial','font_size': 10,'border': 1,})
        num_format = workbook.add_format({'font_name': 'Arial','font_size': 10,'num_format': '#,##0.00','border': 1,'align': 'right',})
        total_label_format = workbook.add_format({'bold': True,'font_name': 'Arial','font_size': 10,'bg_color': '#e5e7eb','border': 1,'align': 'left',})
        total_num_format = workbook.add_format({'bold': True,'font_name': 'Arial','font_size': 10,'num_format': '#,##0.00','bg_color': '#e5e7eb','border': 1,'align': 'right',})

        worksheet.write(0, 0, self.company_id.name, title_format)
        worksheet.write(1, 0, _("Retention and Advance Report - As of %s", self.date_to), subtitle_format)
        worksheet.write(2, 0, _("Currency: %s", self.currency_id.name), subtitle_format)

        headers = [
            _("Customer"),
            _("Project"),
            _("Total Invoiced (Work)"),
            _("Total Invoiced (Gross)"),
            _("Retention Held"),
            _("Retention Released"),
            _("Retention Balance"),
            _("Advance Received"),
            _("Advance Recovered"),
            _("Advance Balance"),
        ]

        row_idx = 4
        worksheet.set_row(row_idx, 26)
        for col_idx, header in enumerate(headers):
            worksheet.write(row_idx, col_idx, header, header_format)

        start_row = row_idx + 1
        for row in data:
            row_idx += 1
            worksheet.write(row_idx, 0, row['partner_name'], text_format)
            worksheet.write(row_idx, 1, row['project_name'], text_format)
            worksheet.write(row_idx, 2, row['total_invoiced_untaxed'], num_format)
            worksheet.write(row_idx, 3, row['total_invoiced'], num_format)
            worksheet.write(row_idx, 4, row['retention_held'], num_format)
            worksheet.write(row_idx, 5, row['retention_released'], num_format)
            worksheet.write(row_idx, 6, row['retention_balance'], num_format)
            worksheet.write(row_idx, 7, row['advance_received'], num_format)
            worksheet.write(row_idx, 8, row['advance_recovered'], num_format)
            worksheet.write(row_idx, 9, row['advance_balance'], num_format)

        if data:
            row_idx += 1
            worksheet.write(row_idx, 0, _("Total"), total_label_format)
            worksheet.write(row_idx, 1, "", total_label_format)
            for col_idx in range(2, 10):
                col_letter = chr(ord('A') + col_idx)
                formula = f"=SUM({col_letter}{start_row + 1}:{col_letter}{row_idx})"
                worksheet.write_formula(row_idx, col_idx, formula, total_num_format)

        col_widths = [26, 24, 18, 18, 16, 16, 16, 16, 16, 16]
        for idx, width in enumerate(col_widths):
            worksheet.set_column(idx, idx, width)

        workbook.close()
        output.seek(0)
        file_data = base64.b64encode(output.read())
        output.close()

        file_name = f"Retention_Advance_Report_{self.date_to}.xlsx"
        self.write({
            'excel_file': file_data,
            'file_name': file_name,
        })

        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/?model=retention.advance.report.wizard&id={self.id}&field=excel_file&download=true&filename={file_name}',
            'target': 'self',
        }


class RetentionAdvanceReportLine(models.TransientModel):
    _name = 'retention.advance.report.line'
    _description = "Retention and Advance Report Line"

    wizard_id = fields.Many2one('retention.advance.report.wizard',required=True,ondelete='cascade')
    partner_id = fields.Many2one('res.partner',string="Customer")
    project_sale_order_id = fields.Many2one('sale.order',string="Project / Sales Order")
    currency_id = fields.Many2one('res.currency',string="Currency")
    total_invoiced_untaxed = fields.Monetary(string="Total Invoiced (Work)",currency_field='currency_id')
    total_invoiced = fields.Monetary(string="Total Invoiced (Gross)",currency_field='currency_id')
    retention_held = fields.Monetary(string="Retention Held",currency_field='currency_id')
    retention_released = fields.Monetary(string="Retention Released",currency_field='currency_id')
    retention_balance = fields.Monetary(string="Retention Balance",currency_field='currency_id')
    advance_received = fields.Monetary(string="Advance Received",currency_field='currency_id')
    advance_recovered = fields.Monetary(string="Advance Recovered",currency_field='currency_id')
    advance_balance = fields.Monetary(string="Advance Balance",currency_field='currency_id')
