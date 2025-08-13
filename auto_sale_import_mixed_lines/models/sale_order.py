from odoo import models, fields, api

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    invoice_date_import = fields.Date(string="Fecha de Factura (importación)")

    def _validate_outgoing_pickings(self):
        """Valida pickings de salida sin depender de modelos wizard explícitos.
        Usa qty_done en move lines vía write() y procesa cualquier wizard devuelto por button_validate().
        """
        for picking in self.picking_ids.filtered(lambda p: p.picking_type_code == 'outgoing' and p.state not in ('done', 'cancel')):
            # 1) Intentar reservar
            picking.action_assign()

            # 2) Completar qty_done en las líneas (stock.move.line) sin leer atributos inexistentes
            for move in picking.move_ids_without_package:
                # Si el producto requiere lote/serie, mejor no automatizar (sin datos de lote). Lo reportás aparte si querés.
                if move.product_id.tracking != 'none':
                    continue

                if move.move_line_ids:
                    for ml in move.move_line_ids:
                        qty_line = ml.reserved_uom_qty or getattr(ml, 'product_uom_qty', 0.0) or move.product_uom_qty
                        # setear sin leer ml.qty_done -> evita AttributeError en tu entorno
                        ml.write({'qty_done': qty_line})
                else:
                    # Crear una move line completa con la cantidad pedida
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

            # 4) Si aparece un wizard (inmediate/backorder), procesarlo de forma genérica
            if isinstance(res, dict):
                res_model = res.get('res_model')
                res_id = res.get('res_id')
                if res_model and res_id:
                    wiz = self.env[res_model].browse(res_id)
                    # ambos wizards en v17 implementan 'process'; algunos tienen además 'process_cancel_backorder'
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
