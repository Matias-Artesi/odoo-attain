from odoo import models, fields, api
from odoo.exceptions import UserError
import base64, math
import pandas as pd
from io import BytesIO
from datetime import datetime

class SaleImportWizard(models.TransientModel):
    _name = 'sale.import.wizard'
    _description = 'Importador de Ventas'

    file = fields.Binary("Archivo Excel", required=True)
    file_name = fields.Char("Nombre del archivo")
    service_product_id = fields.Many2one('product.product', string="Producto servicio (líneas libres)",
        domain=[('type', '=', 'service'), ('sale_ok', '=', True)])
    validate_invoice = fields.Boolean("Validar factura automáticamente")
    simulate = fields.Boolean("Simulación (no guarda)")
    cancel_all_on_errors = fields.Boolean("Cancelar todo si hay errores", default=True)
    result_summary = fields.Text("Resumen", readonly=True)

    def _is_na(self, v):
        if v is None:
            return True
        if isinstance(v, float) and math.isnan(v):
            return True
        if isinstance(v, str) and v.strip().lower() in ("", "nan", "none", "null"):
            return True
        return False

    def _norm_name(self, v):
        if self._is_na(v):
            return None
        if isinstance(v, float):
            if v.is_integer():
                return str(int(v))
            return str(v).strip()
        return str(v).strip()

    def _norm_str(self, v):
        if self._is_na(v):
            return None
        # Cuando pandas lee columnas que parecen numéricas, usualmente las
        # convierte a `float`.  Para códigos como default_code queremos
        # preservar el valor original sin el sufijo ".0" que agrega al
        # representarlo como string.  Por eso, si el valor es numérico y es un
        # entero, lo transformamos explícitamente a `int` antes de convertirlo
        # a texto.
        if isinstance(v, float):
            if v.is_integer():
                return str(int(v))
            return str(v).strip()
        if isinstance(v, int):
            return str(v)
        return str(v).strip()

    def _norm_journal_code(self, v):
        """Devuelve siempre un código de 5 dígitos ('00015'), tolerando entradas tipo 15, 15.0, '15,0', ' 015 ', etc."""
        if self._is_na(v):
            return None

        # Casos numéricos puros (lo típico cuando Excel convierte a número)
        if isinstance(v, int):
            return f"{v:05d}"
        if isinstance(v, float):
            if math.isnan(v):
                return None
            return f"{int(v):05d}"

        s = str(v).strip()
        if not s:
            return None

        # Aceptar formatos comunes: '15.0', '15,0', '1e1'
        s2 = s.replace(',', '.')
        try:
            f = float(s2)
            if float(f).is_integer():
                return f"{int(f):05d}"
        except Exception:
            pass

        # Si son dígitos puros, pad a 5
        if s.isdigit():
            return s.zfill(5)

        # Extraer sólo dígitos (por si viene 'abc16' u otros ruidos)
        digits = ''.join(ch for ch in s if ch.isdigit())
        if digits:
            # Si son <= 5 dígitos, pad a 5; si son más, devolvemos tal cual (no forzamos truncado).
            return digits.zfill(5) if len(digits) <= 5 else digits

        # Último recurso, devolver tal cual
        return s

    def _find_sale_journal(self, code, company):
        """Busca el diario de ventas por code (con y sin ceros) y, si está l10n_ar, por l10n_ar_afip_pos_number."""
        Journal = self.env['account.journal'].with_company(company.id)
        code_norm = self._norm_journal_code(code)

        # Intento 1: código normalizado (00015)
        if code_norm:
            j = Journal.search([('type', '=', 'sale'), ('code', '=', code_norm)], limit=1)
            if j:
                return j

        # Intento 2: sin ceros a la izquierda (15)
        if code_norm and code_norm.isdigit():
            alt = code_norm.lstrip('0') or '0'
            j = Journal.search([('type', '=', 'sale'), ('code', '=', alt)], limit=1)
            if j:
                return j

        # Intento 3 (opcional l10n_ar): por número de PDV AFIP
        digits = None
        if code is not None:
            s = str(code)
            digits = ''.join(ch for ch in s if ch.isdigit())
        if digits and 'l10n_ar_afip_pos_number' in Journal._fields:
            try:
                j = Journal.search([('type', '=', 'sale'), ('l10n_ar_afip_pos_number', '=', int(digits))], limit=1)
                if j:
                    return j
            except Exception:
                pass

        return Journal.browse(False)

    def _to_date(self, value):
        if self._is_na(value):
            return False
        try:
            return pd.to_datetime(value).date()
        except Exception:
            return False

    def _get_company(self, name_or_id):
        if self._is_na(name_or_id):
            return self.env.company
        try:
            cid = int(name_or_id)
            c = self.env['res.company'].browse(cid)
            if c and c.exists():
                return c
        except Exception:
            pass
        name = self._norm_str(name_or_id)
        c = self.env['res.company'].search([('name', '=', name)], limit=1)
        if not c:
            c = self.env['res.company'].search([('name', 'ilike', name)], limit=1)
        return c or self.env.company

    def _get_partner(self, partner_name_or_id, company):
        Partner = self.env['res.partner'].with_company(company.id)
        if self._is_na(partner_name_or_id):
            return Partner.browse(False)
        try:
            pid = int(partner_name_or_id)
            p = Partner.browse(pid)
            if p and p.exists():
                return p
        except Exception:
            pass
        name = self._norm_str(partner_name_or_id)
        p = Partner.search([('name', '=', name)], limit=1)
        if not p:
            p = Partner.search([('ref', '=', name)], limit=1)
        if not p:
            p = Partner.search([('name', 'ilike', name)], limit=1)
        return p

    def _get_tax_iva_21_sale(self, company):
        domain = [('type_tax_use', '=', 'sale'), ('amount', '=', 21.0), ('company_id', 'in', [company.id, False])]
        Tax = self.env['account.tax'].with_company(company.id)
        t = Tax.search(domain, limit=1)
        if not t:
            t = Tax.search([('type_tax_use', '=', 'sale'), ('name', 'in', ['21%', 'IVA 21%'])], limit=1)
        return t

    def _price_for_product(self, product, qty, partner):
        try:
            pricelist = partner.property_product_pricelist
            if pricelist:
                rule_id, price, _ = pricelist.get_product_price_rule(product, qty, partner=partner)
                if price is not None:
                    return price
        except Exception:
            pass
        return product.lst_price

    def _get_invoice_report_action(self):
        candidates = ['account.report_invoice','account.account_invoices','l10n_ar.report_invoice_document']
        for xmlid in candidates:
            try:
                return self.env.ref(xmlid)
            except ValueError:
                continue
        raise UserError("No se encontró un reporte de facturas válido (account.report_invoice / account.account_invoices).")

    def action_import_sales(self):
        self.ensure_one()
        if not self.file:
            return

        data = base64.b64decode(self.file)
        df = pd.read_excel(BytesIO(data))

        if 'name' not in df.columns:
            raise UserError("La planilla debe incluir una columna 'name' para identificar las órdenes.")
        df['name'] = df['name'].apply(self._norm_name)
        df['name'] = df['name'].ffill()

        header_cols = ['partner_id', 'partner_id/name', 'company_id', 'date_order', 'invoice_date_import', 'journal_code']
        for col in header_cols:
            if col in df.columns:
                df[col] = df[col].ffill()

        if 'partner_id/name' in df.columns:
            df['__partner_name__'] = df['partner_id/name'].apply(self._norm_str)
        elif 'partner_id' in df.columns:
            df['__partner_name__'] = df['partner_id'].apply(self._norm_str)
        else:
            df['__partner_name__'] = None

        if 'company_id' in df.columns:
            df['__company_name__'] = df['company_id'].apply(self._norm_str)
        else:
            df['__company_name__'] = None

        if 'journal_code' in df.columns:
            df['__journal_code__'] = df['journal_code'].apply(self._norm_journal_code)
        else:
            df['__journal_code__'] = None

        if 'order_line/product_uom_qty' in df.columns:
            df['order_line/product_uom_qty'] = df['order_line/product_uom_qty'].fillna(1.0)
        if 'order_line/price_unit' in df.columns:
            df['order_line/price_unit'] = df['order_line/price_unit'].fillna(0.0)

        grouped_orders = {}
        for rec in df.to_dict('records'):
            order_name = rec.get('name')
            if not order_name:
                continue
            grouped_orders.setdefault(order_name, []).append(rec)

        errors = []
        summary = [f"Órdenes detectadas: {len(grouped_orders)}"]
        for order, lines in grouped_orders.items():
            summary.append(f"- {order}: {len(lines)} líneas")

        if self.simulate:
            for order_name, lines in grouped_orders.items():
                first = lines[0]
                company = self._get_company(first.get('__company_name__'))
                partner = self._get_partner(first.get('__partner_name__'), company)
                if not partner:
                    errors.append(f"{order_name}: Cliente no encontrado por nombre/ref: {first.get('__partner_name__')}")
                has_free = any(self._norm_str(l.get('default_code') or l.get('order_line/product_id/default_code')) is None for l in lines)
                if has_free and not self.service_product_id:
                    errors.append(f"{order_name}: Hay líneas sin default_code y no se indicó 'Producto servicio (líneas libres)' en el wizard.")
            if errors:
                summary.append("\nErrores detectados:")
                summary.extend(errors)
            self.result_summary = "\n".join(summary)
            return {
                "type": "ir.actions.act_window",
                "res_model": "sale.import.wizard",
                "view_mode": "form",
                "res_id": self.id,
                "target": "new",
            }

        posted_invoices = self.env['account.move']
        try:
            for order_name, lines in grouped_orders.items():
                order_errors = []
                invoices_to_post = self.env['account.move']
                try:
                    with self.env.cr.savepoint():
                        first = lines[0]
                        company = self._get_company(first.get('__company_name__'))
                        partner = self._get_partner(first.get('__partner_name__'), company)
                        if not partner:
                            order_errors.append(f"Cliente no encontrado por nombre/ref: {first.get('__partner_name__')}")

                        journal_code = first.get('__journal_code__')
                        order_lines = []
                        iva_21 = self._get_tax_iva_21_sale(company)

                        for row in lines:
                            qty = row.get('order_line/product_uom_qty') or 1.0
                            try:
                                qty = float(qty)
                            except Exception:
                                qty = 1.0

                            price_unit = row.get('order_line/price_unit')
                            try:
                                price_unit = float(price_unit) if price_unit is not None else None
                            except Exception:
                                price_unit = None

                            default_code = self._norm_str(row.get('default_code') or row.get('order_line/product_id/default_code'))
                            desc = self._norm_str(row.get('order_line/product_id/name'))

                            if default_code:
                                Product = self.env['product.product'].with_company(company.id)
                                product = Product.search([
                                    ('default_code', '=', default_code),
                                    '|', ('company_id', '=', company.id), ('company_id', '=', False),
                                ], limit=1)

                                if not product:
                                    order_errors.append(f"Producto no encontrado - código: {default_code}")
                                    continue
                                line_vals = {'product_id': product.id, 'product_uom_qty': qty}
                                if price_unit is not None:
                                    line_vals['price_unit'] = price_unit
                                else:
                                    line_vals['price_unit'] = self._price_for_product(product, qty, partner)
                            else:
                                if not self.service_product_id:
                                    order_errors.append("Línea sin default_code requiere 'Producto servicio (líneas libres)'.")
                                    continue
                                if not desc:
                                    order_errors.append("Línea personalizada sin descripción.")
                                    continue
                                if price_unit is None:
                                    order_errors.append("Línea personalizada sin price_unit.")
                                    continue
                                line_vals = {
                                    'product_id': self.service_product_id.id,
                                    'name': desc,
                                    'product_uom_qty': qty,
                                    'price_unit': price_unit,
                                    'tax_id': [(6, 0, [iva_21.id])] if iva_21 else [],
                                }
                            order_lines.append((0, 0, line_vals))

                        if not order_lines and not order_errors:
                            order_errors.append("No se agregaron líneas válidas.")

                        if order_errors:
                            raise UserError("\n- ".join([f"{order_name}: {msg}" for msg in order_errors]))

                        order_vals = {
                            'name': str(order_name),
                            'partner_id': partner.id,
                            'company_id': company.id,
                            'date_order': self._to_date(first.get('date_order')),
                            'invoice_date_import': self._to_date(first.get('invoice_date_import')),
                            'order_line': order_lines,
                        }
                        sale_order = self.env['sale.order'].with_context(auto_invoice_on_import=True).create(order_vals)

                        for invoice in sale_order.invoice_ids:
                            if journal_code:
                                j = self._find_sale_journal(journal_code, company)
                                if j:
                                    invoice.journal_id = j.id
                                else:
                                    raise UserError(
                                        f"{order_name}: No se encontró diario con código '{journal_code}' (normalizado: '{self._norm_journal_code(journal_code)}')."
                                    )
                            invoices_to_post |= invoice

                except UserError as e:
                    errors.append(str(e))
                    if self.cancel_all_on_errors:
                        raise
                except Exception as e:
                    errors.append(f"{order_name}: {str(e)}")
                    if self.cancel_all_on_errors:
                        raise
                else:
                    if self.validate_invoice:
                        for invoice in invoices_to_post:
                            invoice.action_post()
                            posted_invoices |= invoice

            if errors and self.cancel_all_on_errors:
                raise UserError("Se detectaron errores y se canceló toda la importación:\n- " + "\n- ".join(errors))

        except UserError as ue:
            summary.append(str(ue))
            self.result_summary = "\n".join(summary)
            return {
                "type": "ir.actions.act_window",
                "res_model": "sale.import.wizard",
                "view_mode": "form",
                "res_id": self.id,
                "target": "new",
            }
        except Exception as e:
            summary.append(f"❌ Error crítico, se deshizo todo: {str(e)}")
            self.result_summary = "\n".join(summary)
            return {
                "type": "ir.actions.act_window",
                "res_model": "sale.import.wizard",
                "view_mode": "form",
                "res_id": self.id,
                "target": "new",
            }

        if self.validate_invoice and posted_invoices:
            report = self._get_invoice_report_action()
            return report.report_action(posted_invoices)

        if errors:
            summary.append("Errores detectados:")
            summary.extend(errors)
        else:
            summary.append("Importación completada sin errores.")

        self.result_summary = "\n".join(summary)
        return {
            "type": "ir.actions.act_window",
            "res_model": "sale.import.wizard",
            "view_mode": "form",
            "res_id": self.id,
            "target": "new",
        }
