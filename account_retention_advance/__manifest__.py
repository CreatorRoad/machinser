{
    'name': 'Retention and Advance on Invoices',
    'version': '19.0.1.0.0',
    'category': 'Accounting',
    'summary': 'Retention guarantee and advance recovery management for contractor invoices',
    'author': 'Sajith',
    'license': 'LGPL-3',
    'depends': [
        'account',
        'sale',
    ],
    'data': [
        'security/ir.model.access.csv',
        'views/res_config_settings_views.xml',
        'views/account_move_views.xml',
        'views/account_payment_views.xml',
        'views/retention_release_views.xml',
        'views/retention_advance_report_views.xml',
        'report/report_invoice.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
