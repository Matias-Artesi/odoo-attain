from odoo import models, fields, api, exceptions, _
import base64
from io import BytesIO

try:
    import pandas as pd
except Exception:
    pd = None

class SaleImportWizard(models.TransientModel):
    _name = 'sale.import.wizard'
    _description = 'Importador de Ventas'

    file = fields.Binary("Archivo Excel", required=True)
    file_name = fields.Char("Nombre del archivo")
    validate_invoice = fields.Boolean("Validar factura automáticamente")
    simulate = fields.Boolean("Simulación (no guarda)")
    cancel_all_on_errors = fields.Boolean("Cancelar todo si hay errores", default=True)
    service_product_id = fields.Many2one('product.product', string="Producto servicio (líneas libres)",
                                         domain="[('detailed_type','=','service'),('sale_ok','=',True)]")
    result_summary = fields.Text("Resumen", readonly=True)

    def _is_nan(self, v):
        if v is None:
            return True
        try:
            import math
            return isinstance(v, float) and math.isnan(v)
        except Exception:
            return False

    def _clean(self, v):
        return None if self._is_nan(v) else v

    def _get_company(self, val):
        val = self._clean(val)
        Company = self.env['res.company']
        if not val:
            return self.env.company
        try:
            cid = int(val)
            comp = Company.browse(cid)
            return comp if comp.exists() else False
        except Exception:
            pass
        comp = Company.search([('name', '=', str(val))], limit=1)
        if comp:
            return comp
        comp = Company.search([('partner_id.name', '=', str(val))], limit=1)
        return comp or False

    def _get_partner(self, val):
        val = self._clean(val)
        Partner = self.env['res.partner']
        if not val:
            return False
        try:
            pid = int(val)
            p = Partner.browse(pid)
            return p if p.exists() else False
        except Exception:
            pass
        p = Partner.search([('name', '=', str(val))], limit=1)
        if not p:
            p = Partner.search([('ref', '=', str(val))], limit=1)
        return p or False

    def _to_date(self, val):
        if not val or self._is_nan(val):
            return False
        if pd:
            try:
                return pd.to_datetime(val).date()
            except Exception:
                return False
        try:
            from datetime import datetime
            if isinstance(val, str):
                return datetime.strptime(val, "%Y-%m-%d").date()
        except Exception:
            return False
        return False

    def _get_tax_iva_21_sale(self, company):
        Tax = self.env['account.tax'].with_company(company)
        tax = Tax.search([
            ('name', 'in', ['21%', 'IVA 21%']),
            ('type_tax_use', '=', 'sale'),
            ('company_id', 'in', [company.id, False]),
        ], limit=1)
        return tax

    def _price_for_product(self, product, qty, partner):
        pricelist = partner.property_product_pricelist if partner else False
        if pricelist:
            rule = pricelist._get_product_rule(product, qty, partner=partner)
            if rule and 'price' in rule:
                return rule['price']
        return product.lst_price

    def _reopen_self(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _("Importar Ventas"),
            'res_model': 'sale.import.wizard',
            'view_mode': 'form',
            'res_id': self.id,
            'target': 'new',
        }

    def action_import_sales(self):
        self.ensure_one()
        if not self.file:
            raise exceptions.UserError(_("Subí un archivo primero."))
        if pd is None:
            raise exceptions.UserError(_("Este wizard requiere pandas. Agregá pandas y openpyxl al entorno o convertí a CSV."))
        try:
            data = base64.b64decode(self.file)
            df = pd.read_excel(BytesIO(data))
        except Exception as e:
            raise exceptions.UserError(_("No se pudo leer el Excel: %s") % e)

        if 'name' not in df.columns:
            raise exceptions.UserError(_("La columna 'name' (identificador de pedido) es obligatoria."))

        for col in ['partner_id','company_id','date_order','invoice_date_import','journal_code']:
            if col in df.columns:
                df[col] = df[col].ffill()

        grouped = {}
        errors = []
        summary_lines = []
        for idx, row in df.iterrows():
            order_name = row.get('name')
            if not order_name or (isinstance(order_name, float) and pd.isna(order_name)):
                errors.append(f"Fila {idx+2}: sin 'name'.")
                continue
            grouped.setdefault(order_name, []).append(row)

        summary_lines.append(f"Órdenes detectadas: {len(grouped)}")

        if self.simulate:
            for order, lines in grouped.items():
                summary_lines.append(f"- {order}: {len(lines)} líneas (simulación)")
            if errors:
                summary_lines.append("Errores detectados:")
                summary_lines.extend(errors)
            self.result_summary = "\n".join(summary_lines)
            return self._reopen_self()

        try:
            with self.env.cr.savepoint():
                for order_name, lines in grouped.items():
                    first = lines[0]

                    partner = self._get_partner(first.get('partner_id'))
                    if not partner:
                        errors.append(f"{order_name}: partner_id inválido o no encontrado ({first.get('partner_id')}).")
                        continue

                    company = self._get_company(first.get('company_id'))
                    if not company:
                        errors.append(f"{order_name}: company_id inválido o no encontrado ({first.get('company_id')}).")
                        continue

                    iva21 = self._get_tax_iva_21_sale(company)

                    order_lines = []
                    for ln in lines:
                        default_code = self._clean(ln.get('default_code'))
                        descr = self._clean(ln.get('order_line/product_id/name'))
                        qty = ln.get('order_line/product_uom_qty') or 1.0
                        price_unit = self._clean(ln.get('price_unit'))

                        try:
                            qty = float(qty)
                        except Exception:
                            qty = 1.0
                        if qty <= 0:
                            errors.append(f"{order_name}: cantidad inválida en línea '{descr}' (qty={qty}).")
                            continue

                        if default_code:
                            product = self.env['product.product'].search([('default_code','=',str(default_code))], limit=1)
                            if not product:
                                errors.append(f"{order_name}: producto no encontrado por default_code '{default_code}'.")
                                continue
                            line_vals = {
                                'product_id': product.id,
                                'product_uom': product.uom_id.id,
                                'product_uom_qty': qty,
                            }
                            if price_unit is None:
                                line_vals['price_unit'] = self._price_for_product(product, qty, partner)
                            else:
                                try:
                                    line_vals['price_unit'] = float(price_unit)
                                except Exception:
                                    errors.append(f"{order_name}: price_unit inválido en línea de producto '{default_code}'.")
                                    continue
                        else:
                            if not self.service_product_id:
                                errors.append(f"{order_name}: hay líneas sin default_code y no seleccionaste 'Producto servicio (líneas libres)' en el wizard.")
                                continue
                            if not descr:
                                errors.append(f"{order_name}: línea personalizada sin descripción.")
                                continue
                            if price_unit is None:
                                errors.append(f"{order_name}: línea personalizada '{descr}' sin price_unit.")
                                continue
                            try:
                                pu = float(price_unit)
                            except Exception:
                                errors.append(f"{order_name}: price_unit inválido en línea personalizada '{descr}'.")
                                continue
                            line_vals = {
                                'product_id': self.service_product_id.id,
                                'name': str(descr),
                                'product_uom': self.service_product_id.uom_id.id,
                                'product_uom_qty': qty,
                                'price_unit': pu,
                            }
                            if iva21:
                                line_vals['tax_id'] = [(6,0, iva21.ids)]
                        order_lines.append((0,0,line_vals))

                    if not order_lines:
                        errors.append(f"{order_name}: no se agregaron líneas válidas.")
                        continue

                    date_order = self._to_date(first.get('date_order'))
                    inv_date = self._to_date(first.get('invoice_date_import'))

                    order_vals = {
                        'name': str(order_name),
                        'partner_id': partner.id,
                        'company_id': company.id,
                        'date_order': date_order,
                        'invoice_date_import': inv_date,
                        'order_line': order_lines,
                    }

                    sale = self.env['sale.order'].with_context(auto_invoice_on_import=True).create(order_vals)

                    invoice = sale.invoice_ids[:1]
                    if invoice:
                        journal_code = self._clean(first.get('journal_code'))
                        if journal_code:
                            journal = self.env['account.journal'].with_company(company).search([('code','=',str(journal_code)), ('type','=','sale')], limit=1)
                            if journal:
                                invoice.journal_id = journal.id
                        if self.validate_invoice:
                            invoice.action_post()

                if errors and self.cancel_all_on_errors:
                    raise exceptions.UserError(_("Se detectaron errores, se canceló toda la importación:\n- ") + "\n- ".join(errors))
        except exceptions.UserError as e:
            summary_lines.append(str(e))
            self.result_summary = "\n".join(summary_lines)
            return self._reopen_self()

        if errors:
            summary_lines.append("Errores detectados:")
            summary_lines.extend(errors)
        else:
            summary_lines.append("Importación completada sin errores.")
        self.result_summary = "\n".join(summary_lines)
        return self._reopen_self()
