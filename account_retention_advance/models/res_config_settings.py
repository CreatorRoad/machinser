# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    retention_account_id = fields.Many2one(related='company_id.retention_account_id',string="Retention Receivable Account",readonly=False,check_company=True,domain="[('account_type', '=', 'asset_receivable')]")
    retention_percentage = fields.Float(related='company_id.default_retention_percentage',string="Default Retention %",readonly=False,)
    retention_journal_id = fields.Many2one(related='company_id.retention_journal_id',string="Retention Journal",readonly=False,check_company=True,domain="[('type', '=', 'general')]")
