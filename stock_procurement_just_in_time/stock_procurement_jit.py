# -*- coding: utf8 -*-
#
# Copyright (C) 2014 NDP Systèmes (<http://www.ndp-systemes.fr>).
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

from dateutil.relativedelta import relativedelta
import logging
import openerp.addons.decimal_precision as dp
from openerp.addons.connector.session import ConnectorSession
from openerp.addons.connector.queue.job import job
from openerp.tools import float_compare, float_round, flatten
from openerp.tools.sql import drop_view_if_exists
from openerp import fields, models, api, exceptions, _

ORDERPOINT_CHUNK = 1

_logger = logging.getLogger(__name__)


@job
def process_orderpoints(session, model_name, ids):
    """Processes the given orderpoints."""
    _logger.info("<<Started chunk of %s orderpoints to process" % ORDERPOINT_CHUNK)
    for op in session.env[model_name].browse(ids):
        op.process()


class StockMove(models.Model):
    _inherit = 'stock.move'

    procurement_id = fields.Many2one('procurement.order', index=True)


class ProcurementOrderQuantity(models.Model):
    _inherit = 'procurement.order'

    move_dest_id = fields.Many2one('stock.move', index=True)
    product_id = fields.Many2one('product.product', index=True)
    state = fields.Selection(index=True)
    qty = fields.Float(string="Quantity", digits_compute=dp.get_precision('Product Unit of Measure'),
                       help='Quantity in the default UoM of the product', compute="_compute_qty", store=True)

    @api.multi
    @api.depends('product_qty', 'product_uom')
    def _compute_qty(self):
        uom_obj = self.env['product.uom']
        for m in self:
            qty = uom_obj._compute_qty_obj(m.product_uom, m.product_qty, m.product_id.uom_id)
            m.qty = qty

    @api.multi
    def reschedule_for_need(self, need):
        """Reschedule procurements to a given need.
        Will set the date of the procurements one second before the date of the need.
        :param need: dict
        """
        for proc in self:
            new_date = fields.Datetime.from_string(need['date']) + relativedelta(seconds=-1)
            proc.date_planned = fields.Datetime.to_string(new_date)
            _logger.debug("Rescheduled proc: %s, new date: %s" % (proc, proc.date_planned))
        self.with_context(reschedule_planned_date=True).action_reschedule()

    @api.model
    def _procure_orderpoint_confirm(self, use_new_cursor=False, company_id=False):
        """
        Create procurement based on Orderpoint

        :param bool use_new_cursor: if set, use a dedicated cursor and auto-commit after processing each procurement.
            This is appropriate for batch jobs only.
        """
        orderpoint_env = self.env['stock.warehouse.orderpoint']
        dom = company_id and [('company_id', '=', company_id)] or []
        if self.env.context.get('compute_product_ids') and not self.env.context.get('compute_all_products'):
            dom += [('product_id', 'in', self.env.context.get('compute_product_ids'))]
        orderpoint_ids = orderpoint_env.search(dom)
        op_product_ids = orderpoint_ids.read(['id', 'product_id'], load=False)

        result = dict()
        for row in op_product_ids:
            if row['product_id'] not in result:
                result[row['product_id']] = list()
            result[row['product_id']].append(row['id'])
        product_ids = result.values()

        while product_ids:
            products = product_ids[:ORDERPOINT_CHUNK]
            product_ids = product_ids[ORDERPOINT_CHUNK:]
            orderpoints = flatten(products)
            if self.env.context.get('without_job'):
                for op in self.env['stock.warehouse.orderpoint'].browse(orderpoints):
                    op.process()
            else:
                process_orderpoints.delay(ConnectorSession.from_env(self.env), 'stock.warehouse.orderpoint',
                                          orderpoints, description="Computing orderpoints %s" % orderpoints)
        return {}

    @api.model
    def propagate_cancel(self, procurement):
        """
        Improves the original propagate_cancel, in order to cancel it even if one of its moves is done.
        """

        ignore_move_ids = procurement.rule_id.action == 'move' and procurement.move_ids and \
                          procurement.move_ids.filtered(lambda move: move.state == 'done').ids or []
        return super(ProcurementOrderQuantity,
                     self.with_context(ignore_move_ids=ignore_move_ids)).propagate_cancel(procurement)


