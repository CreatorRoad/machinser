# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare


class RetentionReleaseWizard(models.TransientModel):
    _name = 'retention.release.wizard'
    _description = "Release Customer Retention"

    partner_id = fields.Many2one('res.partner',string="Customer",required=True,)
    project_sale_order_id = fields.Many2one('sale.order',string="Project / Sales Order",domain="[('partner_id', 'child_of', partner_id)]",)
    date = fields.Date(string="Release Date",required=True,default=fields.Date.context_today,)
    journal_id = fields.Many2one('account.journal',string="Journal",required=True,domain="[('type', '=', 'general'), ('company_id', '=', company_id)]",default=lambda self: self._default_journal_id())
    company_id = fields.Many2one('res.company',string="Company",required=True,default=lambda self: self.env.company,)
    currency_id = fields.Many2one(related='company_id.currency_id',string="Company Currency")
    line_ids = fields.One2many('retention.release.wizard.line','wizard_id',string="Invoices")
    total_amount_to_release = fields.Monetary(string="Total to Release",compute='_compute_total_amount_to_release',currency_field='currency_id')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        active_ids = self.env.context.get('active_ids') or ([self.env.context.get('active_id')] if self.env.context.get('active_id') else [])
        active_model = self.env.context.get('active_model')

        invoices = self.env['account.move']
        default_inv_ctx = self.env.context.get('default_invoice_ids')
        if default_inv_ctx and isinstance(default_inv_ctx, list) and len(default_inv_ctx) > 0 and len(default_inv_ctx[0]) > 2:
            invoices = self.env['account.move'].browse(default_inv_ctx[0][2]).filtered(
                lambda m: m.move_type == 'out_invoice' and m.state == 'posted' and m.retention_balance > 0
            )
        elif active_model == 'account.move' and active_ids:
            invoices = self.env['account.move'].browse(active_ids).filtered(
                lambda m: m.move_type == 'out_invoice' and m.state == 'posted' and m.retention_balance > 0
            )
        elif res.get('partner_id'):
            partner = self.env['res.partner'].browse(res['partner_id'])
            domain = [
                ('move_type', '=', 'out_invoice'),
                ('state', '=', 'posted'),
                ('commercial_partner_id', '=', partner.commercial_partner_id.id),
                ('retention_balance', '>', 0),
            ]
            if res.get('project_sale_order_id'):
                domain.append(('project_sale_order_id', '=', res['project_sale_order_id']))
            invoices = self.env['account.move'].search(domain)

        if invoices:
            if not res.get('partner_id'):
                res['partner_id'] = invoices[0].commercial_partner_id.id
            if not res.get('project_sale_order_id') and invoices[0].project_sale_order_id:
                res['project_sale_order_id'] = invoices[0].project_sale_order_id.id
            if 'line_ids' in fields_list:
                res['line_ids'] = [
                    (0, 0, {
                        'invoice_id': inv.id,
                        'amount_to_release': inv.retention_balance,
                    })
                    for inv in invoices
                ]

        return res

    @api.model
    def _default_journal_id(self):
        company = self.env.company
        return company.retention_journal_id or self.env['account.journal'].search([
            ('company_id', '=', company.id),
            ('type', '=', 'general'),
        ], limit=1)

    @api.depends('line_ids.amount_to_release', 'line_ids.currency_id')
    def _compute_total_amount_to_release(self):
        for wizard in self:
            total = 0.0
            for line in wizard.line_ids:
                if line.amount_to_release > 0:
                    if line.currency_id == wizard.currency_id:
                        total += line.amount_to_release
                    else:
                        total += line.currency_id._convert(
                            line.amount_to_release,
                            wizard.currency_id,
                            wizard.company_id,
                            wizard.date or fields.Date.context_today(wizard),
                        )
            wizard.total_amount_to_release = total

    @api.onchange('partner_id', 'project_sale_order_id')
    def _onchange_partner_or_project(self):
        if not self.partner_id:
            self.line_ids = [(5, 0, 0)]
            return

        domain = [
            ('move_type', '=', 'out_invoice'),
            ('state', '=', 'posted'),
            ('commercial_partner_id', '=', self.partner_id.commercial_partner_id.id),
            ('retention_balance', '>', 0),
            ('company_id', '=', self.company_id.id),
        ]
        if self.project_sale_order_id:
            domain.append(('project_sale_order_id', '=', self.project_sale_order_id.id))

        if self.env.context.get('default_invoice_ids') and isinstance(self.env.context.get('default_invoice_ids'), list):
            inv_ctx_ids = self.env.context.get('default_invoice_ids')[0][2]
            if inv_ctx_ids:
                domain.append(('id', 'in', inv_ctx_ids))

        invoices = self.env['account.move'].search(domain)
        self.line_ids = [(5, 0, 0)] + [
            (0, 0, {
                'invoice_id': inv.id,
                'amount_to_release': inv.retention_balance,
            })
            for inv in invoices
        ]

    def action_release_retention(self):
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_("No invoices selected for retention release."))

        company = self.company_id
        ret_account = company.retention_account_id
        if not ret_account:
            raise UserError(_("Please configure the Retention Receivable Account in Settings."))

        violations = company._get_lock_date_violations(self.date)
        if violations:
            lock_date, lock_type = violations[0]
            raise UserError(_(
                "Cannot release retention on %(date)s due to %(type)s lock date (%(lock_date)s).",
                date=self.date,
                type=lock_type.replace('_', ' '),
                lock_date=lock_date,
            ))

        lines = self.line_ids.filtered(lambda l: l.amount_to_release > 0)
        if not lines:
            raise UserError(_("Please specify an amount to release on at least one line."))

        created_moves = self.env['account.move']

        for line in lines:
            inv = line.invoice_id
            if float_compare(line.amount_to_release, line.retention_balance, precision_digits=2) > 0:
                raise ValidationError(_(
                    "Amount to release (%(rel)s) cannot exceed the retention balance (%(bal)s) on invoice %(inv)s.",
                    rel=line.amount_to_release,
                    bal=line.retention_balance,
                    inv=inv.name,
                ))

            rec_line = inv.line_ids.filtered(
                lambda l: l.account_id.account_type == 'asset_receivable'
            )[:1]
            normal_account = rec_line.account_id if rec_line else inv.partner_id.property_account_receivable_id

            comp_amount = inv.currency_id._convert(
                line.amount_to_release,
                company.currency_id,
                company,
                self.date,
            )

            release_move = self.env['account.move'].create({
                'move_type': 'entry',
                'date': self.date,
                'journal_id': self.journal_id.id,
                'company_id': company.id,
                'ref': _("Retention release - %s", inv.name),
                'line_ids': [
                    (0, 0, {
                        'name': _("Retention released - %s", inv.name),
                        'partner_id': inv.commercial_partner_id.id,
                        'account_id': normal_account.id,
                        'debit': comp_amount,
                        'credit': 0.0,
                        'amount_currency': line.amount_to_release,
                        'currency_id': inv.currency_id.id,
                        'date_maturity': self.date,
                    }),
                    (0, 0, {
                        'name': _("Retention clearing - %s", inv.name),
                        'partner_id': inv.commercial_partner_id.id,
                        'account_id': ret_account.id,
                        'debit': 0.0,
                        'credit': comp_amount,
                        'amount_currency': -line.amount_to_release,
                        'currency_id': inv.currency_id.id,
                    }),
                ],
            })
            release_move.action_post()
            created_moves += release_move

            orig_debit = inv.retention_move_id.line_ids.filtered(
                lambda l: l.account_id == ret_account and l.debit > 0
            )[:1]
            rel_credit = release_move.line_ids.filtered(
                lambda l: l.account_id == ret_account and l.credit > 0
            )[:1]
            if orig_debit and rel_credit:
                self.env['account.partial.reconcile'].create({
                    'debit_move_id': orig_debit.id,
                    'credit_move_id': rel_credit.id,
                    'amount': comp_amount,
                    'debit_amount_currency': line.amount_to_release,
                    'credit_amount_currency': line.amount_to_release,
                })

            inv.write({'retention_release_move_ids': [(4, release_move.id)]})

        if len(created_moves) == 1:
            return {
                'name': _("Retention Release Entry"),
                'type': 'ir.actions.act_window',
                'res_model': 'account.move',
                'view_mode': 'form',
                'res_id': created_moves.id,
            }
        return {
            'name': _("Retention Release Entries"),
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('id', 'in', created_moves.ids)],
        }


class RetentionReleaseWizardLine(models.TransientModel):
    _name = 'retention.release.wizard.line'
    _description = "Retention Release Line"

    wizard_id = fields.Many2one('retention.release.wizard',required=True,ondelete='cascade')
    invoice_id = fields.Many2one('account.move',string="Invoice",required=True,domain="[('move_type', '=', 'out_invoice'), ('state', '=', 'posted')]")
    invoice_date = fields.Date(related='invoice_id.invoice_date',string="Invoice Date")
    project_sale_order_id = fields.Many2one(related='invoice_id.project_sale_order_id',string="Project / Sales Order")
    currency_id = fields.Many2one(related='invoice_id.currency_id',string="Currency")
    retention_held = fields.Monetary(string="Retention Held",related='invoice_id.retention_amount',currency_field='currency_id',)
    retention_already_released = fields.Monetary(string="Already Released",related='invoice_id.retention_released_amount',currency_field='currency_id')
    retention_balance = fields.Monetary(string="Retention Balance",related='invoice_id.retention_balance',currency_field='currency_id')
    amount_to_release = fields.Monetary(string="Amount to Release",currency_field='currency_id',required=True,default=0.0)
