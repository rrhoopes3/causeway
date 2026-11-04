"""Build Weebles: plump velvet demon with a ceramic visor mask.

Hierarchy (names are the three.js API):
  Body
    ...parts
    InnerFace   the demon's head, seen through a hole in the shell once the mask is up
    Mask        rises and tips back on local -X, so its face shows above the demon's; companion.js does this
Character faces Blender -Y. Units metres. Height ~0.24 m.

Run headless to rebuild companion.glb and the two stills:
  blender --background --factory-startup --python causeway/companion/build_weebles.py
"""
from __future__ import annotations

import math
import traceback
from pathlib import Path

import bpy
import bmesh
import numpy as np
from mathutils import Matrix, Vector

STATIC = Path(__file__).resolve().parent / "static"
MASK_TEX = str(STATIC / "weebles_mask.jpg")
COL_NAME = "WeeblesChar"
# same as the lift in companion.js, so the stills show what the page shows
MASK_LIFT = -0.32
MASK_RISE = 0.122
PAGE_BG = "#0b0d11"
HEAT_RAMP = (
    (0.0, "#050102"),
    (0.2, "#34060a"),
    (0.45, "#b3190a"),
    (0.68, "#ff5e12"),
    (0.86, "#ffb838"),
    (1.0, "#fff3cf"),
)
# right half of the face, x and z in metres from the head's axis; the left half is its mirror
EYE = ((0.0048, 0.0), (0.0424, 0.0297), (0.0371, 0.0095), (0.0265, -0.0042), (0.0138, -0.0058))
BROW = ((0.0, 0.0062), (0.0459, 0.0426), (0.0521, 0.0314), (0.0062, -0.0022))
MOUTH = (
    (0.0, -0.0062), (0.0168, -0.0095), (0.0336, -0.0067), (0.0482, 0.0034),
    (0.0403, -0.0146), (0.028, -0.028), (0.0146, -0.0364), (0.0, -0.0398),
)
TEETH = (
    ((0.0, -0.0067), (0.0123, -0.009), (0.0056, -0.028)),
    ((0.0123, -0.009), (0.028, -0.0078), (0.0202, -0.0325)),
    ((0.028, -0.0078), (0.0414, -0.0022), (0.0336, -0.0207)),
    ((0.0, -0.0398), (0.0056, -0.0392), (0.0, -0.0235)),
    ((0.0056, -0.0392), (0.0202, -0.0336), (0.0129, -0.0151)),
    ((0.0202, -0.0336), (0.0347, -0.0224), (0.0274, -0.0118)),
)


def srgb(hex_color: str) -> tuple[float, float, float, float]:
    h = hex_color.lstrip("#")
    rgb = [int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]
    out = []
    for c in rgb:
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return (*out, 1.0)


def smoothstep(a, b, x):
    t = max(0.0, min(1.0, (x - a) / (b - a)))
    return t * t * (3 - 2 * t)


def pbr(name, *, base="#808080", metallic=0.0, roughness=0.5, **kw):
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    for n in list(nt.nodes):
        nt.nodes.remove(n)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    bsdf.location = (0, 0)
    out.location = (300, 0)
    bsdf.inputs["Base Color"].default_value = srgb(base) if isinstance(base, str) else base
    bsdf.inputs["Metallic"].default_value = metallic
    bsdf.inputs["Roughness"].default_value = roughness
    for key, val in kw.items():
        label = key.replace("_", " ").title() if "_" in key else key
        # map py kwargs to socket names
        mapping = {
            "specular": "Specular IOR Level",
            "coat": "Coat Weight",
            "coat_roughness": "Coat Roughness",
            "coat_ior": "Coat IOR",
            "sheen": "Sheen Weight",
            "sheen_roughness": "Sheen Roughness",
            "sheen_tint": "Sheen Tint",
            "subsurface": "Subsurface Weight",
            "subsurface_scale": "Subsurface Scale",
            "emission": "Emission Color",
            "emission_strength": "Emission Strength",
            "alpha": "Alpha",
            "ior": "IOR",
        }
        sock = mapping.get(key, label)
        if sock in bsdf.inputs:
            inp = bsdf.inputs[sock]
            if sock in ("Sheen Tint", "Emission Color", "Coat Tint", "Specular Tint") and isinstance(val, str):
                val = srgb(val)
            inp.default_value = val
    return mat