class StockMoveJustInTime(models.Model):
    _inherit = 'stock.move'

    @api.multi
    def action_cancel(self):
        return super(StockMoveJustInTime, self.filtered(lambda move: move.id not in
                                                                     (self.env.context.get('ignore_move_ids') or []))). \
            action_cancel()


class StockWarehouseOrderPointJit(models.Model):
    _inherit = 'stock.warehouse.orderpoint'

    @api.multi
    @api.returns('procurement.order')
    def get_next_proc(self, need):
        """Returns the next procurement.order after this line which date is not the line's date."""
        self.ensure_one()
        next_line = self.compute_stock_levels_requirements(product_id=self.product_id.id,
                                                           location=self.location_id,
                                                           list_move_types=('existing', 'in', 'out', 'planned',),
                                                           limit=False, parameter_to_sort='date', to_reverse=False)
        next_line = [x for x in next_line if x.get('date') and x['date'] > need['date'] and x['proc_id']]
        if next_line:
            return self.env['procurement.order'].search([('id', '=', next_line[0]['proc_id'])])
        return self.env['procurement.order']

    @api.multi
    def get_next_need(self):
        """Returns a dict of stock level requirements where the stock level is below minimum qty for the product and
        the location of the orderpoint."""
        self.ensure_one()
        need = self.compute_stock_levels_requirements(product_id=self.product_id.id, location=self.location_id,
                                                      list_move_types=('out',), limit=False, parameter_to_sort='date',
                                                      to_reverse=False)
        need = [x for x in need if float_compare(x['qty'], self.product_min_qty,
                                                 precision_rounding=self.product_uom.rounding) < 0]
        if need:
            need = need[0]
            if need.get('id') or need.get('proc_id') or need.get('product_id') or need.get('location_id') or \
                    need.get('move_type') or need.get('qty') or need.get('date') or need.get('move_qty'):
                return need
        return False

    @api.multi
    def redistribute_procurements(self, date_start, date_end, days=1):
        """Redistribute procurements related to these orderpoints between date_start and date_end.
        Procurements will be considered as over-supplying if the quantity in stock calculated 'days' after the
        procurement is above the orderpoint calculated maximum quantity. This allows not to consider movements of
        large quantities over a small period of time (that can lead to ponctual over stock) as being over supply.

        This function works by taking procurements one by one from the right. For each it checks whether the quantity
        in stock days after this procurement is above the max value. If it is, the procurement is rescheduled
        temporarily to date_end. This way, we check at which date between the procurement's original date and its
        current date the stock level falls below the minimum quantity and finally place the procurement at this date.

        :param date_start: the starting date as datetime. If False, start at the earliest available date.
        :param date_end: the ending date as datetime
        :param days: defines the number of days after a procurement at which to consider stock quantity.
        """
        for op in self:
            date_domain = [('date_planned', '<', fields.Datetime.to_string(date_end))]
            if date_start:
                date_domain += [('date_planned', '>=', fields.Datetime.to_string(date_start))]
            procs = self.env['procurement.order'].search([('product_id', '=', op.product_id.id),
                                                          ('location_id', '=', op.location_id.id),
                                                          ('state', 'in', ['confirmed', 'running', 'exception'])] +
                                                         date_domain,
                                                         order="date_planned DESC")
            for proc in procs:
                stock_date = min(
                    fields.Datetime.from_string(proc.date_planned) + relativedelta(days=days),
                    date_end)
                stock_level = self.env['stock.warehouse.orderpoint'].compute_stock_levels_requirements(
                    product_id=proc.product_id.id, location=proc.location_id,
                    list_move_types=('in', 'out', 'existing', 'planned',),
                    parameter_to_sort='date', to_reverse=True, limit=False)
                stock_level = [x for x in stock_level if x.get('date') and x['date'] <
                               fields.Datetime.to_string(stock_date)]
                if stock_level and stock_level[0]['qty'] > op.get_max_qty(stock_date):
                    # We have too much of products: so we reschedule the procurement at end date
                    proc.date_planned = fields.Datetime.to_string(date_end + relativedelta(seconds=-1))
                    proc.with_context(reschedule_planned_date=True,
                                      do_not_propagate_rescheduling=True,
                                      do_not_propagate=True,
                                      mail_notrack=True).action_reschedule()
                    # Then we reschedule back to the next need if any
                    need = op.get_next_need()
                    if need and fields.Datetime.from_string(need['date']) < date_end:
                        # Our rescheduling ended in creating a need before our procurement, so we move it to this date
                        proc.with_context(reschedule_planned_date=True,
                                          do_not_propagate_rescheduling=True,
                                          do_not_propagate=True,
                                          mail_notrack=True).reschedule_for_need(need)
                    _logger.debug("Rescheduled procurement %s, new date: %s" % (proc, proc.date_planned))
            procs.with_context(reschedule_planned_date=True).action_reschedule()

    @api.multi
    def create_from_need(self, need):
        """Creates a procurement to fulfill the given need with the data calculated from the given order point.
        Will set the date of the procurement one second before the date of the need.

        :param need: the 'stock levels requirements' dictionary to fulfill
        :param orderpoint: the 'stock.orderpoint' record set with the needed date
        """
        proc_obj = self.env['procurement.order']
        for orderpoint in self:
            qty = max(orderpoint.product_min_qty,
                      orderpoint.get_max_qty(fields.Datetime.from_string(need['date']))) - need['qty']
            reste = orderpoint.qty_multiple > 0 and qty % orderpoint.qty_multiple or 0.0
            if float_compare(reste, 0.0, precision_rounding=orderpoint.product_uom.rounding) > 0:
                qty += orderpoint.qty_multiple - reste
            qty = float_round(qty, precision_rounding=orderpoint.product_uom.rounding)

            proc_vals = proc_obj._prepare_orderpoint_procurement(orderpoint, qty)
            proc_date = fields.Datetime.from_string(need['date']) + relativedelta(seconds=-1)
            proc_vals.update({
                'date_planned': fields.Datetime.to_string(proc_date)
            })
            proc = proc_obj.create(proc_vals)
            if not self.env.context.get("procurement_no_run"):
                proc.run()
            _logger.debug("Created proc: %s, (%s, %s). Product: %s, Location: %s" %
                          (proc, proc.date_planned, proc.product_qty, orderpoint.product_id, orderpoint.location_id))

    @api.multi
    def get_last_scheduled_date(self):
        """Returns the last scheduled date for this order point."""
        self.ensure_one()
        last_schedule = self.env['stock.warehouse.orderpoint'].compute_stock_levels_requirements(
            product_id=self.product_id.id,
            location=self.location_id,
            list_move_types=['in', 'out', 'existing'], limit=1,
            parameter_to_sort='date', to_reverse=True)
        res = last_schedule and last_schedule[0].get('date') and \
              fields.Datetime.from_string(last_schedule[0].get('date')) or False
        return res

    @api.multi
    def remove_unecessary_procurements(self, timestamp):
        """Remove the unecessary procurements that are placed just before timestamp, and recreate one if necessary to
        match exactly this order point product_min_qty.

        :param timestamp: datetime object
        """
        for orderpoint in self:
            last_outgoing = self.env['stock.warehouse.orderpoint'].compute_stock_levels_requirements(
                product_id=orderpoint.product_id.id, location=orderpoint.location_id,
                list_move_types=('out',), limit=1, parameter_to_sort='date', to_reverse=True
            )
            last_outgoing = [x for x in last_outgoing if x['date'] <= fields.Datetime.to_string(timestamp)]
            # We get all procurements placed before timestamp, but after the last outgoing line sorted by inv quantity
            procs = self.env['procurement.order'].search([('product_id', '=', orderpoint.product_id.id),
                                                          ('location_id', '=', orderpoint.location_id.id),
                                                          ('state', 'not in', ['done', 'cancel']),
                                                          ('date_planned', '<=', fields.Datetime.to_string(timestamp))],
                                                         order='qty DESC')
            if last_outgoing:
                procs = procs.filtered(lambda y: y.date_planned > last_outgoing[0]['date'])
            _logger.debug("Removing not needed procurements: %s", procs.ids)
            procs.cancel()
            procs.unlink()

    @api.multi
    def process(self):
        """Process this orderpoint."""
        for op in self:
            _logger.debug("Computing orderpoint %s (%s, %s, %s)" % (op.id, op.name, op.product_id.name,
                                                                    op.location_id.display_name))
            need = op.get_next_need()
            date_cursor = False
            while need:
                op.redistribute_procurements(date_cursor, fields.Datetime.from_string(need['date']), days=1)
                # We move the date_cursor to the need date
                date_cursor = fields.Datetime.from_string(need['date'])
                # We check if there is already a procurement in the future
                next_proc = op.get_next_proc(need)
                if next_proc:
                    # If there is a future procurement, we reschedule it (required date) to fit our need
                    next_proc.reschedule_for_need(need)
                else:
                    # Else, we create a new procurement
                    op.create_from_need(need)
                need = op.get_next_need()
            # Now we want to make sure that at the end of the scheduled outgoing moves, the stock level is
            # the minimum quantity of the orderpoint.
            last_scheduled_date = op.get_last_scheduled_date()
            if last_scheduled_date:
                date_end = last_scheduled_date + relativedelta(minutes=+1)
                op.redistribute_procurements(date_cursor, date_end)
                op.remove_unecessary_procurements(date_end)

    @api.model
    def compute_stock_levels_requirements(self, product_id, location, list_move_types, limit=1,
                                          parameter_to_sort='date', to_reverse=False):
        """
        Computes stock level report
        :param product_id: int
        :param location: recordset
        :param list_move_types: tuple or list of strings (move types)
        :param limit: maximum number of lines in the result
        :param parameter_to_sort: str
        :param to_reverse: bool
        :return: list of need dictionaries
        """

        # Computing the top parent location
        min_date = False
        result = []
        intermediate_result = []
        query_moves_in = """
            SELECT
                sm.id,
                sm.product_qty,
                min(COALESCE(po.date_planned, sm.date)) AS date,
                min(po.id)
            FROM
                stock_move sm
                LEFT JOIN stock_location sl ON sm.location_dest_id = sl.id
                LEFT JOIN procurement_order po ON sm.procurement_id = po.id
            WHERE
                sm.product_id = %s
                AND sm.state NOT IN ('cancel', 'done', 'draft')
                AND sl.parent_left >= %s
                AND sl.parent_left < %s
            GROUP BY sm.id, sm.product_qty
            ORDER BY date
        """
        self.env.cr.execute(query_moves_in, (product_id, location.parent_left, location.parent_right))
        moves_in_tuples = self.env.cr.fetchall()

        query_moves_out = """
            SELECT
                sm.id,
                sm.product_qty,
                min(sm.date) AS date
            FROM
                stock_move sm
                LEFT JOIN stock_location sl ON sm.location_id = sl.id
            WHERE
                sm.product_id = %s
                AND sm.state NOT IN ('cancel', 'done', 'draft')
                AND sl.parent_left >= %s
                AND sl.parent_left < %s
            GROUP BY sm.id, sm.product_qty
            ORDER BY date
        """
        self.env.cr.execute(query_moves_out, (product_id, location.parent_left, location.parent_right))
        moves_out_tuples = self.env.cr.fetchall()

        stock_quant_restricted = self.env['stock.quant'].search([('product_id', '=', product_id),
                                                                 ('location_id', 'child_of', location.id)])
        query_procs = """
            SELECT
                po.id,
                min(po.date_planned),
                min(po.qty)
            FROM
                procurement_order po
                LEFT JOIN stock_location sl ON po.location_id = sl.id
                LEFT JOIN stock_move sm ON po.id = sm.procurement_id
            WHERE
                po.product_id = %s
                AND sl.parent_left >= %s
                AND sl.parent_left < %s
                AND po.state NOT IN ('done', 'cancel')
                AND (sm.state = 'draft' OR sm.id IS NULL)
            GROUP BY po.id
            ORDER BY po.date_planned
        """
        self.env.cr.execute(query_procs, (product_id, location.parent_left, location.parent_right))
        procurement_tuples = self.env.cr.fetchall()
        dates = []
        if moves_in_tuples:
            dates += [moves_in_tuples[0][2]]
        if moves_out_tuples:
            dates += [moves_out_tuples[0][2]]
        if procurement_tuples:
            dates += [procurement_tuples[0][1]]
        if dates:
            min_date = min(dates)

        # existing items
        existing_qty = sum([x.qty for x in stock_quant_restricted])
        intermediate_result += [{
            'proc_id': False,
            'location_id': location.id,
            'move_type': 'existing',
            'date': min_date,
            'qty': existing_qty,
            'move_id': False,
        }]

        # incoming items
        for sm in moves_in_tuples:
            intermediate_result += [{
                'proc_id': sm[3],
                'location_id': location.id,
                'move_type': 'in',
                'date': sm[2],
                'qty': sm[1],
                'move_id': sm[0],
            }]

        # outgoing items
        for sm in moves_out_tuples:
            intermediate_result += [{
                'proc_id': False,
                'location_id': location.id,
                'move_type': 'out',
                'date': sm[2],
                'qty': - sm[1],
                'move_id': sm[0],
            }]

        # planned items
        for po in procurement_tuples:
            intermediate_result += [{
                'proc_id': po[0],
                'location_id': location.id,
                'move_type': 'planned',
                'date': po[1],
                'qty': po[2],
                'move_id': False,
            }]

        intermediate_result = sorted(intermediate_result, key=lambda a: a['date'])
        qty = existing_qty
        for dictionary in intermediate_result:
            if dictionary['move_type'] != 'existing':
                qty += dictionary['qty']
            result += [{
                'proc_id': dictionary['proc_id'],
                'product_id': product_id,
                'location_id': dictionary['location_id'],
                'move_type': dictionary['move_type'],
                'date': dictionary['date'],
                'qty': qty,
                'move_qty': dictionary['qty'],
            }]

        result = sorted(result, key=lambda z: z[parameter_to_sort], reverse=to_reverse)
        result = [x for x in result if x['move_type'] in list_move_types]
        if limit:
            return result[:limit]
        else:
            return result


