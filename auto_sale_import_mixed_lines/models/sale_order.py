from odoo import models, fields, api

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    invoice_date_import = fields.Date(string="Fecha de Factura (importación)",
                                      help="Usado para fijar la fecha de la factura creada automáticamente en importación.")

    @api.model
    def create(self, vals):
        order = super().create(vals)
        if self.env.context.get('auto_invoice_on_import'):
            # Confirmar venta
            order.action_confirm()

            # Validar entregas con immediate transfer (aunque no haya reservas)
            for picking in order.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel')):
                try:
                    picking.action_assign()
                except Exception:
                    pass
                imt = self.env['stock.immediate.transfer'].create({'pick_ids': [(4, picking.id)]})
                imt.process()

            # Crear factura en borrador
            invoice = order._create_invoices()
            if invoice and order.invoice_date_import:
                invoice.invoice_date = order.invoice_date_import
        return order