def add_image(mat, filepath, socket="Base Color"):
    nt = mat.node_tree
    bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = bpy.data.images.load(filepath, check_existing=True)
    tex.location = (-400, 0)
    nt.links.new(tex.outputs["Color"], bsdf.inputs[socket])
    return tex


def new_obj(name, mesh, col, parent=None, loc=(0, 0, 0)):
    obj = bpy.data.objects.new(name, mesh)
    obj.location = loc
    col.objects.link(obj)
    if parent is not None:
        obj.parent = parent
    return obj


def mesh_from_bm(name, bm):
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    me.update()
    me.validate()
    return me


def ico(radius, subdivisions=3, scale=(1, 1, 1)):
    bm = bmesh.new()
    bmesh.ops.create_icosphere(bm, subdivisions=subdivisions, radius=radius)
    sx, sy, sz = scale
    for v in bm.verts:
        v.co.x *= sx
        v.co.y *= sy
        v.co.z *= sz
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return bm


def capsule(radius, depth, segs=16):
    """Z-up capsule, origin at centre."""
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=segs, v_segments=segs // 2, radius=radius)
    half = depth / 2
    for v in bm.verts:
        if v.co.z > 0.0001:
            v.co.z += half
        elif v.co.z < -0.0001:
            v.co.z -= half
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return bm


def tapered_horn(points, start_r, end_r, segs=14):
    """Sweep a tapered tube through points (list of Vector). Origin at first point."""
    bm = bmesh.new()
    n = len(points)
    rings = []
    for i, p in enumerate(points):
        t = i / (n - 1)
        r = start_r * (1 - t) ** 0.85 + end_r * t
        if i < n - 1:
            tangent = (points[i + 1] - p).normalized()
        else:
            tangent = (p - points[i - 1]).normalized()
        up = Vector((0, 0, 1))
        if abs(tangent.dot(up)) > 0.9:
            up = Vector((1, 0, 0))
        x = tangent.cross(up).normalized()
        y = x.cross(tangent).normalized()
        ring = []
        for k in range(segs):
            a = 2 * math.pi * k / segs
            v = bm.verts.new(p + (x * math.cos(a) + y * math.sin(a)) * r)
            ring.append(v)
        rings.append(ring)
    for i in range(n - 1):
        for k in range(segs):
            a = rings[i][k]
            b = rings[i][(k + 1) % segs]
            c = rings[i + 1][(k + 1) % segs]
            d = rings[i + 1][k]
            bm.faces.new((a, b, c, d))
    # tip cap
    tip = bm.verts.new(points[-1] + (points[-1] - points[-2]).normalized() * end_r * 0.4)
    for k in range(segs):
        bm.faces.new((rings[-1][k], rings[-1][(k + 1) % segs], tip))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return bm


def ring_torus(major, minor, major_n=48, minor_n=10):
    """Torus in XY, then caller rotates it. bmesh has no create_torus in 5.2."""
    bm = bmesh.new()
    bmesh.ops.create_circle(bm, cap_ends=False, cap_tris=False, segments=minor_n, radius=minor)
    bmesh.ops.translate(bm, verts=bm.verts[:], vec=(major, 0, 0))
    geom = bm.verts[:] + bm.edges[:]
    bmesh.ops.spin(
        bm,
        geom=geom,
        angle=math.tau,
        steps=major_n,
        axis=Vector((0, 0, 1)),
        cent=Vector((0, 0, 0)),
    )
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-4)
    dead = [e for e in bm.edges if e.is_boundary]
    # close the spin seam; doubles should have done it
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return bm


def spherical_cap(radius, cap_angle, segs_u=48, segs_v=20, facing="-Y"):
    """Open spherical cap, pole along -Y, origin at the sphere centre. Clean circular rim."""
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=segs_u, v_segments=segs_v, radius=radius)
    # default UV sphere is Z-up; rotate so the +Z pole points to -Y
    bmesh.ops.rotate(
        bm,
        verts=bm.verts,
        cent=Vector((0, 0, 0)),
        matrix=Matrix.Rotation(math.radians(-90), 3, "X"),
    )
    plane_y = -radius * math.cos(cap_angle)
    geom = list(bm.verts) + list(bm.edges) + list(bm.faces)
    bmesh.ops.bisect_plane(
        bm,
        geom=geom,
        dist=1e-5,
        plane_co=Vector((0.0, plane_y, 0.0)),
        plane_no=Vector((0.0, 1.0, 0.0)),
        clear_outer=True,
        clear_inner=False,
    )
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return bm


