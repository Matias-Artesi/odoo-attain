from odoo import models, fields, api

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    invoice_date_import = fields.Date(string="Fecha de Factura (importación)")

    def _validate_outgoing_pickings(self):
        """Valida entregas de salida marcando qty_done en move lines con la cantidad planificada del move.
        No depende de atributos frágiles en move lines y procesa cualquier wizard que devuelva button_validate().
        """
        for picking in self.picking_ids.filtered(lambda p: p.picking_type_code == 'outgoing' and p.state not in ('done', 'cancel')):
            # 1) Intentar reservar (no es obligatorio, pero ayuda si hay stock)
            picking.action_assign()

            # 2) Completar qty_done sin leer campos no estándar en move lines
            for move in picking.move_ids_without_package:
                # Si en tu flujo no usás lotes/series, OMITIMOS esos productos por seguridad
                if move.product_id.tracking != 'none':
                    continue

                planned_qty = move.product_uom_qty or 0.0

                if move.move_line_ids:
                    # Ponemos toda la cantidad planificada en la primera línea
                    first_ml = move.move_line_ids[:1]
                    first_ml.write({'qty_done': planned_qty})
                    # Opcional: aseguramos 0.0 en el resto
                    others = move.move_line_ids - first_ml
                    if others:
                        others.write({'qty_done': 0.0})
                else:
                    # Si no hay líneas, creamos una con toda la cantidad planificada
                    self.env['stock.move.line'].create({
                        'move_id': move.id,
                        'picking_id': picking.id,
                        'product_id': move.product_id.id,
                        'location_id': move.location_id.id,
                        'location_dest_id': move.location_dest_id.id,
                        'product_uom_id': move.product_uom.id,
                        'qty_done': planned_qty,
                    })

            # 3) Validar el picking
            res = picking.button_validate()

            # 4) Si Odoo devuelve un wizard (inmediata/backorder), procesarlo sin suposiciones
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
            # Confirmar venta -> genera pickings/moves
            order.action_confirm()
            # Validar entregas de salida
            order._validate_outgoing_pickings()
            # Crear factura en borrador
            invoice = order._create_invoices()
            # Aplicar fecha de factura si vino en el Excel
            if invoice and order.invoice_date_import:
                invoice.invoice_date = order.invoice_date_import
        return order
