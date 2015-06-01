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

from datetime import datetime

from openerp.tests import common

class TestStockPerformanceImproved(common.TransactionCase):

    def setUp(self):
        super(TestStockPerformanceImproved, self).setUp()
        self.product = self.browse_ref("product.product_product_27")
        self.location_stock = self.browse_ref("stock.stock_location_stock")
        self.location_shelf = self.browse_ref("stock.stock_location_components")
        self.location_shelf2 = self.browse_ref("stock.stock_location_14")
        self.location_inv = self.browse_ref("stock.location_inventory")
        self.product_uom_unit_id = self.ref("product.product_uom_unit")
        self.picking_type_id = self.ref("stock.picking_type_internal")

    def test_10_simple_moves(self):
        """Basic checks of picking assignment."""
        move = self.env['stock.move'].create({
            'name': "Test Performance Improved",
            'product_id': self.product.id,
            'product_uom': self.product_uom_unit_id,
            'product_uom_qty': 12,
            'location_id': self.location_shelf.id,
            'location_dest_id': self.location_shelf2.id,
            'picking_type_id': self.picking_type_id,
        })
        move.action_confirm()
        self.assertTrue(move.picking_id, "Move should have been assigned a picking.")

        move2 = self.env['stock.move'].create({
            'name': "Test Performance Improved",
            'product_id': self.product.id,
            'product_uom': self.product_uom_unit_id,
            'product_uom_qty': 10,
            'location_id': self.location_shelf2.id,
            'location_dest_id': self.location_shelf.id,
            'picking_type_id': self.picking_type_id,
        })
        move2.action_confirm()
        self.assertFalse(move2.picking_id, "Move should not have been assigned a picking.")

        move.action_assign()
        move.action_done()
        move2.action_confirm()
        self.assertTrue(move2.picking_id, "Move should have been assigned a picking after transfer.")

    def test_20_linked_moves(self):
        """Test of linked moves."""
        move2 = self.env['stock.move'].create({
            'name': "Test Performance Improved",
            'product_id': self.product.id,
            'product_uom': self.product_uom_unit_id,
            'product_uom_qty': 8,
            'location_id': self.location_shelf2.id,
            'location_dest_id': self.location_shelf.id,
            'picking_type_id': self.picking_type_id,
        })
        move = self.env['stock.move'].create({
            'name': "Test Performance Improved",
            'product_id': self.product.id,
            'product_uom': self.product_uom_unit_id,
            'product_uom_qty': 8,
            'location_id': self.location_shelf.id,
            'location_dest_id': self.location_shelf2.id,
            'picking_type_id': self.picking_type_id,
            'move_dest_id': move2.id,
        })
        move2.action_confirm()
        move.action_confirm()
        self.assertTrue(move.picking_id, "Move should have been assigned a picking")
        self.assertFalse(move2.picking_id, "Move should not have been assigned a picking")
        move.action_assign()
        move.action_done()
        self.assertTrue(move2.picking_id, "Move should have been assigned a picking when previous is done")

    def test_30_check_picking(self):
        """Check if the moves are assigned to the correct picking."""
        move = self.env['stock.move'].create({
            'name': "Test Performance Improved",
            'product_id': self.product.id,
            'product_uom': self.product_uom_unit_id,
            'product_uom_qty': 13,
            'location_id': self.location_shelf.id,
            'location_dest_id': self.location_shelf2.id,
            'picking_type_id': self.picking_type_id,
        })
        move.action_confirm()
        picking = move.picking_id
        self.assertTrue(picking, "Move should have been assigned a picking.")
        self.assertEqual(picking.state, 'confirmed')
        move2 = self.env['stock.move'].create({
            'name': "Test Performance Improved",
            'product_id': self.product.id,
            'product_uom': self.product_uom_unit_id,
            'product_uom_qty': 9,
            'location_id': self.location_shelf.id,
            'location_dest_id': self.location_shelf2.id,
            'picking_type_id': self.picking_type_id,
        })
        move2.action_confirm()
        self.assertEqual(move2.picking_id, picking, "Move should have been assigned the existing confirmed picking")
        self.assertEqual(picking.state, 'confirmed')
        picking.action_assign()
        self.assertEqual(picking.state, 'assigned')
        move3 = self.env['stock.move'].create({
            'name': "Test Performance Improved",
            'product_id': self.product.id,
            'product_uom': self.product_uom_unit_id,
            'product_uom_qty': 4,
            'location_id': self.location_shelf.id,
            'location_dest_id': self.location_shelf2.id,
            'picking_type_id': self.picking_type_id,
        })
        move3.action_confirm()
        self.assertEqual(move3.picking_id, picking, "Move should have been assigned the existing assigned picking")
        picking.do_transfer()
        self.assertEqual(picking.state, 'done')
        for m in [move, move2, move3]:
            self.assertEqual(m.state, 'done')
        move4 = self.env['stock.move'].create({
            'name': "Test Performance Improved",
            'product_id': self.product.id,
            'product_uom': self.product_uom_unit_id,
            'product_uom_qty': 5,
            'location_id': self.location_shelf.id,
            'location_dest_id': self.location_shelf2.id,
            'picking_type_id': self.picking_type_id,
        })
        move4.action_confirm()
        self.assertTrue(move4.picking_id)
        self.assertNotEqual(move4.picking_id, picking, "Move should have been assigned a new picking")