def project_uv_from_y(me, radius):
    """Planar UV from -Y, mapping the cap's XZ disk into 0..1."""
    uv = me.uv_layers.new(name="UVMap")
    for poly in me.polygons:
        for li in poly.loop_indices:
            loop = me.loops[li]
            co = me.vertices[loop.vertex_index].co
            u = co.x / (2 * radius) + 0.5
            v = co.z / (2 * radius) + 0.5
            uv.data[li].uv = (u, v)


def polygon_sdf(x, y, points):
    """Distance from each (x, y) to a closed polygon, negative inside."""
    pts = np.asarray(points)
    dist = np.full(x.shape, np.inf)
    inside = np.zeros(x.shape, dtype=bool)
    for a, b in zip(pts, np.roll(pts, -1, axis=0)):
        ex, ey = b - a
        wx, wy = x - a[0], y - a[1]
        t = np.clip((wx * ex + wy * ey) / (ex * ex + ey * ey), 0, 1)
        dist = np.minimum(dist, np.hypot(wx - t * ex, wy - t * ey))
        if ey:
            inside ^= ((a[1] > y) != (b[1] > y)) & (wx < wy * ex / ey)
    return np.where(inside, -dist, dist)


def face_image(span: float, size: int = 384) -> str:
    """Paint the demon's face as heat, colour it through HEAT_RAMP, save a JPEG for the emissive map. span = half width in metres."""
    pixel = 2 * span / size
    axis = (np.arange(size) + 0.5) * pixel - span
    z, x = np.meshgrid(axis, axis, indexing="ij")
    side = np.abs(x)

    def solid(sdf):
        return np.clip(0.5 - sdf / pixel, 0, 1)

    eye = polygon_sdf(side, z, EYE)
    brow = polygon_sdf(side, z, BROW)
    mouth = polygon_sdf(side, z, MOUTH)
    teeth = np.min([polygon_sdf(side, z, t) for t in TEETH], axis=0)
    pupil = np.hypot((side - 0.0244) / 0.0016, (z - 0.0085) / 0.0085) - 1

    # soft red bleeding out onto the dark head
    heat = np.maximum(0.3 * np.exp(-np.maximum(brow, 0) / 0.006), 0.28 * np.exp(-np.maximum(mouth, 0) / 0.006))
    heat = np.maximum(heat, 0.5 * np.exp(-np.maximum(eye, 0) / 0.0035))
    # the mouth burns low behind the teeth, brighter along its centre line
    throat = 0.42 + 0.18 * np.clip(-mouth / 0.012, 0, 1)
    heat = heat + (throat - heat) * solid(mouth)
    heat = heat + ((0.9 + 0.1 * np.clip(-teeth / 0.004, 0, 1)) - heat) * solid(teeth)
    # eyes run white hot at the core, with a black slit
    fire = 0.76 + 0.24 * np.clip(-eye / 0.005, 0, 1)
    heat = heat + (fire - heat) * solid(eye)
    heat = heat * (1 - np.clip(0.5 - pupil * 2, 0, 1) * solid(eye))

    stops = [s for s, _ in HEAT_RAMP]
    rgb = [np.interp(heat, stops, [int(c.lstrip("#")[i : i + 2], 16) / 255 for _, c in HEAT_RAMP]) for i in (0, 2, 4)]
    rgba = np.dstack((*rgb, np.ones_like(heat)))

    img = bpy.data.images.new("WeeblesFace", size, size)
    img.pixels.foreach_set(rgba.astype(np.float32).ravel())
    img.file_format = "JPEG"
    out = str(Path(bpy.app.tempdir) / "weebles_face.jpg")
    img.save(filepath=out, quality=85)
    bpy.data.images.remove(img)
    return out


