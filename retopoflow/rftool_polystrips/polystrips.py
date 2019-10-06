'''
Copyright (C) 2019 CG Cookie
http://cgcookie.com
hello@cgcookie.com

Created by Jonathan Denning, Jonathan Williamson, and Patrick Moore

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.
'''

import bgl
import bpy
import math
import random
from mathutils.geometry import intersect_point_tri_2d, intersect_point_tri_2d

from ..rftool import RFTool

from ..rfwidgets.rfwidget_brushstroke import RFWidget_BrushStroke
from ..rfwidgets.rfwidget_move import RFWidget_Move
from ...addon_common.common.bezier import CubicBezierSpline, CubicBezier
from ...addon_common.common.debug import dprint
from ...addon_common.common.drawing import Drawing, Cursors
from ...addon_common.common.maths import Vec2D, Point
from ...addon_common.common.profiler import profiler
from ...addon_common.common.utils import iter_pairs

from ...config.options import options

from .polystrips_utils import (
    RFTool_PolyStrips_Strip,
    hash_face_pair,
    strip_details,
    crawl_strip,
    is_boundaryvert, is_boundaryedge,
    process_stroke_filter, process_stroke_source,
    process_stroke_get_next, process_stroke_get_marks,
    mark_info,
    )


class RFTool_PolyStrips(RFTool):
    name        = 'PolyStrips'
    description = 'Create and edit strips of quads'
    icon        = 'polystrips_32.png'


################################################################################################
# following imports must happen *after* the above class, because each subclass depends on
# above class to be defined

from .polystrips_ops import PolyStrips_Ops


