# models/sale_order.py
from odoo import models, fields, api

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    invoice_date_import = fields.Date(string="Fecha de Factura (importación)")

    def _validate_outgoing_pickings(self):
        """Valida pickings de salida de la orden sin depender directamente de stock.immediate.transfer."""
        for picking in self.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel')):
            # 1) Intentar reservar
            picking.action_assign()
            # 2) Cargar cantidad hecha
            for move in picking.move_ids_without_package:
                if not move.quantity_done:
                    # Si no hay líneas reservadas, escribir directamente sobre el move (Odoo crea líneas si hace falta)
                    move.quantity_done = move.product_uom_qty
            # 3) Validar
            res = picking.button_validate()
            # 4) Si devuelve un wizard (backorder o immediate), procesarlo de forma segura
            if isinstance(res, dict):
                res_model = res.get('res_model')
                res_id = res.get('res_id')
                if res_model and res_id:
                    wizard = self.env[res_model].browse(res_id)
                    # Ambos wizards (backorder / immediate) implementan 'process' en Odoo 17
                    if hasattr(wizard, 'process'):
                        wizard.process()

    @api.model
    def create(self, vals):
        order = super().create(vals)
        if self.env.context.get('auto_invoice_on_import'):
            # Confirmar venta
            order.action_confirm()
            # Validar entregas robustamente
            order._validate_outgoing_pickings()
            # Crear factura en borrador
            invoice = order._create_invoices()
            # Setear fecha de factura si viene
            if invoice and order.invoice_date_import:
                invoice.invoice_date = order.invoice_date_import
        return order
