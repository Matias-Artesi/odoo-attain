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
    simulate = fields.Boolean("Simulación (no guarda)")
    cancel_all_on_errors = fields.Boolean("Cancelar todo si hay errores", default=True)
    service_product_id = fields.Many2one(
        'product.product',
        string="Producto servicio (líneas libres)",
        domain=[('type','=','service'), ('sale_ok','=',True)]
    )
    result_summary = fields.Text("Resumen", readonly=True)

    # ---------- Helpers ----------
    def _to_date(self, v):
        if v is None or v == '':
            return False
        try:
            return pd.to_datetime(v).date()
        except Exception:
            return False

    def _is_nan(self, v):
        try:
            import math
            return v != v or (isinstance(v, float) and math.isnan(v))
        except Exception:
            return False

    def _coalesce(self, row, keys):
        for k in keys:
            if k in row and row[k] is not None and row[k] != '' and not self._is_nan(row[k]):
                return row[k]
        return None

    def _get_company(self, val):
        Company = self.env['res.company']
        if val is None:
            return self.env.company
        # If numeric id
        try:
            cid = int(val)
            c = Company.browse(cid)
            if c.exists():
                return c
        except Exception:
            pass
        # By name exact, then ilike
        name = str(val).strip()
        c = Company.search([('name', '=', name)], limit=1)
        if not c:
            c = Company.search([('name', 'ilike', name)], limit=1)
        return c

    def _get_partner(self, val, company):
        Partner = self.env['res.partner']
        if val is None:
            return Partner.browse(False)
        # try numeric id
        try:
            pid = int(val)
            p = Partner.browse(pid)
            if p.exists():
                return p
        except Exception:
            pass
        name = str(val).strip()
        # exact name in company or global
        p = Partner.search([('name', '=', name), ('company_id', 'in', [company.id, False])], limit=1)
        if not p:
            # try ref
            p = Partner.search([('ref', '=', name), ('company_id', 'in', [company.id, False])], limit=1)
        if not p:
            # try ilike as last resort
            p = Partner.search([('name', 'ilike', name), ('company_id', 'in', [company.id, False])], limit=1)
        return p

    def _get_tax_iva_21_sale(self, company):
        Tax = self.env['account.tax']
        # Prefer amount match to avoid name clash with purchases
        tax = Tax.search([('type_tax_use','=','sale'),
                          ('amount','=',21.0),
                          ('company_id','in',[company.id, False])], limit=1)
        if not tax:
            tax = Tax.search([('type_tax_use','=','sale'),
                              ('name','in',['21%','IVA 21%']),
                              ('company_id','in',[company.id, False])], limit=1)
        return tax

    def _get_product_by_default_code(self, code, company):
        Product = self.env['product.product']
        return Product.search([('default_code','=', code), ('company_id','in',[company.id, False])], limit=1)

    def _normalize_journal_code(self, v):
        if v is None or v == '':
            return None
        s = str(v).strip()
        if s.isdigit():
            # Typical short code '15' -> '00015'
            return s.zfill(5)
        return s

    def _get_journal(self, code, company):
        if not code:
            return self.env['account.journal'].browse(False)
        Journal = self.env['account.journal']
        # Try exact code; if short numeric provided, also try zfilled
        code1 = str(code).strip()
        code2 = code1.zfill(5) if code1.isdigit() else code1
        j = Journal.search([('type','=','sale'),
                            ('company_id','=',company.id),
                            '|', ('code','=',code1), ('code','=',code2)], limit=1)
        if not j:
            # last resort: any company (multi-company setups)
            j = Journal.search([('type','=','sale'),
                                '|', ('code','=',code1), ('code','=',code2)], limit=1)
        return j

    def _float_or(self, v, default=0.0):
        try:
            return float(v)
        except Exception:
            return default

    # ---------- Main ----------
    def action_import_sales(self):
        self.ensure_one()
        if not self.file:
            raise UserError(_("Por favor, adjunte un archivo Excel."))

        data = base64.b64decode(self.file)
        try:
            df = pd.read_excel(BytesIO(data), sheet_name=0)
        except Exception as e:
            raise UserError(_("No se pudo leer el Excel: %s") % e)

        # Replace NaN with None for easier handling
        df = df.where(pd.notnull(df), None)

        # Group by 'name'
        rows = df.to_dict(orient='records')
        grouped = {}
        errors = []
        for r in rows:
            order_name = r.get('name')
            if not order_name:
                errors.append("Fila sin 'name' (número/identificador de orden).")
                continue
            grouped.setdefault(str(order_name), []).append(r)

        summary_lines = [f"Órdenes detectadas: {len(grouped)}"]

        if self.simulate:
            for oname, lines in grouped.items():
                summary_lines.append(f"- {oname}: {len(lines)} líneas")
            if errors:
                summary_lines.append("\nErrores detectados:")
                summary_lines.extend(errors)
            self.result_summary = "\n".join(summary_lines)
            return {
                "type": "ir.actions.act_window",
                "res_model": "sale.import.wizard",
                "view_mode": "form",
                "target": "new",
                "res_id": self.id,
            }

        def add_error(msg):
            errors.append(msg)

        # Transaction
        try:
            with self.env.cr.savepoint():
                for oname, lines in grouped.items():
                    head = lines[0]

                    # Extract header fields with flexible keys
                    company_val = self._coalesce(head, ['company_id', 'company', 'company/name'])
                    company = self._get_company(company_val) or self.env.company
                    partner_val = self._coalesce(head, ['partner_id/name','partner','partner_name','customer','partner_id'])
                    partner = self._get_partner(partner_val, company)

                    date_order = self._to_date(self._coalesce(head, ['date_order', 'order_date']))
                    inv_date = self._to_date(self._coalesce(head, ['invoice_date_import','invoice_date']))
                    journal_code = self._normalize_journal_code(self._coalesce(head, ['journal_code','journal']))

                    if not partner:
                        add_error(f"{oname}: partner_id inválido o no encontrado ({partner_val}).")
                        continue
                    if not company:
                        add_error(f"{oname}: company inválida o no encontrada ({company_val}).")
                        continue

                    order_lines = []
                    iva21 = self._get_tax_iva_21_sale(company)

                    for line in lines:
                        default_code = self._coalesce(line, ['order_line/product_id/default_code','default_code'])
                        desc = self._coalesce(line, ['order_line/product_id/name','description','name'])
                        qty = self._float_or(self._coalesce(line, ['order_line/product_uom_qty','quantity','qty']), 1.0)
                        price = self._coalesce(line, ['order_line/price_unit','price_unit','price'])

                        if default_code:
                            prod = self._get_product_by_default_code(str(default_code).strip(), company)
                            if not prod:
                                add_error(f"{oname}: Producto no encontrado por default_code '{default_code}'.")
                                continue
                            line_vals = {
                                'product_id': prod.id,
                                'product_uom_qty': qty,
                            }
                            if price is not None and price != '':
                                line_vals['price_unit'] = self._float_or(price)
                            order_lines.append((0,0,line_vals))
                        else:
                            # custom line requires service_product_id and price
                            if not self.service_product_id:
                                add_error(f"{oname}: Hay líneas sin default_code pero no se seleccionó 'Producto servicio (líneas libres)' en el wizard.")
                                continue
                            if not desc:
                                add_error(f"{oname}: Línea personalizada sin descripción.")
                                continue
                            if price is None or price == '':
                                add_error(f"{oname}: Línea personalizada '{desc}' sin price_unit.")
                                continue
                            line_vals = {
                                'product_id': self.service_product_id.id,
                                'name': str(desc),
                                'product_uom_qty': qty,
                                'price_unit': self._float_or(price),
                            }
                            if iva21:
                                line_vals['tax_id'] = [(6,0, iva21.ids)]
                            order_lines.append((0,0,line_vals))

                    if not order_lines:
                        add_error(f"{oname}: No se agregaron líneas válidas.")
                        continue

                    so_vals = {
                        'name': str(oname),
                        'partner_id': partner.id,
                        'company_id': company.id,
                        'date_order': date_order or fields.Date.context_today(self),
                        'invoice_date_import': inv_date,
                        'order_line': order_lines,
                    }

                    sale = self.env['sale.order'].with_context(auto_invoice_on_import=True).create(so_vals)

                    # Post-process invoice (journal, validate)
                    invoice = sale.invoice_ids[:1]
                    if invoice:
                        if journal_code:
                            journal = self._get_journal(journal_code, company)
                            if journal:
                                invoice.journal_id = journal.id
                        if self.validate_invoice:
                            invoice.action_post()

            if errors:
                summary_lines.append("Errores detectados:")
                summary_lines.extend(errors)
                if self.cancel_all_on_errors:
                    # Abort all by raising
                    raise UserError("\n".join(summary_lines))

            if not errors:
                summary_lines.append("Importación completada sin errores.")

        except UserError as ue:
            self.result_summary = str(ue)
            return {
                "type": "ir.actions.act_window",
                "res_model": "sale.import.wizard",
                "view_mode": "form",
                "target": "new",
                "res_id": self.id,
            }
        except Exception as e:
            # unexpected error -> propagate summary with error
            summary_lines.append(f"❌ Error crítico: {e}")
            self.result_summary = "\n".join(summary_lines)
            return {
                "type": "ir.actions.act_window",
                "res_model": "sale.import.wizard",
                "view_mode": "form",
                "target": "new",
                "res_id": self.id,
            }

        self.result_summary = "\n".join(summary_lines)
        return {
            "type": "ir.actions.act_window",
            "res_model": "sale.import.wizard",
            "view_mode": "form",
            "target": "new",
            "res_id": self.id,
        }