class PolyStrips(RFTool_PolyStrips, PolyStrips_Ops):
    @RFTool_PolyStrips.on_init
    def init(self):
        self.rfwidgets = {
            'brushstroke': RFWidget_BrushStroke(self),
            'move': RFWidget_Move(self),
        }
        self.rfwidget = self.rfwidgets['brushstroke']

    @RFTool_PolyStrips.on_reset
    def reset(self):
        self.strips = []
        self.strip_pts = []
        self.hovering_strips = set()
        self.hovering_handles = []
        self.sel_cbpts = []
        self.stroke_cbs = CubicBezierSpline()

    @RFTool_PolyStrips.on_target_change
    @profiler.function
    def update_target(self):
        if self._fsm.state in {'move handle'}: return

        self.strips = []

        # get selected quads
        bmquads = set(bmf for bmf in self.rfcontext.get_selected_faces() if len(bmf.verts) == 4)
        if not bmquads: return

        # find junctions at corners
        junctions = set()
        for bmf in bmquads:
            # skip if in middle of a selection
            if not any(is_boundaryvert(bmv, bmquads) for bmv in bmf.verts): continue
            # skip if in middle of possible strip
            edge0,edge1,edge2,edge3 = [is_boundaryedge(bme, bmquads) for bme in bmf.edges]
            if (edge0 or edge2) and not (edge1 or edge3): continue
            if (edge1 or edge3) and not (edge0 or edge2): continue
            junctions.add(bmf)

        # find junctions that might be in middle of strip but are ends to other strips
        boundaries = set((bme,bmf) for bmf in bmquads for bme in bmf.edges if is_boundaryedge(bme, bmquads))
        while boundaries:
            bme,bmf = boundaries.pop()
            for bme_ in bmf.neighbor_edges(bme):
                strip = crawl_strip(bmf, bme_, bmquads, junctions)
                if strip is None: continue
                junctions.add(strip[-1])

        # find strips between junctions
        touched = set()
        for bmf0 in junctions:
            bme0,bme1,bme2,bme3 = bmf0.edges
            edge0,edge1,edge2,edge3 = [is_boundaryedge(bme, bmquads) for bme in bmf0.edges]

            def add_strip(bme):
                strip = crawl_strip(bmf0, bme, bmquads, junctions)
                if not strip:
                    return
                bmf1 = strip[-1]
                if len(strip) > 1 and hash_face_pair(bmf0, bmf1) not in touched:
                    touched.add(hash_face_pair(bmf0,bmf1))
                    touched.add(hash_face_pair(bmf1,bmf0))
                    self.strips.append(RFTool_PolyStrips_Strip(strip))

            if not edge0: add_strip(bme0)
            if not edge1: add_strip(bme1)
            if not edge2: add_strip(bme2)
            if not edge3: add_strip(bme3)
            if options['polystrips max strips'] and len(self.strips) > options['polystrips max strips']:
                self.strips = []
                break

        self.update_strip_viz()

    @profiler.function
    def update_strip_viz(self):
        self.strip_pts = [[strip.curve.eval(i/10) for i in range(10+1)] for strip in self.strips]


    @RFTool_PolyStrips.FSM_State('main')
    def main(self) :

        Point_to_Point2D = self.rfcontext.Point_to_Point2D
        mouse = self.rfcontext.actions.mouse

        self.vis_accel = self.rfcontext.get_vis_accel()

        self.hovering_handles.clear()
        self.hovering_strips.clear()
        for strip in self.strips:
            for i,cbpt in enumerate(strip.curve):
                v = Point_to_Point2D(cbpt)
                if v is None: continue
                if (mouse - v).length > self.drawing.scale(options['select dist']): continue
                # do not filter out non-visible handles, because otherwise
                # they might not be movable if they are inside the model
                self.hovering_handles.append(cbpt)
                self.hovering_strips.add(strip)

        if self.rfcontext.actions.ctrl and not self.rfcontext.actions.shift:
            self.rfwidget = self.rfwidgets['brushstroke']
            Cursors.set('CROSSHAIR')
        elif self.hovering_handles:
            self.rfwidget = self.rfwidgets['move']
            Cursors.set('HAND')
        else:
            self.rfwidget = self.rfwidgets['brushstroke']
            Cursors.set('CROSSHAIR')

        # handle edits
        if self.hovering_handles:
            if self.rfcontext.actions.pressed('action'):
                return 'move handle'
            if self.rfcontext.actions.pressed('action alt0'):
                return 'rotate'
            if self.rfcontext.actions.pressed('action alt1'):
                return 'scale'


        if self.actions.pressed({'select', 'select add'}):
            ret = self.rfcontext.setup_selection_painting(
                'face',
                #fn_filter_bmelem=self.filter_edge_selection,
                kwargs_select={'supparts': False},
                kwargs_deselect={'subparts': False},
            )
            print('selecting', ret)
            return ret

    @RFTool_PolyStrips.FSM_State('move handle', 'can enter')
    def movehandle_canenter(self):
        return len(self.hovering_handles) > 0

    @RFTool_PolyStrips.FSM_State('move handle', 'enter')
    def movehandle_enter(self):
        self.sel_cbpts = []
        self.mod_strips = set()

        cbpts = list(self.hovering_handles)
        self.mod_strips |= self.hovering_strips
        for strip in self.strips:
            p0,p1,p2,p3 = strip.curve.points()
            if p0 in cbpts and p1 not in cbpts:
                cbpts.append(p1)
                self.mod_strips.add(strip)
            if p3 in cbpts and p2 not in cbpts:
                cbpts.append(p2)
                self.mod_strips.add(strip)

        for strip in self.mod_strips: strip.capture_edges()
        inners = [ p for strip in self.strips for p in strip.curve.points()[1:3] ]

        self.sel_cbpts = [(cbpt, cbpt in inners, Point(cbpt), self.rfcontext.Point_to_Point2D(cbpt)) for cbpt in cbpts]
        self.mousedown = self.rfcontext.actions.mouse
        self.mouselast = self.rfcontext.actions.mouse
        self.rfwidget = self.rfwidgets['move']
        self.move_done_pressed = 'confirm'
        self.move_done_released = 'action'
        self.move_cancelled = 'cancel'
        self.rfcontext.undo_push('manipulate bezier')

    @RFTool_PolyStrips.FSM_State('move handle')
    @RFTool_PolyStrips.dirty_when_done
    def modal_handle(self):
        if self.rfcontext.actions.pressed(self.move_done_pressed):
            return 'main'
        if self.rfcontext.actions.released(self.move_done_released):
            return 'main'
        if self.rfcontext.actions.pressed(self.move_cancelled):
            self.rfcontext.undo_cancel()
            return 'main'

        if (self.rfcontext.actions.mouse - self.mouselast).length == 0: return
        self.mouselast = self.rfcontext.actions.mouse

        delta = Vec2D(self.rfcontext.actions.mouse - self.mousedown)
        up,rt,fw = self.rfcontext.Vec_up(),self.rfcontext.Vec_right(),self.rfcontext.Vec_forward()
        for cbpt,inner,oco,oco2D in self.sel_cbpts:
            nco2D = oco2D + delta
            if not inner:
                xyz,_,_,_ = self.rfcontext.raycast_sources_Point2D(nco2D)
                if xyz: cbpt.xyz = xyz
            else:
                ov = self.rfcontext.Point2D_to_Vec(oco2D)
                nr = self.rfcontext.Point2D_to_Ray(nco2D)
                od = self.rfcontext.Point_to_depth(oco)
                cbpt.xyz = nr.eval(od / ov.dot(nr.d))

        for strip in self.hovering_strips:
            strip.update(self.rfcontext.nearest_sources_Point, self.rfcontext.raycast_sources_Point, self.rfcontext.update_face_normal)

        self.update_strip_viz()



    @RFTool_PolyStrips.Draw('post3d')
    def draw_post3d_spline(self):
        if not self.strips: return

        strips = self.strips
        hov_strips = self.hovering_strips

        Point_to_Point2D = self.rfcontext.Point_to_Point2D

        def is_visible(v):
            return True   # self.rfcontext.is_visible(v, None)

        def draw(alphamult, hov_alphamult, hover):
            nonlocal strips

            if not hover: hov_alphamult = alphamult

            size_outer = options['polystrips handle outer size']
            size_inner = options['polystrips handle inner size']
            border_outer = options['polystrips handle border']
            border_inner = options['polystrips handle border']

            bgl.glEnable(bgl.GL_BLEND)

            # draw outer-inner lines
            pts = [Point_to_Point2D(p) for strip in strips for p in strip.curve.points()]
            self.rfcontext.drawing.draw2D_lines(pts, (1,1,1,0.45), width=2)

            # draw junction handles (outer control points of curve)
            faces_drawn = set() # keep track of faces, so don't draw same handles 2+ times
            pts_outer,pts_inner = [],[]
            for strip in strips:
                bmf0,bmf1 = strip.end_faces()
                p0,p1,p2,p3 = strip.curve.points()
                if bmf0 not in faces_drawn:
                    if is_visible(p0): pts_outer += [Point_to_Point2D(p0)]
                    faces_drawn.add(bmf0)
                if bmf1 not in faces_drawn:
                    if is_visible(p3): pts_outer += [Point_to_Point2D(p3)]
                    faces_drawn.add(bmf1)
                if is_visible(p1): pts_inner += [Point_to_Point2D(p1)]
                if is_visible(p2): pts_inner += [Point_to_Point2D(p2)]
            self.rfcontext.drawing.draw2D_points(pts_outer, (1.00,1.00,1.00,1.0), radius=size_outer, border=border_outer, borderColor=(0.00,0.00,0.00,0.5))
            self.rfcontext.drawing.draw2D_points(pts_inner, (0.25,0.25,0.25,0.8), radius=size_inner, border=border_inner, borderColor=(0.75,0.75,0.75,0.4))

        if True:
            # always draw on top!
            bgl.glEnable(bgl.GL_BLEND)
            bgl.glDisable(bgl.GL_DEPTH_TEST)
            bgl.glDepthMask(bgl.GL_FALSE)
            draw(1.0, 1.0, False)
            bgl.glEnable(bgl.GL_DEPTH_TEST)
            bgl.glDepthMask(bgl.GL_TRUE)
        else:
            # allow handles to go under surface
            bgl.glDepthRange(0, 0.9999)     # squeeze depth just a bit
            bgl.glEnable(bgl.GL_BLEND)
            bgl.glDepthMask(bgl.GL_FALSE)   # do not overwrite depth
            bgl.glEnable(bgl.GL_DEPTH_TEST)

            # draw in front of geometry
            bgl.glDepthFunc(bgl.GL_LEQUAL)
            draw(
                options['target alpha'],
                options['target alpha'], # hover
                False, #options['polystrips handle hover']
            )

            # draw behind geometry
            bgl.glDepthFunc(bgl.GL_GREATER)
            draw(
                options['target hidden alpha'],
                options['target hidden alpha'], # hover
                False, #options['polystrips handle hover']
            )

            bgl.glDepthFunc(bgl.GL_LEQUAL)
            bgl.glDepthRange(0.0, 1.0)
            bgl.glDepthMask(bgl.GL_TRUE)

    @RFTool_PolyStrips.Draw('post2d')
    def draw_post2d(self):
        self.rfcontext.drawing.set_font_size(12)
        Point_to_Point2D = self.rfcontext.Point_to_Point2D
        text_draw2D = self.rfcontext.drawing.text_draw2D

        for strip in self.strips:
            c = len(strip)
            vs = [Point_to_Point2D(f.center()) for f in strip]
            vs = [Vec2D(v) for v in vs if v]
            if not vs: continue
            ctr = sum(vs, Vec2D((0,0))) / len(vs)
            text_draw2D('%d' % c, ctr+Vec2D((2,14)), color=(1,1,0,1), dropshadow=(0,0,0,0.5))