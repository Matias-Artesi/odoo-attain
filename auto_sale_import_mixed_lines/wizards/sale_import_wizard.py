from odoo import models, fields, api, _
from odoo.exceptions import UserError
import base64
from io import BytesIO

try:
    import pandas as pd
except Exception:
    pd = None

def _to_val(v):
    try:
        import math
        if v is None:
            return None
        if isinstance(v, float) and math.isnan(v):
            return None
        return v
    except Exception:
        return v

class SaleImportWizard(models.TransientModel):
    _name = 'sale.import.wizard'
    _description = 'Importador de Ventas'

    file = fields.Binary("Archivo Excel", required=True)
    file_name = fields.Char("Nombre del archivo")
    validate_invoice = fields.Boolean("Validar factura automáticamente")
    simulate = fields.Boolean("Simulación (no guarda)")
    cancel_all_on_errors = fields.Boolean("Cancelar todo si hay errores", default=True)
    service_product_id = fields.Many2one('product.product', string="Producto servicio (líneas libres)",
                                         domain="[('type','=','service'),('sale_ok','=',True)]")
    result_summary = fields.Text("Resumen", readonly=True)

    def _get_price_for_product(self, partner, product, qty):
        if not partner or not partner.property_product_pricelist:
            return product.lst_price
        pricelist = partner.property_product_pricelist
        try:
            res = pricelist.get_product_price_rule(product, qty, partner=partner)
            if isinstance(res, (list, tuple)) and len(res) >= 1:
                return float(res[0])
            return float(res)
        except Exception:
            return product.lst_price

    def action_import_sales(self):
        if not self.file:
            return

        if pd is None:
            raise UserError(_("Este wizard requiere la librería 'pandas' para leer Excel."))

        data = base64.b64decode(self.file)
        df = pd.read_excel(BytesIO(data))
        errors = []
        summary_lines = []
        grouped = {}

        for idx, row in df.iterrows():
            name = _to_val(row.get('name'))
            if not name:
                errors.append(f"Fila {idx+2}: sin 'name' (número de orden).")
                continue
            grouped.setdefault(name, []).append(row)

        summary_lines.append(f"Órdenes detectadas: {len(grouped)}")
        for order_name, rows in grouped.items():
            summary_lines.append(f"- {order_name}: {len(rows)} líneas")

        has_free_lines = any(_to_val(r.get('default_code')) in (None, '', 0) for rows in grouped.values() for r in rows)
        if has_free_lines and not self.service_product_id:
            errors.append("Se detectaron líneas personalizadas (sin default_code). Seleccione un 'Producto servicio (líneas libres)'.")

        if self.simulate:
            if errors:
                summary_lines.append("\nErrores detectados:")
                summary_lines.extend(errors)
            self.result_summary = "\n".join(summary_lines)
            return

        def _get_tax_21(company):
            tax = self.env['account.tax'].search([('name', '=', '21%'),
                                                  ('type_tax_use', '=', 'sale'),
                                                  ('company_id', '=', company.id)], limit=1)
            if not tax:
                tax = self.env['account.tax'].search([('name', '=', '21%'),
                                                      ('type_tax_use', '=', 'sale')], limit=1)
            return tax

        created_orders = []
        try:
            with self.env.cr.savepoint():
                for order_name, rows in grouped.items():
                    first = rows[0]
                    partner_id = _to_val(first.get('partner_id'))
                    company_id = _to_val(first.get('company_id'))
                    date_order = _to_val(first.get('date_order'))
                    invoice_date_import = _to_val(first.get('invoice_date_import'))
                    journal_code = _to_val(first.get('journal_code'))

                    if not partner_id:
                        errors.append(f"{order_name}: partner_id ausente.")
                        if self.cancel_all_on_errors:
                            raise UserError("\n".join(errors))
                        else:
                            continue
                    partner = self.env['res.partner'].browse(int(partner_id))
                    if not partner.exists():
                        errors.append(f"{order_name}: partner_id {partner_id} no encontrado.")
                        if self.cancel_all_on_errors:
                            raise UserError("\n".join(errors))
                        else:
                            continue

                    company = self.env['res.company'].browse(int(company_id)) if company_id else self.env.company
                    if company_id and not company.exists():
                        errors.append(f"{order_name}: company_id {company_id} no encontrado; se usará la compañía actual.")
                        company = self.env.company

                    tax_21 = _get_tax_21(company)

                    order_lines_vals = []
                    for ridx, r in enumerate(rows, start=1):
                        default_code = _to_val(r.get('default_code'))
                        desc = _to_val(r.get('order_line/product_id/name'))
                        qty = _to_val(r.get('order_line/product_uom_qty'))
                        price = _to_val(r.get('price_unit'))

                        if not qty:
                            qty = 1.0
                        try:
                            qty = float(qty)
                        except Exception:
                            errors.append(f"{order_name}: línea {ridx} cantidad inválida '{qty}'.")
                            if self.cancel_all_on_errors:
                                raise UserError("\n".join(errors))
                            else:
                                continue

                        if default_code:
                            product = self.env['product.product'].search([('default_code', '=', default_code)], limit=1)
                            if not product:
                                errors.append(f"{order_name}: línea {ridx} producto no encontrado (default_code='{default_code}').")
                                if self.cancel_all_on_errors:
                                    raise UserError("\n".join(errors))
                                else:
                                    continue
                            unit_price = price if price is not None else self._get_price_for_product(partner, product, qty)
                            line_vals = {
                                'product_id': product.id,
                                'product_uom_qty': qty,
                                'price_unit': unit_price,
                            }
                        else:
                            if not desc or price is None:
                                errors.append(f"{order_name}: línea {ridx} personalizada requiere descripción y price_unit.")
                                if self.cancel_all_on_errors:
                                    raise UserError("\n".join(errors))
                                else:
                                    continue
                            line_vals = {
                                'product_id': self.service_product_id.id,
                                'name': desc,
                                'product_uom_qty': qty,
                                'price_unit': float(price),
                            }
                            if tax_21:
                                line_vals['tax_id'] = [(6, 0, [tax_21.id])]
                        order_lines_vals.append((0, 0, line_vals))

                    if not order_lines_vals:
                        errors.append(f"{order_name}: sin líneas válidas.")
                        if self.cancel_all_on_errors:
                            raise UserError("\n".join(errors))
                        else:
                            continue

                    order_vals = {
                        'name': order_name,
                        'partner_id': partner.id,
                        'company_id': company.id,
                        'date_order': date_order,
                        'invoice_date_import': invoice_date_import,
                        'order_line': order_lines_vals,
                    }
                    order = self.env['sale.order'].with_context(auto_invoice_on_import=True).create(order_vals)

                    invoice = order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice')[:1]
                    if invoice:
                        if journal_code:
                            journal = self.env['account.journal'].search([('code', '=', journal_code),
                                                                          ('type', '=', 'sale'),
                                                                          ('company_id', '=', company.id)], limit=1)
                            if not journal:
                                journal = self.env['account.journal'].search([('code', '=', journal_code),
                                                                              ('type', '=', 'sale')], limit=1)
                            if journal:
                                invoice.journal_id = journal.id
                        if self.validate_invoice:
                            invoice.action_post()

                    created_orders.append(order_name)

                if errors and self.cancel_all_on_errors:
                    raise UserError("\n".join(errors))

        except UserError as ue:
            summary_lines.append("❌ Importación cancelada por errores:")
            summary_lines.append(str(ue))
            self.result_summary = "\n".join(summary_lines)
            return
        except Exception as e:
            summary_lines.append("❌ Error crítico: se deshizo todo.")
            summary_lines.append(str(e))
            self.result_summary = "\n".join(summary_lines)
            return

        if errors:
            summary_lines.append("\nSe crearon algunas órdenes, pero con errores:")
            summary_lines.extend(errors)
        else:
            summary_lines.append("\nImportación completada sin errores.")
        summary_lines.append(f"Órdenes creadas: {len(created_orders)}")
        self.result_summary = "\n".join(summary_lines)