def shade_smooth(obj):
    mesh = obj.data
    mesh.shade_smooth()
    if hasattr(mesh, "use_auto_smooth"):
        mesh.use_auto_smooth = True
        mesh.auto_smooth_angle = math.radians(60)
    else:
        # Blender 4.1+: smooth by angle modifier
        pass


def subsurf(obj, levels=2):
    m = obj.modifiers.new("Subsurf", "SUBSURF")
    m.levels = levels
    m.render_levels = levels
    m.quality = 3
    m.uv_smooth = "PRESERVE_CORNERS"
    return m


def origin_to_world(obj, world_point: Vector):
    """Move mesh so that world_point becomes the object origin, keep visual place."""
    bpy.context.view_layer.update()
    local = obj.matrix_world.inverted() @ world_point
    obj.data.transform(Matrix.Translation(-local))
    obj.location = world_point
    bpy.context.view_layer.update()


def build():
    sc = bpy.data.scenes.get("Weebles")
    if sc is None:
        sc = bpy.context.scene
        sc.name = "Weebles"
    win = bpy.context.window
    if win is not None:
        win.scene = sc

    # collection
    col = bpy.data.collections.get(COL_NAME) or bpy.data.collections.new(COL_NAME)
    if col.name not in sc.collection.children:
        sc.collection.children.link(col)
    for obj in list(col.objects):
        bpy.data.objects.remove(obj, do_unlink=True)

    # materials
    mat_body = pbr(
        "WeeblesBody",
        base="#1c1018",
        roughness=0.68,
        sheen=0.55,
        sheen_roughness=0.4,
        sheen_tint="#3a2038",
        subsurface=0.22,
        subsurface_scale=0.04,
        specular=0.35,
        coat=0.08,
        coat_roughness=0.5,
    )
    if "Subsurface Radius" in (bsdf := next(n for n in mat_body.node_tree.nodes if n.type == "BSDF_PRINCIPLED")).inputs:
        bsdf.inputs["Subsurface Radius"].default_value = (0.8, 0.35, 0.5)
    mat_horn = pbr(
        "WeeblesHorn",
        base="#6b1020",
        roughness=0.28,
        coat=0.45,
        coat_roughness=0.18,
        coat_ior=1.5,
        specular=0.55,
        emission="#ff3a1a",
        emission_strength=0.04,
    )
    mat_claw = pbr("WeeblesClaw", base="#f3eadc", roughness=0.32, specular=0.45, coat=0.15, coat_roughness=0.3)
    mat_mask = pbr(
        "WeeblesMask",
        base="#f4ece3",
        roughness=0.16,
        coat=0.9,
        coat_roughness=0.08,
        coat_ior=1.6,
        specular=0.55,
        subsurface=0.12,
        subsurface_scale=0.03,
    )
    if Path(MASK_TEX).is_file():
        add_image(mat_mask, MASK_TEX)
    mat_mask_back = pbr("WeeblesMaskBack", base="#2a1820", roughness=0.78, specular=0.2)
    mat_glow = pbr(
        "WeeblesGlow",
        base="#ff7a2e",
        roughness=0.4,
        emission="#ff7a2e",
        emission_strength=6.0,
    )
    mat_inner = pbr("WeeblesInner", base="#050304", roughness=0.85, specular=0.1, emission_strength=1.0)
    mat_hood = pbr("WeeblesHood", base="#050304", roughness=0.9, specular=0.1)
    mat_floor = pbr("WeeblesFloor", base="#0b0d11", roughness=0.85)

    # --- Body (named Body): round dumpling, centre at origin of the object ---
    BODY_R = 0.078
    body_bm = ico(BODY_R, subdivisions=4, scale=(1.06, 0.98, 1.08))
    for v in body_bm.verts:
        # squash a little sitting-belly, keep the front ( -Y ) full so the visor can wrap it
        # (blended, not stepped: a step would show as kinks in the edge of the face hole)
        belly = smoothstep(0.0, -0.04, v.co.z)
        v.co.x *= 1 - 0.08 * belly
        v.co.y *= 1 - 0.05 * belly
        v.co *= 1 - 0.03 * smoothstep(0.01, 0.05, v.co.z)
    # open the shell behind the visor, a little inside its rim, so the visor hides the hole when down
    bmesh.ops.bisect_plane(
        body_bm,
        geom=list(body_bm.verts) + list(body_bm.edges) + list(body_bm.faces),
        dist=1e-5,
        plane_co=Vector((0.0, -0.0575, 0.004)),
        plane_no=Vector((0.0, -0.999, 0.042)),
        clear_outer=True,
        clear_inner=False,
    )
    # turn the cut edge inward so it reads as a thick hood, not a paper edge, and seals against the head
    lip = bmesh.ops.extrude_edge_only(body_bm, edges=[e for e in body_bm.edges if e.is_boundary])
    for elem in lip["geom"]:
        if isinstance(elem, bmesh.types.BMVert):
            elem.co.y += 0.03
        elif isinstance(elem, bmesh.types.BMFace):
            elem.material_index = 1
    bmesh.ops.recalc_face_normals(body_bm, faces=body_bm.faces)
    body_me = mesh_from_bm("Body", body_bm)
    body = new_obj("Body", body_me, col, loc=(0, 0, 0.096))
    body.data.materials.append(mat_body)
    body.data.materials.append(mat_hood)
    shade_smooth(body)
    subsurf(body, 1)

    # stubby legs
    for side, name in ((-1, "Leg.L"), (1, "Leg.R")):
        bm = ico(0.024, subdivisions=3, scale=(0.9, 0.85, 1.15))
        obj = new_obj(name, mesh_from_bm(name, bm), col, parent=body, loc=(side * 0.03, 0.006, -0.072))
        obj.data.materials.append(mat_body)
        shade_smooth(obj)
        subsurf(obj, 1)
        for k, dx in enumerate((-0.008, 0.0, 0.008)):
            cbm = ico(0.0046, subdivisions=2, scale=(0.7, 1.5, 0.55))
            c = new_obj(f"Toe.{name[-1]}.{k}", mesh_from_bm(f"Toe.{name[-1]}.{k}", cbm), col, parent=obj, loc=(dx, -0.018, -0.022))
            c.rotation_euler = (math.radians(22), 0, 0)
            c.data.materials.append(mat_claw)
            shade_smooth(c)

    # short arms, slightly forward
    for side, name in ((-1, "Arm.L"), (1, "Arm.R")):
        bm = capsule(0.016, 0.02, segs=16)
        obj = new_obj(name, mesh_from_bm(name, bm), col, parent=body, loc=(side * 0.072, -0.018, -0.004))
        obj.rotation_euler = (math.radians(55), 0, side * math.radians(-28))
        obj.data.materials.append(mat_body)
        shade_smooth(obj)
        subsurf(obj, 1)
        hbm = ico(0.015, subdivisions=2, scale=(1.15, 0.9, 0.8))
        hand = new_obj(f"Hand.{name[-1]}", mesh_from_bm(f"Hand.{name[-1]}", hbm), col, parent=obj, loc=(0, 0, -0.026))
        hand.data.materials.append(mat_body)
        shade_smooth(hand)
        for k, (dx, dy) in enumerate(((-0.007, -0.005), (0.0, -0.008), (0.007, -0.005))):
            cbm = ico(0.0042, subdivisions=2, scale=(0.7, 0.7, 1.7))
            c = new_obj(f"Claw.{name[-1]}.{k}", mesh_from_bm(f"Claw.{name[-1]}.{k}", cbm), col, parent=hand, loc=(dx, dy, -0.011))
            c.data.materials.append(mat_claw)
            shade_smooth(c)

    # chunky horns that curve back (+Y) and out
    horn_pts = [
        Vector((0.0, 0.0, 0.0)),
        Vector((0.008, 0.008, 0.018)),
        Vector((0.016, 0.02, 0.038)),
        Vector((0.02, 0.03, 0.054)),
        Vector((0.014, 0.028, 0.07)),
    ]
    for side, name in ((-1, "Horn.L"), (1, "Horn.R")):
        pts = [Vector((side * p.x, p.y, p.z)) for p in horn_pts]
        bm = tapered_horn(pts, 0.02, 0.0035, segs=16)
        obj = new_obj(name, mesh_from_bm(name, bm), col, parent=body, loc=(side * 0.03, 0.02, 0.055))
        obj.rotation_euler = (math.radians(-18), 0, side * math.radians(10))
        obj.data.materials.append(mat_horn)
        shade_smooth(obj)
        subsurf(obj, 1)

    # tail from the lower back (+Y), curling out
    tail_pts = [
        Vector((0, 0, 0)),
        Vector((0.008, 0.022, -0.006)),
        Vector((0.02, 0.048, 0.004)),
        Vector((0.028, 0.07, 0.022)),
        Vector((0.024, 0.082, 0.04)),
    ]
    tbm = tapered_horn(tail_pts, 0.012, 0.004, segs=14)
    tail = new_obj("Tail", mesh_from_bm("Tail", tbm), col, parent=body, loc=(0.0, 0.06, -0.04))
    tail.rotation_euler = (math.radians(-10), math.radians(-15), math.radians(10))
    tail.data.materials.append(mat_body)
    shade_smooth(tail)
    subsurf(tail, 1)
    sbm = ico(0.016, subdivisions=2, scale=(0.45, 0.16, 1.2))
    spade = new_obj("Spade", mesh_from_bm("Spade", sbm), col, parent=tail, loc=(0.024, 0.084, 0.044))
    spade.rotation_euler = (math.radians(10), math.radians(20), math.radians(8))
    spade.data.materials.append(mat_horn)
    shade_smooth(spade)

    # the demon: a dark head filling the hole, its face painted in the emissive map, well clear of the visor
    inner_r = 0.072
    inner_ang = math.radians(80)
    ibm = spherical_cap(inner_r, inner_ang, segs_u=32, segs_v=16)
    inner_me = mesh_from_bm("InnerFace", ibm)
    face_span = inner_r * math.sin(inner_ang)
    project_uv_from_y(inner_me, face_span)
    add_image(mat_inner, face_image(face_span), socket="Emission Color")
    inner = new_obj("InnerFace", inner_me, col, parent=body, loc=(0, 0.01, 0.004))
    inner.data.materials.append(mat_inner)
    shade_smooth(inner)

    # --- Mask: visor on the front of the dumpling, dark body reads as a hood ---
    # stands proud of the shell all the way round to its rim, so the shell cannot poke through it
    mask_r = BODY_R * 1.04
    cap_ang = math.radians(48)
    mbm = spherical_cap(mask_r, cap_ang, segs_u=72, segs_v=32)
    mask_me = mesh_from_bm("MaskMesh", mbm)
    # the art fills the visor out to its rim; any tighter and the texture repeats around the edge
    project_uv_from_y(mask_me, mask_r * math.sin(cap_ang))
    mask_mesh = new_obj("MaskFront", mask_me, col, loc=(0, 0, 0))
    mask_mesh.data.materials.append(mat_mask)
    shade_smooth(mask_mesh)
    subsurf(mask_mesh, 1)

    bbm = spherical_cap(mask_r * 0.992, cap_ang * 0.99, segs_u=40, segs_v=16)
    for f in bbm.faces:
        f.normal_flip()
    back_me = mesh_from_bm("MaskBackMesh", bbm)
    mask_back = new_obj("MaskBack", back_me, col)
    mask_back.data.materials.append(mat_mask_back)
    shade_smooth(mask_back)

    rim_r = mask_r * math.sin(cap_ang)
    rim_y = -mask_r * math.cos(cap_ang)
    tbm = ring_torus(rim_r, 0.0016, 64, 8)
    bmesh.ops.rotate(tbm, verts=tbm.verts, cent=Vector((0, 0, 0)), matrix=Matrix.Rotation(math.radians(90), 3, "X"))
    rim = new_obj("MaskRim", mesh_from_bm("MaskRim", tbm), col, loc=(0, rim_y, 0))
    rim.data.materials.append(mat_glow)
    shade_smooth(rim)

    gbm = ring_torus(rim_r * 1.002, 0.0011, 64, 8)
    bmesh.ops.rotate(gbm, verts=gbm.verts, cent=Vector((0, 0, 0)), matrix=Matrix.Rotation(math.radians(90), 3, "X"))
    glow = new_obj("MaskGlow", mesh_from_bm("MaskGlow", gbm), col, loc=(0, rim_y + 0.0004, 0))
    glow.data.materials.append(mat_glow)
    shade_smooth(glow)

    # concentric with Body: Body origin is the dumpling centre
    mask_loc = Vector(body.location) + Vector((0.0, 0.0, 0.002))
    for o in (mask_mesh, mask_back, rim, glow):
        o.location = Vector(o.location) + mask_loc

    bpy.context.view_layer.update()
    # hinge: highest Z of mask front in world
    hinge = Vector((0.0, mask_loc.y + rim_y, mask_loc.z + rim_r))
    # more accurately: max z of mask_mesh
    ws = [mask_mesh.matrix_world @ v.co for v in mask_mesh.data.vertices]
    top = max(ws, key=lambda p: p.z)
    hinge = Vector((0.0, top.y, top.z))

    mask = bpy.data.objects.new("Mask", None)
    mask.empty_display_type = "PLAIN_AXES"
    mask.empty_display_size = 0.02
    col.objects.link(mask)
    mask.location = hinge
    mask.parent = body
    # keep world location after parenting
    bpy.context.view_layer.update()
    mask.matrix_parent_inverse = body.matrix_world.inverted()

    for o in (mask_mesh, mask_back, rim, glow):
        mw = o.matrix_world.copy()
        o.parent = mask
        bpy.context.view_layer.update()
        o.matrix_parent_inverse = mask.matrix_world.inverted()
        o.matrix_world = mw

    # look-dev floor (not parented, hide from export by putting it outside collection? keep in scene root)
    fbm = bmesh.new()
    bmesh.ops.create_grid(fbm, x_segments=1, y_segments=1, size=0.6)
    floor = new_obj("LookFloor", mesh_from_bm("LookFloor", fbm), sc.collection, loc=(0, 0, 0))
    floor.data.materials.append(mat_floor)

    # camera for looks
    cam_data = bpy.data.cameras.get("WeeblesCam") or bpy.data.cameras.new("WeeblesCam")
    cam = bpy.data.objects.get("WeeblesCam")
    if cam is None:
        cam = bpy.data.objects.new("WeeblesCam", cam_data)
        sc.collection.objects.link(cam)
    cam.data.lens = 85
    cam.location = (0.22, -0.42, 0.16)
    cam.rotation_euler = (math.radians(78), 0, math.radians(22))
    sc.camera = cam

    key = bpy.data.objects.get("WeeblesKey")
    if key is None:
        kd = bpy.data.lights.new("WeeblesKey", "AREA")
        kd.energy = 40
        kd.size = 0.4
        kd.color = (1.0, 0.94, 0.88)
        key = bpy.data.objects.new("WeeblesKey", kd)
        sc.collection.objects.link(key)
    key.location = (0.25, -0.3, 0.4)
    key.rotation_euler = (math.radians(55), 0, math.radians(35))

    rim_l = bpy.data.objects.get("WeeblesRim")
    if rim_l is None:
        rd = bpy.data.lights.new("WeeblesRim", "AREA")
        rd.energy = 25
        rd.size = 0.3
        rd.color = (0.55, 0.35, 1.0)
        rim_l = bpy.data.objects.new("WeeblesRim", rd)
        sc.collection.objects.link(rim_l)
    rim_l.location = (-0.2, 0.25, 0.3)
    rim_l.rotation_euler = (math.radians(70), 0, math.radians(-150))

    ember = bpy.data.objects.get("WeeblesEmber")
    if ember is None:
        ed = bpy.data.lights.new("WeeblesEmber", "POINT")
        ed.energy = 4
        ed.color = (1.0, 0.45, 0.18)
        ed.shadow_soft_size = 0.03
        ember = bpy.data.objects.new("WeeblesEmber", ed)
        sc.collection.objects.link(ember)
    ember.location = (0, -0.07, 0.12)

    bpy.context.view_layer.update()

    # report
    deps = bpy.context.evaluated_depsgraph_get()
    faces = 0
    names = []
    for o in col.objects:
        names.append(f"{o.name} parent={o.parent.name if o.parent else '-'} loc={[round(x,3) for x in o.location]}")
        if o.type == "MESH":
            ev = o.evaluated_get(deps)
            me = ev.to_mesh(depsgraph=deps)
            faces += len(me.polygons)
            ev.to_mesh_clear()
    xs, ys, zs = [], [], []
    for o in col.objects:
        if o.type != "MESH":
            continue
        for c in o.bound_box:
            w = o.matrix_world @ Vector(c)
            xs.append(w.x); ys.append(w.y); zs.append(w.z)
    print("objects", len(col.objects), "eval_faces", faces)
    print("bbox", round(min(xs), 3), round(min(ys), 3), round(min(zs), 3), "->", round(max(xs), 3), round(max(ys), 3), round(max(zs), 3))
    print("height", round(max(zs) - min(zs), 3))
    print("has_Body", "Body" in [o.name for o in col.objects])
    print("has_Mask", "Mask" in [o.name for o in col.objects])
    print("Mask_loc", [round(x, 4) for x in bpy.data.objects["Mask"].location])
    print("MASK_TEX", Path(MASK_TEX).is_file())


