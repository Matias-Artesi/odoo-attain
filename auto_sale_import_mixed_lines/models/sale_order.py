from odoo import models, fields, api

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    invoice_date_import = fields.Date(
        string="Fecha de Factura (importación)",
        help="Usado solo en importación masiva para fijar la fecha de la factura creada."
    )

    @api.model
    def create(self, vals):
        # Crear el pedido normalmente
        sale_order = super().create(vals)

        # Automatismos solo cuando lo pide el contexto (desde el wizard)
        if self.env.context.get('auto_invoice_on_import'):
            # Confirmar la venta
            sale_order.action_confirm()

            # Validar entregas: usar transferencia inmediata para evitar problemas de reservas
            for picking in sale_order.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel')):
                # Intentar asignar y luego procesar transferencia inmediata
                try:
                    picking.action_assign()
                except Exception:
                    # Si falla la asignación, igual intentamos transferencia inmediata
                    pass
                imd = self.env['stock.immediate.transfer'].create({'pick_ids': [(4, picking.id)]})
                imd.process()

            # Crear factura en borrador
            invoice = sale_order._create_invoices()
            if invoice and sale_order.invoice_date_import:
                # Fijar la fecha de factura si fue cargada
                invoice.invoice_date = sale_order.invoice_date_import

        return sale_order
