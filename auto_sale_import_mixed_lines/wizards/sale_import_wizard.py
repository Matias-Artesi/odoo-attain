from odoo import models, fields, api, _
from odoo.exceptions import UserError
import base64
import pandas as pd
from io import BytesIO

class SaleImportWizard(models.TransientModel):
    _name = 'sale.import.wizard'
    _description = 'Importador de Ventas'

    file = fields.Binary("Archivo Excel", required=True)
    file_name = fields.Char("Nombre del archivo")
    validate_invoice = fields.Boolean("Validar factura automáticamente")
    simulate = fields.Boolean("Simulación (no guarda)")
    abort_on_errors = fields.Boolean("Cancelar todo si hay errores", default=True)
    service_product_id = fields.Many2one('product.product', string="Producto servicio (líneas libres)",
                                         domain="[('type','=','service'),('sale_ok','=',True)]",
                                         help="Se usará para líneas sin default_code (líneas libres).")
    result_summary = fields.Text("Resumen", readonly=True)

    def _action_reopen_self(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'view_mode': 'form',
            'res_id': self.id,
            'target': 'new',
        }

    def _to_str(self, v):
        return "" if v is None else str(v)

    def _to_float(self, v, default=0.0):
        try:
            import math
            if v is None or (isinstance(v, float) and (v != v)):  # NaN check
                return default
            return float(v)
        except Exception:
            return default

    def _to_date(self, v):
        try:
            import pandas as pd
            if v is None or (isinstance(v, float) and (v != v)):
                return False
            return pd.to_datetime(v).date()
        except Exception:
            return False

    def action_import_sales(self):
        self.ensure_one()
        if not self.file:
            raise UserError(_("Debe seleccionar un archivo."))

        data = base64.b64decode(self.file)
        try:
            df = pd.read_excel(BytesIO(data))
        except Exception as e:
            raise UserError(_("No se pudo leer el Excel: %s") % e)

        errors = []
        summary_lines = []

        # Agrupar por 'name' (número de pedido)
        grouped = {}
        for idx, row in df.iterrows():
            name = row.get('name')
            if name is None or (isinstance(name, float) and (name != name)):
                errors.append(f"Fila {idx+2}: sin 'name' (número de pedido).")
                continue
            grouped.setdefault(name, []).append(row)

        summary_lines.append(f"Órdenes detectadas: {len(grouped)}")

        # PREVALIDACIÓN / PREPARACIÓN
        prepared_orders = []  # cada item: dict con header + lines
        for order_name, rows in grouped.items():
            first = rows[0]

            partner_id = int(self._to_float(first.get('partner_id'), 0))
            company_id = int(self._to_float(first.get('company_id'), 0))
            date_order = self._to_date(first.get('date_order'))
            invoice_date = self._to_date(first.get('invoice_date_import'))
            journal_code = self._to_str(first.get('journal_code')).strip()

            if not partner_id:
                errors.append(f"{order_name}: 'partner_id' ausente o inválido.")
                continue
            if not company_id:
                errors.append(f"{order_name}: 'company_id' ausente o inválido.")
                continue

            partner = self.env['res.partner'].browse(partner_id)
            company = self.env['res.company'].browse(company_id)
            if not partner.exists():
                errors.append(f"{order_name}: Cliente no encontrado (ID {partner_id}).")
                continue
            if not company.exists():
                errors.append(f"{order_name}: Compañía no encontrada (ID {company_id}).")
                continue

            # IVA 21% de VENTAS, en la compañía
            tax_21 = self.env['account.tax'].with_company(company).search([
                ('name', '=', '21%'),
                ('type_tax_use', '=', 'sale'),
            ], limit=1)

            # Pricelist
            pricelist = partner.property_product_pricelist.with_company(company) if partner.property_product_pricelist else False

            # Preparar líneas agrupadas
            prepared_lines = []
            line_count = 0
            for r in rows:
                line_count += 1
                default_code = self._to_str(r.get('default_code')).strip()
                desc = self._to_str(r.get('order_line/product_id/name')).strip()
                qty = self._to_float(r.get('order_line/product_uom_qty'), 1.0)
                price_unit_val = r.get('price_unit')
                price_unit = None if (price_unit_val is None or (isinstance(price_unit_val, float) and (price_unit_val != price_unit_val))) else float(price_unit_val)

                if default_code:
                    # Producto stockable por default_code
                    product = self.env['product.product'].with_company(company).search([('default_code', '=', default_code)], limit=1)
                    if not product:
                        errors.append(f"{order_name}: Producto no encontrado por código '{default_code}'.")
                        continue

                    # Determinar precio: Excel > pricelist > lst_price
                    if price_unit is None:
                        if pricelist:
                            try:
                                price_rule = pricelist.get_product_price_rule(product, qty, partner=partner)
                                if isinstance(price_rule, (list, tuple)) and len(price_rule) >= 1:
                                    computed_price = float(price_rule[0])
                                else:
                                    computed_price = product.lst_price
                            except Exception:
                                computed_price = product.lst_price
                            price_unit = computed_price
                        else:
                            price_unit = product.lst_price

                    prepared_lines.append({
                        'type': 'product',
                        'product': product,
                        'desc': desc or product.display_name,
                        'qty': qty,
                        'price_unit': price_unit,
                    })
                else:
                    # Línea personalizada (requiere service_product_id y price_unit)
                    if not self.service_product_id:
                        errors.append(f"{order_name}: Línea libre requiere 'Producto servicio (líneas libres)' seleccionado en el wizard.")
                        continue
                    if not desc:
                        errors.append(f"{order_name}: Línea libre sin descripción (order_line/product_id/name).")
                        continue
                    if price_unit is None:
                        errors.append(f"{order_name}: Línea libre '{desc}' sin 'price_unit'.")
                        continue

                    prepared_lines.append({
                        'type': 'custom',
                        'desc': desc,
                        'qty': qty,
                        'price_unit': price_unit,
                        'tax_21': tax_21,
                    })

            summary_lines.append(f"- {order_name}: {line_count} líneas (válidas: {len(prepared_lines)})")

            if not prepared_lines:
                errors.append(f"{order_name}: no se agregaron líneas válidas.")
                continue

            prepared_orders.append({
                'name': order_name,
                'partner': partner,
                'company': company,
                'date_order': date_order,
                'invoice_date': invoice_date,
                'journal_code': journal_code,
                'lines': prepared_lines,
            })

        # SIMULACIÓN
        if self.simulate:
            if errors:
                summary_lines.append("")
                summary_lines.append("Errores detectados:")
                summary_lines.extend(errors)
            self.result_summary = "
