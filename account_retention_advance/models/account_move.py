# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_is_zero, float_compare


class AccountMove(models.Model):
    _inherit = 'account.move'

    project_sale_order_id = fields.Many2one('sale.order', string="Project / Sales Order", check_company=True, compute='_compute_project_sale_order_id', store=True, readonly=False, precompute=True, domain="[('partner_id', 'child_of', commercial_partner_id)]")
    retention_percentage = fields.Float(string="Retention %", default=lambda self: self.env.company.default_retention_percentage, digits=(5, 2), copy=True,)
    retention_amount = fields.Monetary(string="Retention Amount", currency_field='currency_id', compute='_compute_retention_amount', store=True, readonly=False, copy=True,)
    handover_date = fields.Date(string="Expected Handover Date", copy=True,)
    retention_move_id = fields.Many2one('account.move', string="Retention Journal Entry", readonly=True, copy=False,)    
    retention_release_move_ids = fields.Many2many('account.move','account_move_retention_release_rel','invoice_id','release_move_id', string="Retention Release Entries", readonly=True, copy=False,)
    retention_released_amount = fields.Monetary(string="Retention Released", currency_field='currency_id', compute='_compute_retention_released_amount', store=True,)
    retention_balance = fields.Monetary(string="Retention Balance", currency_field='currency_id', compute='_compute_retention_released_amount', store=True,)
    advance_payment_id = fields.Many2one('account.payment', string="Advance Payment to Recover", check_company=True, copy=True, domain="[('partner_id', 'child_of', commercial_partner_id), ('is_advance_payment', '=', True)]",)
    advance_remaining_on_payment = fields.Monetary(string="Available Advance", related='advance_payment_id.advance_remaining_amount', currency_field='currency_id',)
    advance_recovery_type = fields.Selection([('percentage', 'Percentage of Work Done'),('fixed', 'Fixed Amount'),],string="Advance Recovery Type",default='percentage',copy=True,)
    advance_recovery_percentage = fields.Float(string="Advance Recovery %",digits=(5, 2),default=0.0,copy=True,)
    advance_recovery_amount = fields.Monetary(string="Advance Recovery Amount",currency_field='currency_id',compute='_compute_advance_recovery_amount',store=True,readonly=False,copy=True,)
    advance_reconciled_partial_id = fields.Many2one('account.partial.reconcile',string="Advance Partial Match",readonly=True,copy=False,)
    amount_to_pay_now = fields.Monetary(string="Amount to Pay Now",currency_field='currency_id',compute='_compute_amount_to_pay_now',store=True,)

    @api.depends('invoice_line_ids.sale_line_ids.order_id')
    def _compute_project_sale_order_id(self):
        for move in self:
            if move.is_sale_document(include_receipts=True):
                orders = move.invoice_line_ids.sale_line_ids.order_id
                move.project_sale_order_id = orders[:1].id if orders else (move.project_sale_order_id or False)

    @api.depends('retention_percentage', 'amount_untaxed', 'move_type')
    def _compute_retention_amount(self):
        for move in self:
            if move.is_sale_document(include_receipts=True) and move.retention_percentage:
                move.retention_amount = round(move.amount_untaxed * (move.retention_percentage / 100.0), 2)
            elif not move.retention_percentage:
                move.retention_amount = 0.0

    @api.depends('advance_payment_id','advance_recovery_type','advance_recovery_percentage','amount_untaxed','advance_payment_id.advance_remaining_amount','move_type',)
    def _compute_advance_recovery_amount(self):
        for move in self:
            if not move.is_sale_document(include_receipts=True) or not move.advance_payment_id:
                if not move.advance_payment_id:
                    move.advance_recovery_amount = 0.0
                continue

            available = move.advance_payment_id.advance_remaining_amount
            if move.advance_payment_id.currency_id != move.currency_id:
                available = move.advance_payment_id.currency_id._convert(
                    available,
                    move.currency_id,
                    move.company_id,
                    move.invoice_date or fields.Date.context_today(move),
                )

            if move.advance_recovery_type == 'percentage':
                rec_amount = round(move.amount_untaxed * (move.advance_recovery_percentage / 100.0), 2)
            else:
                rec_amount = move.advance_recovery_amount or 0.0

            move.advance_recovery_amount = min(rec_amount, available) if available > 0 else 0.0

    @api.depends('amount_total', 'retention_amount', 'advance_recovery_amount', 'move_type')
    def _compute_amount_to_pay_now(self):
        for move in self:
            if move.is_sale_document(include_receipts=True):
                deductions = (move.retention_amount or 0.0) + (move.advance_recovery_amount or 0.0)
                move.amount_to_pay_now = max(0.0, move.amount_total - deductions)
            else:
                move.amount_to_pay_now = move.amount_total

    @api.depends('retention_amount','retention_release_move_ids.state','retention_release_move_ids.line_ids.debit','retention_move_id.line_ids.amount_residual','retention_move_id.line_ids.amount_residual_currency')
    def _compute_retention_released_amount(self):
        for move in self:
            released = 0.0
            if move.retention_release_move_ids:
                ret_account = move.company_id.retention_account_id
                for rel_move in move.retention_release_move_ids.filtered(lambda m: m.state == 'posted'):
                    for line in rel_move.line_ids:
                        if line.account_id == ret_account and line.credit > 0:
                            if line.currency_id == move.currency_id:
                                released += abs(line.amount_currency)
                            else:
                                released += line.company_currency_id._convert(
                                    line.credit,
                                    move.currency_id,
                                    move.company_id,
                                    rel_move.date or fields.Date.context_today(move),
                                )
            move.retention_released_amount = released

            if move.retention_move_id:
                ret_account = move.company_id.retention_account_id
                ret_debit = move.retention_move_id.line_ids.filtered(lambda l: l.account_id == ret_account and l.debit > 0)[:1]
                if ret_debit:
                    if ret_debit.currency_id and ret_debit.currency_id == move.currency_id:
                        move.retention_balance = max(0.0, abs(ret_debit.amount_residual_currency))
                    else:
                        move.retention_balance = max(0.0, ret_debit.company_currency_id._convert(
                            ret_debit.amount_residual,
                            move.currency_id,
                            move.company_id,
                            move.invoice_date or fields.Date.context_today(move),
                        ))
                else:
                    move.retention_balance = max(0.0, (move.retention_amount or 0.0) - released)
            else:
                move.retention_balance = max(0.0, (move.retention_amount or 0.0) - released)

    @api.constrains('retention_percentage', 'advance_recovery_percentage', 'retention_amount', 'advance_recovery_amount', 'amount_total')
    def _check_retention_advance_amounts(self):
        for move in self:
            if not move.is_sale_document(include_receipts=True):
                continue
            if not (0.0 <= move.retention_percentage <= 100.0):
                raise ValidationError(_("Retention percentage must be between 0 and 100."))
            if not (0.0 <= move.advance_recovery_percentage <= 100.0):
                raise ValidationError(_("Advance recovery percentage must be between 0 and 100."))
            if move.retention_amount < 0.0:
                raise ValidationError(_("Retention amount cannot be negative."))
            if move.advance_recovery_amount < 0.0:
                raise ValidationError(_("Advance recovery amount cannot be negative."))

            if move.advance_payment_id and move.state == 'draft':
                available = move.advance_payment_id.advance_remaining_amount
                if move.advance_payment_id.currency_id != move.currency_id:
                    available = move.advance_payment_id.currency_id._convert(
                        available,
                        move.currency_id,
                        move.company_id,
                        move.invoice_date or fields.Date.context_today(move),
                    )
                if float_compare(move.advance_recovery_amount, available, precision_digits=2) > 0:
                    raise ValidationError(_(
                        "Advance recovery amount (%(recovery)s) exceeds available balance on payment (%(available)s).",
                        recovery=move.advance_recovery_amount,
                        available=available,
                    ))

            if move.amount_total > 0:
                deductions = (move.retention_amount or 0.0) + (move.advance_recovery_amount or 0.0)
                if float_compare(deductions, move.amount_total, precision_digits=2) > 0:
                    raise ValidationError(_("Retention and advance deductions cannot exceed the total invoice amount."))

    def _verify_lock_dates(self, target_date):
        self.ensure_one()
        violations = self.company_id._get_lock_date_violations(target_date)
        if violations:
            lock_date, lock_type = violations[0]
            raise UserError(_(
                "Cannot perform this action on %(date)s due to %(type)s lock date (%(lock_date)s).",
                date=target_date,
                type=lock_type.replace('_', ' '),
                lock_date=lock_date,
            ))

    def action_post(self):
        for move in self:
            move._verify_lock_dates(move.invoice_date or move.date or fields.Date.context_today(move))

        res = super().action_post()

        for move in self:
            if move.move_type == 'out_invoice':
                move._process_retention_and_advance()
            elif move.move_type == 'out_refund':
                move._process_refund_retention_and_advance()

        return res

    def _process_retention_and_advance(self):
        self.ensure_one()
        company = self.company_id

        if self.retention_amount > 0 and not self.retention_move_id:
            ret_account = company.retention_account_id
            if not ret_account:
                raise UserError(_("Please configure the Retention Receivable Account in Accounting Settings."))

            rec_line = self.line_ids.filtered(
                lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled
            )[:1]
            if not rec_line:
                raise UserError(_("No receivable line found on invoice %s.", self.name))

            journal = company.retention_journal_id or self.env['account.journal'].search([
                ('company_id', '=', company.id),
                ('type', '=', 'general'),
            ], limit=1)
            if not journal:
                raise UserError(_("No general journal found to record retention entry for %s.", company.name))

            entry_date = self.invoice_date or self.date or fields.Date.context_today(self)
            self._verify_lock_dates(entry_date)

            ret_balance = self.currency_id._convert(
                self.retention_amount,
                company.currency_id,
                company,
                entry_date,
            )

            retention_entry = self.env['account.move'].create({
                'move_type': 'entry',
                'date': entry_date,
                'journal_id': journal.id,
                'company_id': company.id,
                'ref': _("Retention held - %s", self.name),
                'line_ids': [
                    (0, 0, {
                        'name': _("Retention guarantee - %s", self.name),
                        'partner_id': self.commercial_partner_id.id,
                        'account_id': ret_account.id,
                        'debit': ret_balance,
                        'credit': 0.0,
                        'amount_currency': self.retention_amount,
                        'currency_id': self.currency_id.id,
                        'date_maturity': self.handover_date or self.invoice_date_due or entry_date,
                    }),
                    (0, 0, {
                        'name': _("Retention deduction - %s", self.name),
                        'partner_id': self.commercial_partner_id.id,
                        'account_id': rec_line.account_id.id,
                        'debit': 0.0,
                        'credit': ret_balance,
                        'amount_currency': -self.retention_amount,
                        'currency_id': self.currency_id.id,
                    }),
                ],
            })
            retention_entry.action_post()
            self.retention_move_id = retention_entry.id

            ret_credit = retention_entry.line_ids.filtered(
                lambda l: l.account_id == rec_line.account_id and l.credit > 0
            )[:1]
            if ret_credit:
                self.env['account.partial.reconcile'].create({
                    'debit_move_id': rec_line.id,
                    'credit_move_id': ret_credit.id,
                    'amount': ret_balance,
                    'debit_amount_currency': self.retention_amount,
                    'credit_amount_currency': self.retention_amount,
                })

        if self.advance_recovery_amount > 0 and self.advance_payment_id and not self.advance_reconciled_partial_id:
            advance_line = self.advance_payment_id._get_unreconciled_receivable_line()
            if not advance_line:
                raise UserError(_("No open balance found on advance payment %s.", self.advance_payment_id.name))

            rec_line = self.line_ids.filtered(
                lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled
            )[:1]
            if not rec_line:
                raise UserError(_("No open receivable line available to match advance on %s.", self.name))

            entry_date = self.invoice_date or self.date or fields.Date.context_today(self)
            adv_balance = self.currency_id._convert(
                self.advance_recovery_amount,
                company.currency_id,
                company,
                entry_date,
            )

            debit_amt_curr = self.advance_recovery_amount
            if advance_line.currency_id == self.currency_id:
                credit_amt_curr = self.advance_recovery_amount
            else:
                credit_amt_curr = self.currency_id._convert(
                    self.advance_recovery_amount,
                    advance_line.currency_id,
                    company,
                    entry_date,
                )

            partial = self.env['account.partial.reconcile'].create({
                'debit_move_id': rec_line.id,
                'credit_move_id': advance_line.id,
                'amount': adv_balance,
                'debit_amount_currency': debit_amt_curr,
                'credit_amount_currency': credit_amt_curr,
            })
            self.advance_reconciled_partial_id = partial.id

    def _process_refund_retention_and_advance(self):
        self.ensure_one()
        orig_invoice = self.reversed_entry_id
        if not orig_invoice or not orig_invoice.is_sale_document(include_receipts=True):
            return

        orig_untaxed = abs(orig_invoice.amount_untaxed)
        if float_is_zero(orig_untaxed, precision_digits=2):
            return

        ratio = min(1.0, abs(self.amount_untaxed) / orig_untaxed)
        company = self.company_id
        entry_date = self.invoice_date or self.date or fields.Date.context_today(self)
        self._verify_lock_dates(entry_date)

        if orig_invoice.retention_amount > 0 and orig_invoice.retention_move_id:
            ret_reversal = round(orig_invoice.retention_amount * ratio, 2)
            if ret_reversal > 0 and company.retention_account_id:
                ret_account = company.retention_account_id
                rec_line = self.line_ids.filtered(
                    lambda l: l.account_id.account_type == 'asset_receivable'
                )[:1]
                normal_account = rec_line.account_id if rec_line else self.commercial_partner_id.property_account_receivable_id
                journal = company.retention_journal_id or self.env['account.journal'].search([
                    ('company_id', '=', company.id),
                    ('type', '=', 'general'),
                ], limit=1)

                ret_balance = self.currency_id._convert(ret_reversal, company.currency_id, company, entry_date)

                rev_move = self.env['account.move'].create({
                    'move_type': 'entry',
                    'date': entry_date,
                    'journal_id': journal.id,
                    'company_id': company.id,
                    'ref': _("Retention reversal - %s", self.name),
                    'line_ids': [
                        (0, 0, {
                            'name': _("Retention reversal - %s", self.name),
                            'partner_id': self.commercial_partner_id.id,
                            'account_id': normal_account.id,
                            'debit': ret_balance,
                            'credit': 0.0,
                            'amount_currency': ret_reversal,
                            'currency_id': self.currency_id.id,
                        }),
                        (0, 0, {
                            'name': _("Retention reduction - %s", self.name),
                            'partner_id': self.commercial_partner_id.id,
                            'account_id': ret_account.id,
                            'debit': 0.0,
                            'credit': ret_balance,
                            'amount_currency': -ret_reversal,
                            'currency_id': self.currency_id.id,
                        }),
                    ],
                })
                rev_move.action_post()

                orig_ret_debit = orig_invoice.retention_move_id.line_ids.filtered(
                    lambda l: l.account_id == ret_account and l.debit > 0
                )[:1]
                rev_ret_credit = rev_move.line_ids.filtered(
                    lambda l: l.account_id == ret_account and l.credit > 0
                )[:1]
                if orig_ret_debit and rev_ret_credit:
                    self.env['account.partial.reconcile'].create({
                        'debit_move_id': orig_ret_debit.id,
                        'credit_move_id': rev_ret_credit.id,
                        'amount': ret_balance,
                        'debit_amount_currency': ret_reversal,
                        'credit_amount_currency': ret_reversal,
                    })

                rev_normal_debit = rev_move.line_ids.filtered(
                    lambda l: l.account_id == normal_account and l.debit > 0
                )[:1]
                cn_credit_line = self.line_ids.filtered(
                    lambda l: l.account_id == normal_account and l.credit > 0
                )[:1]
                if rev_normal_debit and cn_credit_line:
                    self.env['account.partial.reconcile'].create({
                        'debit_move_id': rev_normal_debit.id,
                        'credit_move_id': cn_credit_line.id,
                        'amount': ret_balance,
                        'debit_amount_currency': ret_reversal,
                        'credit_amount_currency': ret_reversal,
                    })

        if orig_invoice.advance_recovery_amount > 0 and orig_invoice.advance_reconciled_partial_id:
            adv_reversal = round(orig_invoice.advance_recovery_amount * ratio, 2)
            partial = orig_invoice.advance_reconciled_partial_id
            if partial.exists():
                if ratio >= 0.999:
                    orig_invoice.advance_reconciled_partial_id = False
                    partial.unlink()
                else:
                    rev_balance = self.currency_id._convert(adv_reversal, company.currency_id, company, entry_date)
                    new_amount = max(0.0, partial.amount - rev_balance)
                    new_debit_curr = max(0.0, partial.debit_amount_currency - adv_reversal)
                    new_credit_curr = max(0.0, partial.credit_amount_currency - adv_reversal)
                    debit_line = partial.debit_move_id
                    credit_line = partial.credit_move_id
                    partial.unlink()
                    if not float_is_zero(new_amount, precision_digits=2):
                        new_partial = self.env['account.partial.reconcile'].create({
                            'debit_move_id': debit_line.id,
                            'credit_move_id': credit_line.id,
                            'amount': new_amount,
                            'debit_amount_currency': new_debit_curr,
                            'credit_amount_currency': new_credit_curr,
                        })
                        orig_invoice.advance_reconciled_partial_id = new_partial.id
                    else:
                        orig_invoice.advance_reconciled_partial_id = False

    def button_draft(self):
        for move in self:
            if move.retention_released_amount > 0:
                raise UserError(_(
                    "Cannot reset invoice %(inv)s to draft because %(amount)s %(symbol)s of retention has already been released. "
                    "Please cancel or reverse the retention release entry first.",
                    inv=move.name,
                    amount=move.retention_released_amount,
                    symbol=move.currency_id.symbol,
                ))

            if move.retention_move_id:
                ret_move = move.retention_move_id
                move.retention_move_id = False
                ret_move.line_ids.remove_move_reconcile()
                if ret_move.state == 'posted':
                    ret_move.button_draft()
                ret_move.unlink()

            if move.advance_reconciled_partial_id:
                partial = move.advance_reconciled_partial_id
                move.advance_reconciled_partial_id = False
                if partial.exists():
                    partial.unlink()

        return super().button_draft()

    def action_view_retention_entry(self):
        self.ensure_one()
        if not self.retention_move_id:
            raise UserError(_("No retention journal entry found for this invoice."))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Retention Entry"),
            'res_model': 'account.move',
            'view_mode': 'form',
            'res_id': self.retention_move_id.id,
        }

    def action_view_retention_releases(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Retention Releases"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.retention_release_move_ids.ids)],
        }

    def action_release_retention_single(self):
        self.ensure_one()
        if self.retention_balance <= 0:
            raise UserError(_("No retention balance remaining to release on this invoice."))
        return {
            'name': _("Release Retention"),
            'type': 'ir.actions.act_window',
            'res_model': 'retention.release.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_partner_id': self.partner_id.id,
                'default_project_sale_order_id': self.project_sale_order_id.id if self.project_sale_order_id else False,
                'default_invoice_ids': [(6, 0, self.ids)],
                'active_model': 'account.move',
                'active_ids': self.ids,
                'active_id': self.id,
            },
        }