def export_glb(path: Path | None = None) -> Path:
    """Write collection WeeblesChar as a three.js-ready GLB ( +Y up, modifiers applied)."""
    out = Path(path) if path else Path(__file__).resolve().parent / "static" / "companion.glb"
    bpy.ops.export_scene.gltf(
        filepath=str(out),
        export_format="GLB",
        collection=COL_NAME,
        export_yup=True,
        export_apply=True,
        export_cameras=False,
        export_lights=False,
        export_extras=True,
        export_skins=False,
        export_animations=False,
        export_draco_mesh_compression_enable=False,
        export_materials="EXPORT",
        export_image_format="AUTO",
        export_texcoords=True,
        export_normals=True,
    )
    print("glb", out, out.stat().st_size)
    return out


def render_stills(folder: Path = STATIC, size: int = 512) -> None:
    """Render the phone page's fallback images: mask down and lifted, same framing, on the page's background."""
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.samples = 64
    sc.cycles.use_denoising = True
    sc.render.resolution_x = sc.render.resolution_y = size
    sc.render.resolution_percentage = 100
    sc.render.image_settings.file_format = "JPEG"
    sc.render.image_settings.quality = 90
    sc.view_settings.view_transform = "Standard"
    sc.view_settings.look = "None"
    sc.world = bpy.data.worlds.get("WeeblesWorld") or bpy.data.worlds.new("WeeblesWorld")
    sc.world.use_nodes = True
    bg = next(n for n in sc.world.node_tree.nodes if n.type == "BACKGROUND")
    bg.inputs["Color"].default_value = srgb(PAGE_BG)
    bg.inputs["Strength"].default_value = 1.0
    # only the character and its key and rim, dimmed from the look-dev levels for this view transform:
    # no look-dev floor, no default cube from a fresh scene, and the face itself is the ember
    keep = {*bpy.data.collections[COL_NAME].objects, *(bpy.data.objects[n] for n in ("WeeblesKey", "WeeblesRim"))}
    for o in sc.objects:
        o.hide_render = o not in keep
    bpy.data.objects["WeeblesKey"].data.energy = 3
    bpy.data.objects["WeeblesRim"].data.energy = 6
    # a little to one side, framed to hold the risen mask above him
    cam = sc.camera
    cam.data.lens = 88
    cam.location = (-0.2, -0.88, 0.2)
    cam.rotation_euler = (Vector((0.0, 0.0, 0.155)) - Vector(cam.location)).to_track_quat("-Z", "Y").to_euler()
    mask = bpy.data.objects["Mask"]
    home = mask.location.z
    for name, lift, rise in (("weebles_idle.jpg", 0.0, 0.0), ("weebles_open.jpg", MASK_LIFT, MASK_RISE)):
        mask.rotation_euler.x = lift
        mask.location.z = home + rise
        sc.render.filepath = str(Path(folder) / name)
        bpy.ops.render.render(write_still=True)
        print("still", sc.render.filepath, Path(sc.render.filepath).stat().st_size)
    mask.rotation_euler.x = 0.0
    mask.location.z = home


if __name__ == "__main__":
    try:
        build()
        export_glb()
        render_stills()
        print("OK")
    except Exception:
        print(traceback.format_exc())
        raise
