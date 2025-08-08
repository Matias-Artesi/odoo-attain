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
        s = (s or "").replace("/", "-")
        s = re.sub(r'[\\:*?"<>|]', '', s)   # remove illegal
        s = re.sub(r'\s+', '_', s)           # spaces -> underscores
        return s.strip('_-') or None

    def _compose_invoice_filename(self):
        """Build `<SO>-<INV>` with robust fallbacks and uniqueness in drafts."""
        self.ensure_one()
        # parts
        origin = (self.invoice_origin or "").strip()
        # if there are multiple origins like "SO001, SO002", keep them all
        if origin:
            origin = ','.join([o.strip() for o in origin.split(',') if o.strip()])
        inv_name = self.name or ''
        # Use a readable fallback in draft (name is usually '/')
        if self.state == 'draft' or inv_name in ('', '/'):
            inv_name = f"DRAFT-{self.id}"

        # sanitize
        origin_part = self._sanitize_filename_part(origin)
        inv_part = self._sanitize_filename_part(inv_name) or f"INV-{self.id}"

        if self.move_type == 'out_invoice' and origin_part:
            base = f"{origin_part}-{inv_part}"
        else:
            base = inv_part
        # keep it reasonable length
        return (base[:180]) or f"INVOICE-{self.id}"

    # --- hook used by ir.actions.report ---
    def _get_report_base_filename(self):
        return self._compose_invoice_filename()