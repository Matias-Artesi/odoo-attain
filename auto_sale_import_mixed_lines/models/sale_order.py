# models/sale_order.py
from odoo import models, fields, api

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    invoice_date_import = fields.Date(string="Fecha de Factura (importación)")

    def _validate_outgoing_pickings(self):
        for picking in self.picking_ids.filtered(lambda p: p.picking_type_code == 'outgoing' and p.state not in ('done', 'cancel')):
            # 1) Intentar reservar
            picking.action_assign()

            # 2) Completar qty_done si hace falta (para evitar wizard)
            for move in picking.move_ids_without_package:
                # Si el producto requiere lote/serie, conviene NO automatizar sin datos de lote
                if move.product_id.tracking != 'none':
                    # Podés registrar un error aquí y saltar
                    continue

                if move.move_line_ids:
                    for ml in move.move_line_ids:
                        if not ml.qty_done:
                            ml.qty_done = ml.reserved_uom_qty or ml.product_uom_qty or move.product_uom_qty
                else:
                    # Sin move lines: creamos una línea con la cantidad total pedida
                    self.env['stock.move.line'].create({
                        'move_id': move.id,
                        'product_id': move.product_id.id,
                        'picking_id': picking.id,
                        'location_id': move.location_id.id,
                        'location_dest_id': move.location_dest_id.id,
                        'product_uom_id': move.product_uom.id,
                        'qty_done': move.product_uom_qty,
                    })

            # 3) Validar
            res = picking.button_validate()

            # 4) Si hay wizard (immediate/backorder), procesarlo
            if isinstance(res, dict):
                model = res.get('res_model')
                res_id = res.get('res_id')
                if model and res_id:
                    wiz = self.env[model].browse(res_id)
                    if hasattr(wiz, 'process'):
                        wiz.process()
                    elif hasattr(wiz, 'process_cancel_backorder'):
                        wiz.process_cancel_backorder()

    @api.model
    def create(self, vals):
        order = super().create(vals)
        if self.env.context.get('auto_invoice_on_import'):
            order.action_confirm()
            order._validate_outgoing_pickings()
            invoice = order._create_invoices()
            if invoice and order.invoice_date_import:
                invoice.invoice_date = order.invoice_date_import
        return order
