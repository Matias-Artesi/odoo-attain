from odoo import models, fields, api

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    invoice_date_import = fields.Date(string="Fecha de Factura (importación)")

    def _validate_outgoing_pickings(self):
        """Valida entregas de salida sin depender de atributos inexistentes.
        Carga qty_done en move lines con la cantidad planificada y procesa cualquier wizard devuelto.
        """
        for picking in self.picking_ids.filtered(lambda p: p.picking_type_code == 'outgoing' and p.state not in ('done', 'cancel')):
            # 1) Intentar reservar (si hay stock disponible/ubicaciones correctas)
            picking.action_assign()

            # 2) Completar qty_done en las move lines
            for move in picking.move_ids_without_package:
                # Si tu flujo no usa lotes/series, omitimos automatizar esos productos
                if move.product_id.tracking != 'none':
                    continue

                if move.move_line_ids:
                    # Seteamos qty_done = cantidad planificada de cada línea
                    for ml in move.move_line_ids:
                        # product_uom_qty existe en move line en v17
                        qty_line = ml.product_uom_qty or move.product_uom_qty or 0.0
                        ml.write({'qty_done': qty_line})
                else:
                    # Sin move lines: creamos una con todo el movimiento
                    self.env['stock.move.line'].create({
                        'move_id': move.id,
                        'product_id': move.product_id.id,
                        'picking_id': picking.id,
                        'location_id': move.location_id.id,
                        'location_dest_id': move.location_dest_id.id,
                        'product_uom_id': move.product_uom.id,
                        'qty_done': move.product_uom_qty,
                    })

            # 3) Validar el picking
            res = picking.button_validate()

            # 4) Si Odoo devuelve un wizard (inmediata / backorder), procesarlo genéricamente
            if isinstance(res, dict):
                model = res.get('res_model')
                res_id = res.get('res_id')
                if model and res_id:
                    wiz = self.env[model].browse(res_id)
                    # En v17, ambos wizards implementan 'process'; algunos también 'process_cancel_backorder'
                    if hasattr(wiz, 'process'):
                        wiz.process()
                    elif hasattr(wiz, 'process_cancel_backorder'):
                        wiz.process_cancel_backorder()

    @api.model
    def create(self, vals):
        order = super().create(vals)
        if self.env.context.get('auto_invoice_on_import'):
            # Confirmar venta => Odoo genera automáticamente el/los pickings
            order.action_confirm()
            # Validar entregas de forma robusta
            order._validate_outgoing_pickings()
            # Crear factura en borrador
            invoice = order._create_invoices()
            # Aplicar fecha de factura si vino en el Excel
            if invoice and order.invoice_date_import:
                invoice.invoice_date = order.invoice_date_import
        return order
