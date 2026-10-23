# -*- coding: utf-8 -*-
from odoo import api, fields, models


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    sale_order_id = fields.Many2one('sale.order',string="Project / Sales Order",check_company=True,domain="[('partner_id', 'child_of', partner_id)]",)
    advance_remaining_amount = fields.Monetary(string="Remaining Advance",compute='_compute_advance_remaining_amount',currency_field='currency_id',store=True,)
    is_advance_payment = fields.Boolean(string="Is Advance Payment",compute='_compute_advance_remaining_amount',store=True,)

    @api.depends('payment_type','partner_type','state','amount','currency_id','move_id.line_ids.amount_residual','move_id.line_ids.amount_residual_currency','move_id.line_ids.reconciled',)
    def _compute_advance_remaining_amount(self):
        for payment in self:
            if (payment.payment_type == 'inbound' and payment.partner_type == 'customer' and payment.state in ('in_process', 'paid') and payment.move_id):
                receivable_lines = payment.move_id.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled)
                remaining = 0.0
                for line in receivable_lines:
                    if line.currency_id and line.currency_id == payment.currency_id:
                        remaining += abs(line.amount_residual_currency)
                    else:
                        remaining += abs(line.amount_residual)
                payment.advance_remaining_amount = remaining
                payment.is_advance_payment = remaining > 0
            else:
                payment.advance_remaining_amount = 0.0
                payment.is_advance_payment = False

    def _get_unreconciled_receivable_line(self):
        self.ensure_one()
        if not self.move_id:
            return self.env['account.move.line']
        return self.move_id.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled and (l.credit > 0 or l.amount_residual < 0))[:1]

    def action_post(self):
        for pay in self:
            if not pay.outstanding_account_id:
                outstanding_acc = pay._get_outstanding_account(pay.payment_type)
                if outstanding_acc:
                    pay.outstanding_account_id = outstanding_acc.id
        res = super().action_post()
        for pay in self:
            if not pay.move_id and pay.outstanding_account_id and pay.state in ('in_process', 'paid'):
                pay._generate_journal_entry()
                pay.move_id.filtered(lambda m: m.state == 'draft').action_post()
        return res
