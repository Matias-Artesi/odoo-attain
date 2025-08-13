from odoo import models, fields, api, _
from odoo.exceptions import UserError
import base64
from io import BytesIO
import pandas as pd

class SaleImportWizard(models.TransientModel):
    _name = 'sale.import.wizard'
    _description = 'Importador de Ventas'

    file = fields.Binary("Archivo Excel", required=True)
    file_name = fields.Char("Nombre del archivo")
    validate_invoice = fields.Boolean("Validar factura automáticamente")
    simulate = fields.Boolean("Simulación (no guarda)", default=True)
    abort_on_errors = fields.Boolean("Cancelar todo si hay errores", default=True)
    service_product_id = fields.Many2one(
        'product.product',
        string="Producto servicio (líneas libres)",
        domain="[('detailed_type', '=', 'service'), ('sale_ok', '=', True)]",
        help="Se usará para líneas sin default_code (líneas personalizadas)."
    )
    result_summary = fields.Text("Resumen", readonly=True)

    # ---------------------------
    # Helpers
    # ---------------------------
    def _to_date(self, value):
        if value is False or value is None:
            return False
        try:
            return pd.to_datetime(value).date()
        except Exception:
            return False

    def _to_float(self, value, default=0.0):
        try:
            if value is None or (isinstance(value, float) and pd.isna(value)):
                return default
            return float(value)
        except Exception:
            return default

    # ---------------------------
    # Core
    # ---------------------------
    def action_import_sales(self):
        self.ensure_one()
        if not self.file:
            raise UserError(_("Suba un archivo."))

        # Leer Excel
        try:
            data = base64.b64decode(self.file)
            df = pd.read_excel(BytesIO(data))
        except Exception as e:
            raise UserError(_("No se pudo leer el Excel: %s") % (e,))

        # Normalizar columnas esperadas
        expected_any = ['name', 'partner_id', 'company_id', 'date_order', 'order_line/product_id/name', 'order_line/product_uom_qty']
        missing = [c for c in expected_any if c not in df.columns]
        if missing:
            raise UserError(_("Faltan columnas obligatorias en el Excel: %s") % ', '.join(missing))

        # Forward-fill de cabeceras (como hace el importador estándar de Odoo)
        for col in ['name', 'partner_id', 'company_id', 'date_order', 'invoice_date_import', 'journal_code']:
            if col in df.columns:
                df[col] = df[col].ffill()

        # Construir grupos por 'name'
        errors = []
        summary_lines = []
        groups = {}

        for idx, row in df.iterrows():
            order_name = row.get('name')
            if not order_name or (isinstance(order_name, float) and pd.isna(order_name)):
                errors.append(f"Fila {idx+2}: sin 'name' (número de pedido).")
                continue
            groups.setdefault(order_name, []).append(row)

        summary_lines.append(f"Órdenes detectadas: {len(groups)}")

        # Simulación: contar líneas válidas por orden y detectar errores potenciales
        if self.simulate:
            for order_name, lines in groups.items():
                # Validaciones básicas
                first = lines[0]
                partner_id = int(first.get('partner_id')) if pd.notna(first.get('partner_id')) else None
                company_id = int(first.get('company_id')) if pd.notna(first.get('company_id')) else None
                if not partner_id:
                    errors.append(f"{order_name}: partner_id vacío.")
                if not company_id:
                    errors.append(f"{order_name}: company_id vacío.")

                valid_line_count = 0
                for r in lines:
                    default_code = (r.get('default_code') or '').strip() if isinstance(r.get('default_code'), str) else r.get('default_code')
                    qty = self._to_float(r.get('order_line/product_uom_qty'), 0.0)
                    if qty <= 0:
                        errors.append(f"{order_name}: cantidad inválida en una línea.")
                        continue
                    if default_code:
                        valid_line_count += 1
                    else:
                        desc = r.get('order_line/product_id/name')
                        price = r.get('price_unit')
                        if not (isinstance(desc, str) and desc.strip()):
                            errors.append(f"{order_name}: línea personalizada sin descripción.")
                            continue
                        if price is None or (isinstance(price, float) and pd.isna(price)):
                            errors.append(f"{order_name}: línea personalizada sin price_unit.")
                            continue
                        valid_line_count += 1
                summary_lines.append(f"- {order_name}: {len(lines)} líneas (válidas: {valid_line_count})")
            if errors:
                summary_lines.append("")
                summary_lines.append("Errores detectados:")
                summary_lines.extend(errors)
            self.result_summary = "\n".join(summary_lines)
            # Volver a abrir el wizard con el resumen
            return {
                "type": "ir.actions.act_window",
                "res_model": "sale.import.wizard",
                "view_mode": "form",
                "res_id": self.id,
                "target": "new",
            }

        # Ejecución real
        try:
            with self.env.cr.savepoint():
                for order_name, lines in groups.items():
                    first = lines[0]
                    try:
                        partner = self.env['res.partner'].browse(int(first.get('partner_id')))
                    except Exception:
                        partner = False
                    try:
                        company = self.env['res.company'].browse(int(first.get('company_id')))
                    except Exception:
                        company = False
                    if not partner or not partner.exists():
                        errors.append(f"{order_name}: Cliente no encontrado (ID {first.get('partner_id')}).")
                        continue
                    if not company or not company.exists():
                        errors.append(f"{order_name}: Compañía no encontrada (ID {first.get('company_id')}).")
                        continue

                    # Encontrar IVA 21% de ventas en la compañía
                    tax_21 = self.env['account.tax'].search([
                        ('name', 'in', ['21%', 'IVA 21%']),
                        ('type_tax_use', '=', 'sale'),
                        ('company_id', 'in', [company.id, False]),
                    ], limit=1)

                    order_lines_vals = []
                    for r in lines:
                        qty = self._to_float(r.get('order_line/product_uom_qty'), 0.0)
                        if qty <= 0:
                            errors.append(f"{order_name}: cantidad inválida (<=0).")
                            continue

                        default_code = r.get('default_code')
                        default_code = (default_code or '').strip() if isinstance(default_code, str) else default_code
                        desc = r.get('order_line/product_id/name')
                        price = r.get('price_unit')

                        if default_code:
                            product = self.env['product.product'].search([('default_code', '=', default_code)], limit=1)
                            if not product:
                                errors.append(f"{order_name}: Producto no encontrado por default_code '{default_code}'.")
                                continue
                            # calcular precio si no viene
                            price_unit = None
                            if price is not None and not (isinstance(price, float) and pd.isna(price)):
                                price_unit = float(price)
                            else:
                                # intentar lista de precios del partner
                                price_unit = product.lst_price
                                try:
                                    pricelist = partner.property_product_pricelist
                                    if pricelist:
                                        # get_product_price_rule(product, qty, partner)
                                        res = pricelist.get_product_price_rule(product, qty or 1.0, partner=partner)
                                        # Odoo devuelve (price, rule_id)
                                        if isinstance(res, (list, tuple)) and len(res) >= 1:
                                            price_unit = float(res[0])
                                except Exception:
                                    # fallback seguro
                                    price_unit = product.lst_price

                            line_vals = {
                                'product_id': product.id,
                                'name': desc or product.get_product_multiline_description_sale() or product.display_name,
                                'product_uom_qty': qty,
                                'product_uom': product.uom_id.id,
                                'price_unit': price_unit,
                            }
                            # No forzar impuestos aquí: que fluyan desde el producto/posición fiscal
                            order_lines_vals.append((0, 0, line_vals))
                        else:
                            # Línea personalizada: requiere producto servicio y precio
                            if not self.service_product_id:
                                errors.append(f"{order_name}: línea personalizada requiere seleccionar 'Producto servicio (líneas libres)' en el wizard.")
                                continue
                            if not (isinstance(desc, str) and desc.strip()):
                                errors.append(f"{order_name}: línea personalizada sin descripción.")
                                continue
                            if price is None or (isinstance(price, float) and pd.isna(price)):
                                errors.append(f"{order_name}: línea personalizada sin price_unit.")
                                continue

                            line_vals = {
                                'product_id': self.service_product_id.id,
                                'name': desc.strip(),
                                'product_uom_qty': qty,
                                'product_uom': self.service_product_id.uom_id.id,
                                'price_unit': float(price),
                                'tax_id': [(6, 0, tax_21.ids)] if tax_21 else False,
                            }
                            order_lines_vals.append((0, 0, line_vals))

                    if not order_lines_vals:
                        errors.append(f"{order_name}: no se agregaron líneas válidas.")
                        continue

                    order_vals = {
                        'name': order_name,
                        'partner_id': partner.id,
                        'company_id': company.id,
                        'date_order': self._to_date(first.get('date_order')) or fields.Date.context_today(self),
                        'invoice_date_import': self._to_date(first.get('invoice_date_import')),
                        'order_line': order_lines_vals,
                    }

                    sale_order = self.env['sale.order'].with_context(auto_invoice_on_import=True).create(order_vals)

                    # Post-proceso de la factura (diario + validación)
                    invoices = sale_order.invoice_ids.filtered(lambda m: m.move_type == 'out_invoice')
                    invoice = invoices[:1]
                    invoice = invoice and invoice[0] or False
                    if invoice:
                        jcode = first.get('journal_code')
                        if isinstance(jcode, str) and jcode.strip():
                            journal = self.env['account.journal'].search([
                                ('type', '=', 'sale'),
                                ('code', '=', jcode.strip()),
                                ('company_id', '=', company.id),
                            ], limit=1)
                            if journal:
                                invoice.journal_id = journal.id
                            else:
                                errors.append(f"{order_name}: diario de ventas con código '{jcode}' no encontrado para la compañía.")
                        if self.validate_invoice:
                            try:
                                invoice.action_post()
                            except Exception as e:
                                errors.append(f"{order_name}: no se pudo validar la factura ({e}).")

            # Si abort_on_errors y hubo errores: revertir todo
            if errors and self.abort_on_errors:
                raise UserError(_("Se detectaron errores y se canceló todo:\n- " + "\n- ".join(errors)))

            if errors:
                summary_lines.append("")
                summary_lines.append("Errores no bloqueantes:")
                summary_lines.extend(errors)
            else:
                summary_lines.append("")
                summary_lines.append("Importación completada sin errores.")

        except UserError as ue:
            # Mensaje limpio para rollback total
            self.result_summary = str(ue)
            return {
                "type": "ir.actions.act_window",
                "res_model": "sale.import.wizard",
                "view_mode": "form",
                "res_id": self.id,
                "target": "new",
            }
        except Exception as e:
            # Error inesperado: rollback
            self.result_summary = _("Error crítico, se deshizo todo: %s") % (e,)
            return {
                "type": "ir.actions.act_window",
                "res_model": "sale.import.wizard",
                "view_mode": "form",
                "res_id": self.id,
                "target": "new",
            }

        # Guardar resumen y reabrir wizard
        self.result_summary = "\n".join(summary_lines)
        return {
            "type": "ir.actions.act_window",
            "res_model": "sale.import.wizard",
            "view_mode": "form",
            "res_id": self.id,
            "target": "new",
        }