class StockLevelsReport(models.Model):
    _name = "stock.levels.report"
    _description = "Stock Levels Report"
    _order = "date"
    _auto = False

    id = fields.Integer("ID", readonly=True)
    product_id = fields.Many2one("product.product", string="Product", index=True)
    product_categ_id = fields.Many2one("product.category", string="Product Category")
    warehouse_id = fields.Many2one("stock.warehouse", string="Warehouse", index=True)
    other_warehouse_id = fields.Many2one("stock.warehouse", string="Origin/Destination")
    move_type = fields.Selection([('existing', 'Existing'), ('in', 'Incoming'), ('out', 'Outcoming')],
                                 string="Move Type", index=True)
    date = fields.Datetime("Date", index=True)
    qty = fields.Float("Stock Quantity", group_operator="last")
    move_qty = fields.Float("Moved Quantity")

    def init(self, cr):
        drop_view_if_exists(cr, "stock_levels_report")
        cr.execute("""
CREATE OR REPLACE VIEW stock_levels_report AS (
    WITH link_location_warehouse AS (
        SELECT
            sl.id AS location_id,
            sw.id AS warehouse_id
        FROM stock_warehouse sw
        LEFT JOIN stock_location sl_view ON sl_view.id = sw.view_location_id
        LEFT JOIN stock_location sl ON sl.parent_left >= sl_view.parent_left AND sl.parent_left <= sl_view.parent_right),


      min_product as (
    SELECT
                min(sm.date_expected)- interval '1 second' as min_date,
                sm.product_id as product_id
            FROM
                stock_move sm
                LEFT JOIN stock_location sl ON sm.location_dest_id = sl.id
                LEFT JOIN link_location_warehouse link ON link.location_id = sm.location_id
                LEFT JOIN link_location_warehouse link_dest ON link_dest.location_id = sm.location_dest_id
            WHERE ((link_dest.warehouse_id IS NOT NULL
                AND (link.warehouse_id IS NULL OR link.warehouse_id != link_dest.warehouse_id)) OR (link.warehouse_id IS NOT NULL
                AND (link_dest.warehouse_id IS NULL OR link.warehouse_id != link_dest.warehouse_id)))
                AND sm.state :: TEXT <> 'cancel' :: TEXT
                AND sm.state :: TEXT <> 'done' :: TEXT
                AND sm.state :: TEXT <> 'draft' :: TEXT
            group by sm.product_id
      )

SELECT
        foo.product_id :: TEXT || '-'
        || foo.warehouse_id :: TEXT || '-'
        || coalesce(foo.move_id :: TEXT, 'existing') AS id,
        foo.product_id,
        pt.categ_id                                  AS product_categ_id,
        foo.move_type,
        sum(foo.qty)
        OVER (PARTITION BY foo.warehouse_id, foo.product_id
            ORDER BY date)                           AS qty,
        foo.date                                     AS date,
        foo.qty                                      AS move_qty,
        foo.warehouse_id,
        foo.other_warehouse_id
    FROM
        (
SELECT
                sq.product_id      AS product_id,
                'existing' :: TEXT AS move_type,
                coalesce(min(mp.min_date),max(sq.in_date)) AS date,
                sum(sq.qty)        AS qty,
                NULL               AS move_id,
                link.warehouse_id,
                NULL               AS other_warehouse_id
            FROM
                stock_quant sq
                LEFT JOIN stock_location sl ON sq.location_id = sl.id
                LEFT JOIN link_location_warehouse link ON link.location_id = sl.location_id
                LEFT JOIN min_product mp on mp.product_id=sq.product_id
            WHERE link.warehouse_id IS NOT NULL
            GROUP BY sq.product_id, link.warehouse_id

            UNION ALL
        
SELECT
                sm.product_id    AS product_id,
                'in' :: TEXT     AS move_type,
                sm.date_expected AS date,
                sm.product_qty   AS qty,
                sm.id            AS move_id,
                link_dest.warehouse_id,
                link.warehouse_id AS other_warehouse_id
            FROM
                stock_move sm
                LEFT JOIN stock_location sl ON sm.location_dest_id = sl.id
                LEFT JOIN link_location_warehouse link ON link.location_id = sm.location_id
                LEFT JOIN link_location_warehouse link_dest ON link_dest.location_id = sm.location_dest_id
            WHERE link_dest.warehouse_id IS NOT NULL
                AND (link.warehouse_id IS NULL OR link.warehouse_id != link_dest.warehouse_id)
                AND sm.state :: TEXT <> 'cancel' :: TEXT
                AND sm.state :: TEXT <> 'done' :: TEXT
                AND sm.state :: TEXT <> 'draft' :: TEXT

            UNION ALL

            SELECT
                sm.product_id       AS product_id,
                'out' :: TEXT       AS move_type,
                sm.date_expected    AS date,
                -sm.product_qty     AS qty,
                sm.id               AS move_id,
                link.warehouse_id,
                link_dest.warehouse_id AS other_warehouse_id
            FROM
                stock_move sm
                LEFT JOIN stock_location sl ON sm.location_id = sl.id
                LEFT JOIN link_location_warehouse link ON link.location_id = sm.location_id
                LEFT JOIN link_location_warehouse link_dest ON link_dest.location_id = sm.location_dest_id
            WHERE link.warehouse_id IS NOT NULL
                AND (link_dest.warehouse_id IS NULL OR link.warehouse_id != link_dest.warehouse_id)
                AND sm.state :: TEXT <> 'cancel' :: TEXT
                AND sm.state :: TEXT <> 'done' :: TEXT
                AND sm.state :: TEXT <> 'draft' :: TEXT
) foo
        LEFT JOIN product_product pp ON foo.product_id = pp.id
        LEFT JOIN product_template pt ON pp.product_tmpl_id = pt.id
)
        """)


class ProductProduct(models.Model):
    _inherit = 'product.product'

    @api.multi
    def action_show_evolution(self):
        self.ensure_one()
        warehouses = self.env['stock.warehouse'].search([('company_id', '=', self.env.user.company_id.id)])
        if warehouses:
            wid = warehouses[0].id
        else:
            raise exceptions.except_orm(_("Error"), _("Your company does not have a warehouse"))
        ctx = dict(self.env.context)
        ctx.update({
            'search_default_warehouse_id': wid,
            'search_default_product_id': self.id,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _("Stock Evolution"),
            'res_model': 'stock.levels.report',
            'view_type': 'form',
            'view_mode': 'graph,tree',
            'context': ctx,
        }
