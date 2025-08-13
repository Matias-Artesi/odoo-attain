from odoo import models, fields, api
from odoo.exceptions import UserError
import base64
import pandas as pd
from io import BytesIO

class SaleImportWizard(models.TransientModel):
    _name = 'sale.import.wizard'
    _description = 'Importador de Ventas'

    file = fields.Binary("Archivo Excel", required=True)
    file_name = fields.Char("Nombre del archivo")
    service_product_id = fields.Many2one(
        'product.product',
        string="Producto servicio (líneas libres)",
        help="Se usa para líneas sin default_code",
        domain="[('type','=','service'),('sale_ok','=',True)]",
    )
    validate_invoice = fields.Boolean("Validar factura automáticamente")
    simulate = fields.Boolean("Simulación (no guarda)")
    cancel_all_on_errors = fields.Boolean("Cancelar todo si hay errores", default=True)
    result_summary = fields.Text("Resumen", readonly=True)

    def _to_date(self, v):
        if v is None or (isinstance(v, float) and pd.isna(v)) or (isinstance(v, str) and not v.strip()):
            return False
        try:
            return pd.to_datetime(v).date()
        except Exception:
            return False

    def _is_nan(self, v):
        try:
            return pd.isna(v)
        except Exception:
            return False

    def _action_show_self(self):
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'view_mode': 'form',
            'res_id': self.id,
            'target': 'new',
        }

    def action_import_sales(self):
        self.ensure_one()
        if not self.file:
            raise UserError("Subí un archivo Excel.")

        # Leer Excel
        try:
            data = base64.b64decode(self.file)
            df = pd.read_excel(BytesIO(data))
        except Exception as e:
            self.result_summary = f"❌ No pude leer el archivo: {e}"
            return self._action_show_self()

        required_any = ['name', 'order_line/product_id/name', 'order_line/product_uom_qty']
        missing_cols = [c for c in required_any if c not in df.columns]
        if missing_cols:
            self.result_summary = "❌ Faltan columnas obligatorias: " + ", ".join(missing_cols)
            return self._action_show_self()

        # Agrupar filas por nombre de orden
        errors = []
        prepared = {}  # order_name -> {'header': {...}, 'lines': [vals]}

        # Detectar si habrá líneas libres pero no se configuró producto servicio
        has_free_lines = any((not str(row.get('default_code') or '').strip()) for _, row in df.iterrows())
        if has_free_lines and not self.service_product_id:
            errors.append("Hay líneas sin default_code pero no elegiste el 'Producto servicio (líneas libres)' en el wizard.")

        # Pre-validación y preparación
        for _, row in df.iterrows():
            order_name = str(row.get('name') or '').strip()
            if not order_name:
                errors.append("Fila sin 'name' (número de orden).")
                continue

            group = prepared.setdefault(order_name, {'header': None, 'lines': []})
            # Si no hay header aún, usar esta fila como cabecera
            if not group['header']:
                partner_id = row.get('partner_id')
                company_id = row.get('company_id')
                if self._is_nan(partner_id) or self._is_nan(company_id):
                    errors.append(f"{order_name}: Falta 'partner_id' o 'company_id' en la primera fila del grupo.")
                    # seguimos, pero esta orden no se podrá crear
                header = {
                    'partner_id': int(partner_id) if partner_id and not self._is_nan(partner_id) else None,
                    'company_id': int(company_id) if company_id and not self._is_nan(company_id) else None,
                    'date_order': self._to_date(row.get('date_order')),
                    'invoice_date_import': self._to_date(row.get('invoice_date_import')),
                    'journal_code': (str(row.get('journal_code')).strip() if not self._is_nan(row.get('journal_code')) else None),
                    'name': order_name,
                }
                group['header'] = header

            # Preparar línea
            qty = row.get('order_line/product_uom_qty')
            if self._is_nan(qty):
                errors.append(f"{order_name}: Línea sin cantidad.")
                continue
            try:
                qty = float(qty)
            except Exception:
                errors.append(f"{order_name}: Cantidad inválida '{qty}'.")
                continue

            default_code = str(row.get('default_code') or '').strip()
            desc = str(row.get('order_line/product_id/name') or '').strip()
            price_excel = row.get('price_unit')

            if default_code:
                # Producto stockable por default_code
                product = self.env['product.product'].search([
                    ('default_code', '=', default_code),
                ], limit=1)
                if not product:
                    errors.append(f"{order_name}: Producto no encontrado por código '{default_code}'.")
                    continue

                line_vals = {
                    'product_id': product.id,
                    'product_uom_qty': qty,
                }

                if price_excel and not self._is_nan(price_excel):
                    try:
                        line_vals['price_unit'] = float(price_excel)
                    except Exception:
                        errors.append(f"{order_name}: price_unit inválido '{price_excel}' (código {default_code}).")
                        continue
                else:
                    # Fallback simple al precio de lista del producto
                    line_vals['price_unit'] = product.lst_price

            else:
                # Línea personalizada (servicio)
                if not self.service_product_id:
                    errors.append(f"{order_name}: Línea personalizada sin producto servicio configurado en el wizard.")
                    continue
                if not desc:
                    errors.append(f"{order_name}: Línea personalizada sin descripción.")
                    continue
                if price_excel is None or self._is_nan(price_excel):
                    errors.append(f"{order_name}: Línea personalizada '{desc}' sin price_unit.")
                    continue
                try:
                    price_unit = float(price_excel)
                except Exception:
                    errors.append(f"{order_name}: price_unit inválido '{price_excel}' (línea personalizada).")
                    continue

                # Buscar IVA 21% ventas (por compañía o global)
                company_id = group['header']['company_id']
                tax = self.env['account.tax'].search([
                    ('name', 'in', ['21%', 'IVA 21%']),
                    ('type_tax_use', '=', 'sale'),
                    ('company_id', 'in', [company_id, False]),
                ], limit=1)

                line_vals = {
                    'product_id': self.service_product_id.id,
                    'name': desc,
                    'product_uom_qty': qty,
                    'price_unit': price_unit,
                    'tax_id': [(6, 0, [tax.id])] if tax else False,
                }

            group['lines'].append(line_vals)

        # Simulación: mostrar resumen y salir
        if self.simulate:
            lines = [f"Órdenes detectadas: {len(prepared)}"]
            for name, data in prepared.items():
                lines.append(f"- {name}: {len(data['lines'])} líneas válidas")
            if errors:
                lines.append("")
                lines.append("Errores detectados:")
                lines += [f"• {e}" for e in errors]
            self.result_summary = "\n".join(lines)
            return self._action_show_self()

        # Si hay errores y se pidió cancelar todo, no crear nada
        if errors and self.cancel_all_on_errors:
            lines = ["❌ Importación cancelada por errores.", ""]
            lines += ["Errores detectados:"] + [f"• {e}" for e in errors]
            self.result_summary = "\n".join(lines)
            return self._action_show_self()

        # Crear órdenes (solo para las que estén bien preparadas)
        created = 0
        for name, data in prepared.items():
            header = data['header']
            partner = self.env['res.partner'].browse(header['partner_id']) if header['partner_id'] else False
            company = self.env['res.company'].browse(header['company_id']) if header['company_id'] else False

            if not partner or not company:
                errors.append(f"{name}: No se pudo crear (partner o company faltante).")
                continue
            if not data['lines']:
                errors.append(f"{name}: No tiene líneas válidas.")
                continue

            order_vals = {
                'name': name,
                'partner_id': partner.id,
                'company_id': company.id,
                'date_order': header['date_order'],
                'invoice_date_import': header['invoice_date_import'],
                'order_line': [(0, 0, l) for l in data['lines']],
            }
            sale_order = self.env['sale.order'].with_context(auto_invoice_on_import=True).create(order_vals)

            # Post-procesar factura
            invoice = sale_order.invoice_ids[:1]
            if invoice:
                jcode = header.get('journal_code')
                if jcode:
                    journal = self.env['account.journal'].search([
                        ('code', '=', jcode),
                        ('type', '=', 'sale'),
                        ('company_id', '=', company.id),
                    ], limit=1)
                    if journal:
                        invoice.journal_id = journal.id
                if self.validate_invoice:
                    invoice.action_post()

            created += 1

        # Resumen final
        lines = [f"Órdenes detectadas: {len(prepared)}", f"Órdenes creadas: {created}"]
        if errors:
            lines.append("")
            lines.append("Errores (no bloqueantes):")
            lines += [f"• {e}" for e in errors]
        self.result_summary = "\n".join(lines)
        return self._action_show_self()
