from odoo import models, fields, api

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    invoice_date_import = fields.Date(string="Fecha de Factura (importación)",
                                      help="Usado solo en importación masiva para fijar la fecha de la factura.")

    @api.model
    def create(self, vals):
        order = super().create(vals)

        # Automatismos solo si viene desde el wizard de importación
        if self.env.context.get('auto_invoice_on_import'):
            # Confirmar la venta
            order.action_confirm()

            # Validar entregas incluso sin reservas: stock.immediate.transfer
            for picking in order.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel')):
                picking.action_assign()
                imt = self.env['stock.immediate.transfer'].create({'pick_ids': [(4, picking.id)]})
                imt.process()

            # Crear factura en borrador
            invoice = order._create_invoices()
            if invoice and order.invoice_date_import:
                invoice.invoice_date = order.invoice_date_import

        return order
