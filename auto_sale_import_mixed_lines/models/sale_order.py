from odoo import models, fields, api

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    invoice_date_import = fields.Date(string="Fecha de Factura (importación)", help="Usado solo en importación masiva")

    @api.model
    def create(self, vals):
        sale_order = super().create(vals)
        if self.env.context.get('auto_invoice_on_import'):
            sale_order.action_confirm()
            for picking in sale_order.picking_ids.filtered(lambda p: p.state not in ('done','cancel')):
                picking.action_assign()
                imd = self.env['stock.immediate.transfer'].create({'pick_ids': [(4, picking.id)]})
                imd.process()
            invoice = sale_order._create_invoices()
            if invoice and sale_order.invoice_date_import:
                invoice.invoice_date = sale_order.invoice_date_import
        return sale_order
