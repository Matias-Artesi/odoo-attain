from odoo import models, fields, api

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    invoice_date_import = fields.Date(string="Fecha de Factura (importación)")

    def _validate_outgoing_pickings(self):
        """Valida pickings de salida sin tocar campos frágiles.
        1) Reserva, 2) Setea cantidades hechas = reservadas, 3) Valida,
        4) Si hay wizard (inmediata/backorder), lo procesa.
        """
        for picking in self.picking_ids.filtered(
            lambda p: p.picking_type_code == 'outgoing' and p.state not in ('done', 'cancel')
        ):
            # 1) Intentar reservar
            picking.action_assign()

            # 2) Dejar qty_done = qty_reservada (maneja move lines internamente)
            #    Evitamos escribir qty_done/reserved_uom_qty/product_uom_qty a mano
            try:
                picking.action_set_quantities_to_reservation()
            except Exception:
                # Fallback defensivo: si por alguna razón no existe el método (custom raro)
                # intentamos al menos asegurar que existan move lines
                for move in picking.move_ids_without_package:
                    if not move.move_line_ids:
                        self.env['stock.move.line'].create({
                            'move_id': move.id,
                            'picking_id': picking.id,
                            'product_id': move.product_id.id,
                            'location_id': move.location_id.id,
                            'location_dest_id': move.location_dest_id.id,
                            'product_uom_id': move.product_uom.id,
                            # qty_done se completará al validar vía wizard si hace falta
                            'qty_done': 0.0,
                        })

            # 3) Validar
            res = picking.button_validate()

            # 4) Si Odoo devuelve un wizard (inmediata/backorder), procesarlo genéricamente
            if isinstance(res, dict):
                model = res.get('res_model')
                res_id = res.get('res_id')
                if model and res_id:
                    wiz = self.env[model].browse(res_id)
                    for method in ('process', 'process_cancel_backorder', 'action_validate'):
                        if hasattr(wiz, method):
                            getattr(wiz, method)()
                            break

    @api.model
    def create(self, vals):
        order = super().create(vals)
        if self.env.context.get('auto_invoice_on_import'):
            order.action_confirm()                 # crea pickings/moves
            order._validate_outgoing_pickings()    # validación robusta sin tocar fields frágiles
            invoice = order._create_invoices()     # factura borrador
            if invoice and order.invoice_date_import:
                invoice.invoice_date = order.invoice_date_import
        return order
