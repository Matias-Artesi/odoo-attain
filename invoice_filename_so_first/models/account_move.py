import re
from odoo import models

class AccountMove(models.Model):
    _inherit = 'account.move'

    # --- helpers ---
    def _sanitize_filename_part(self, s):
        """Return a filesystem-friendly chunk for filenames.
        - Replaces slashes with hyphens
        - Removes characters illegal in common filesystems
        - Collapses whitespace to underscores
        - Strips leading/trailing separators
        """
        s = (s or "").strip()
        s = s.replace("/", "-")
        s = re.sub(r'[\\:*?"<>|]', '', s)   # remove illegal in common FS
        s = re.sub(r'\s+', '_', s)          # collapse whitespace
        return s.strip(' ._-')

    def _compose_invoice_filename(self):
        self.ensure_one()

        # Invoice identifier
        inv_name = self.name or ""
        if self.state == 'draft' or not inv_name or inv_name in ('/', ''):
            inv_name = f"DRAFT-{self.id}"

        # Try to find the originating SO name (prefer the first related SO)
        sale_names = self.invoice_line_ids.mapped('sale_line_ids.order_id.name')
        origin = sale_names[0] if sale_names else (self.invoice_origin or "")

        # If there are multiple origins in a string, keep the first token
        if isinstance(origin, str) and ',' in origin:
            origin = origin.split(',')[0].strip()

        # sanitize
        origin_part = self._sanitize_filename_part(origin)
        inv_part = self._sanitize_filename_part(inv_name) or f"INV-{self.id}"

        # Apply prefix only to customer invoices/refunds
        if self.move_type in ('out_invoice', 'out_refund') and origin_part:
            base = f"{origin_part}-{inv_part}"
        else:
            base = inv_part

        # keep it reasonable length
        return base[:180] or f"INVOICE-{self.id}"

# --- hook used by ir.actions.report ---
def _get_report_base_filename(self):
    self.ensure_one()
    if self.move_type in ('out_invoice', 'out_refund'):
        return self._compose_invoice_filename()
    return super()._get_report_base_filename()

