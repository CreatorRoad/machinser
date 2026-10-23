# -*- coding: utf-8 -*-
from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    retention_account_id = fields.Many2one('account.account', string="Retention Receivable Account",check_company=True,domain="[('account_type', '=', 'asset_receivable')]")
    default_retention_percentage = fields.Float(string="Default Retention %", default=10.0)
    retention_journal_id = fields.Many2one('account.journal', string="Retention Journal", check_company=True, domain="[('type', '=', 'general')]")
    