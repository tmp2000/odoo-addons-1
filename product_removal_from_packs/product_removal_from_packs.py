# -*- coding: utf8 -*-
#
#    Copyright (C) 2015 NDP Systèmes (<http://www.ndp-systemes.fr>).
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

from openerp import api, models
from openerp.tools.float_utils import float_compare


class StockQuantRemovalFromPacks(models.Model):
    _inherit = 'stock.quant'

    @api.multi
    def apply_rss(self, product, location, quantity, domain):
        packs = self.env['stock.quant.package'].search([('location_id', '=', location.id)]). \
            filtered(lambda p: sum([x.qty for x in p.quant_ids if x.product_id == product]) > 0)
        list_removals = []
        qty_reserved = 0
        if packs:
            qty_to_remove_for_each_pack = float(quantity) / len(packs)
            for pack in packs:
                qty_available_in_pack = sum([x.qty for x in pack.quant_ids if x.product_id == product])
                if qty_available_in_pack >= qty_to_remove_for_each_pack:
                   list_removals += self.apply_removal_strategy(location, product, qty_to_remove_for_each_pack,
                                               domain + [('package_id', '=', pack.id)], 'fifo')
                   qty_reserved += qty_to_remove_for_each_pack
                else:
                    for quant in pack.quant_ids:
                        if quant.product_id == product:
                            qty_reserved += quant.qty
                            list_removals += [(quant, quant.qty)]
        if float_compare(qty_reserved, quantity, precision_rounding=product.uom_id.rounding) < 0:
            list_removals += self.apply_removal_strategy(location, product, quantity - qty_reserved,
                                                         domain + [('package_id', '=', False)], 'fifo')
        return list_removals

    @api.model
    def apply_removal_strategy(self, location, product, quantity, domain, removal_strategy):
        if removal_strategy == 'rss':
            apply_rss = True
            pack_or_lot_or_reservation_domain = [x for x in domain if x[0] == 'package_id' or x[0] == 'lot_id' or
                                                 x[0] == 'reservation_id']
            domain += [('location_id', '=', location.id)]
            for cond in pack_or_lot_or_reservation_domain:
                if cond[2]:
                    apply_rss = False
                    break
            if apply_rss:
                return self.apply_rss(product, location, quantity, domain)
            else:
                return self.apply_removal_strategy(location, product, quantity, domain, 'fifo')
        return super(StockQuantRemovalFromPacks, self).apply_removal_strategy(location, product, quantity, domain,
                                                                              removal_strategy)