".join(summary_lines)
            return self._action_reopen_self()

        # Si se solicitó abortar ante errores, no crear nada
        if errors and self.abort_on_errors:
            summary_lines.append("")
            summary_lines.append("Se detectaron errores. No se creó nada (cancelar todo si hay errores = Sí).")
            summary_lines.append("")
            summary_lines.append("Errores detectados:")
            summary_lines.extend(errors)
            self.result_summary = "
".join(summary_lines)
            return self._action_reopen_self()

        # CREACIÓN (con automatismos en create() de sale.order)
        created_orders = []
        for po in prepared_orders:
            order_line_vals = []
            for pl in po['lines']:
                if pl['type'] == 'product':
                    order_line_vals.append((0, 0, {
                        'product_id': pl['product'].id,
                        'name': pl['desc'],
                        'product_uom_qty': pl['qty'],
                        'product_uom': pl['product'].uom_id.id,
                        'price_unit': pl['price_unit'],
                    }))
                else:
                    order_line_vals.append((0, 0, {
                        'product_id': self.service_product_id.id,
                        'name': pl['desc'],
                        'product_uom_qty': pl['qty'],
                        'product_uom': self.service_product_id.uom_id.id,
                        'price_unit': pl['price_unit'],
                        'tax_id': [(6, 0, pl['tax_21'].ids)] if pl['tax_21'] else False,
                    }))

            order_vals = {
                'name': po['name'],
                'partner_id': po['partner'].id,
                'company_id': po['company'].id,
                'date_order': po['date_order'],
                'invoice_date_import': po['invoice_date'],
                'order_line': order_line_vals,
            }
            order = self.env['sale.order'].with_context(auto_invoice_on_import=True).create(order_vals)

            # Ajustar diario y validar factura si corresponde
            if po['journal_code']:
                journal = self.env['account.journal'].with_company(po['company']).search([
                    ('code', '=', po['journal_code']),
                    ('type', '=', 'sale'),
                ], limit=1)
                if journal and order.invoice_ids:
                    order.invoice_ids[0].journal_id = journal.id
            if self.validate_invoice and order.invoice_ids:
                order.invoice_ids[0].action_post()

            created_orders.append(order)

        summary_lines.append("")
        summary_lines.append(f"Órdenes creadas: {len(created_orders)}")
        if errors and not self.abort_on_errors:
            summary_lines.append("")
            summary_lines.append("Errores (se creó lo posible):")
            summary_lines.extend(errors)

        self.result_summary = "
".join(summary_lines)
        return self._action_reopen_self()
