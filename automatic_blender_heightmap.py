bl_info = {
    "name": "Heightmap Baker",
    "author": "Paul Godbert",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Tool > Heightmap Baker",
    "description": "Bake an orthographic top-down heightmap from a selected mesh object",
    "category": "Object",
}

import bpy
import mathutils
import os
from bpy.props import (
    IntProperty,
    FloatProperty,
    EnumProperty,
    StringProperty,
)


# ---------------------------------------------------------------------------
# Properties stored on the Scene so they persist across operator calls
# ---------------------------------------------------------------------------

class HeightmapBakerSettings(bpy.types.PropertyGroup):
    resolution_x: IntProperty(
        name="Width",
        description="Render resolution X",
        default=2048,
        min=64,
        max=8192,
    )
    resolution_y: IntProperty(
        name="Height",
        description="Render resolution Y",
        default=2048,
        min=64,
        max=8192,
    )
    padding: FloatProperty(
        name="Padding",
        description="Ortho-scale multiplier — values above 1.0 add empty border around the mesh",
        default=1.0,
        min=0.01,
        soft_max=2.0,
        step=1,
    )
    file_format: EnumProperty(
        name="Format",
        description="Output image format",
        items=[
            ('PNG',      "PNG",      "16-bit PNG"),
            ('OPEN_EXR', "EXR",      "32-bit OpenEXR"),
        ],
        default='PNG',
    )
    bit_depth: EnumProperty(
        name="Bit Depth",
        items=[('8', "16-bit", ""), ('16', "16-bit", ""), ('32', "32-bit", "")],
        default='16'
    )
    output_dir: StringProperty(
        name="Output Dir",
        description="Directory to save the heightmap. Leave empty to use the .blend file directory (or home folder if unsaved)",
        default="",
        subtype='DIR_PATH',
    )


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

class OBJECT_OT_bake_heightmap(bpy.types.Operator):
    bl_idname  = "object.bake_heightmap"
    bl_label   = "Bake Heightmap"
    bl_description = "Render a top-down orthographic heightmap of the active mesh"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return (
            context.active_object is not None
            and context.active_object.type == 'MESH'
        )

    def execute(self, context):
        scene    = context.scene
        cfg      = scene.heightmap_baker
        obj      = context.active_object

        RESOLUTION_X = cfg.resolution_x
        RESOLUTION_Y = cfg.resolution_y
        PADDING      = cfg.padding
        FILE_FORMAT  = cfg.file_format
        BIT_DEPTH = cfg.bit_depth

        # ── STORE ORIGINAL STATE ──────────────────────────────────────────
        original_camera   = scene.camera
        original_material = obj.data.materials[0] if obj.data.materials else None
        original_world    = scene.world
        original_engine   = scene.render.engine
        original_res_x    = scene.render.resolution_x
        original_res_y    = scene.render.resolution_y
        original_filepath = scene.render.filepath
        original_format   = scene.render.image_settings.file_format
        original_depth    = scene.render.image_settings.color_depth
        original_film_transparent = scene.render.film_transparent

        # ── TEMPORARY WORLD (pure black background) ───────────────────────
        temp_world = bpy.data.worlds.new("TEMP_HEIGHT_WORLD")
        temp_world.use_nodes = True
        temp_world.node_tree.nodes["Background"].inputs[0].default_value = (0, 0, 0, 1)
        scene.world = temp_world

        # ── CALCULATE LOCAL BOUNDS ────────────────────────────────────────
        bb = obj.bound_box  # 8 local-space corners
        local_min = mathutils.Vector((
            min(c[0] for c in bb),
            min(c[1] for c in bb),
            min(c[2] for c in bb),
        ))
        local_max = mathutils.Vector((
            max(c[0] for c in bb),
            max(c[1] for c in bb),
            max(c[2] for c in bb),
        ))
        local_center = (local_min + local_max) / 2
        world_center = obj.matrix_world @ local_center

        size_x = (local_max.x - local_min.x) * obj.scale.x
        size_y = (local_max.y - local_min.y) * obj.scale.y

        # ── CAMERA SETUP (rotation-invariant) ────────────────────────────
        mesh_ratio   = size_x / size_y
        render_ratio = RESOLUTION_X / RESOLUTION_Y
        ortho_scale  = (size_x / render_ratio) if mesh_ratio > render_ratio else size_y

        # Offset along the object's LOCAL +Z so the camera sits above it
        inv_rot = obj.matrix_world.to_quaternion()
        offset  = inv_rot @ mathutils.Vector((0, 0, 10))

        bpy.ops.object.camera_add(location=world_center + offset)
        temp_cam = context.object
        temp_cam.name = "TEMP_HEIGHT_CAM"
        temp_cam.rotation_euler       = obj.rotation_euler   # match object rotation
        temp_cam.data.type            = 'ORTHO'
        temp_cam.data.ortho_scale     = ortho_scale * PADDING
        scene.camera                  = temp_cam

        # ── TEMPORARY MATERIAL (Generated UV → Z → Emission) ─────────────
        temp_mat = bpy.data.materials.new(name="TEMP_HEIGHT_MAT")
        temp_mat.use_nodes = True
        nodes = temp_mat.node_tree.nodes
        nodes.clear()

        n_tex  = nodes.new('ShaderNodeTexCoord')
        n_sep  = nodes.new('ShaderNodeSeparateXYZ')
        n_emit = nodes.new('ShaderNodeEmission')
        n_out  = nodes.new('ShaderNodeOutputMaterial')

        links = temp_mat.node_tree.links
        links.new(n_tex.outputs['Generated'],  n_sep.inputs[0])
        links.new(n_sep.outputs['Z'],          n_emit.inputs['Color'])
        links.new(n_emit.outputs['Emission'],  n_out.inputs['Surface'])

        if obj.data.materials:
            obj.data.materials[0] = temp_mat
        else:
            obj.data.materials.append(temp_mat)

        # ── RENDER SETTINGS ───────────────────────────────────────────────
        scene.render.resolution_x = RESOLUTION_X
        scene.render.resolution_y = RESOLUTION_Y
        scene.render.image_settings.file_format  = FILE_FORMAT
        scene.render.image_settings.color_depth  = BIT_DEPTH
        scene.render.film_transparent = False

        try:
            scene.render.engine = 'BLENDER_EEVEE'
        except Exception:
            scene.render.engine = 'CYCLES'

        # ── OUTPUT PATH ───────────────────────────────────────────────────
        if cfg.output_dir:
            save_dir = bpy.path.abspath(cfg.output_dir)
        elif bpy.data.is_saved:
            save_dir = bpy.path.abspath("//")
        else:
            save_dir = os.path.expanduser("~")

        extension   = "exr" if FILE_FORMAT == 'OPEN_EXR' else "png"
        output_file = os.path.join(save_dir, f"{obj.name}_heightmap.{extension}")
        scene.render.filepath = output_file

        # ── RENDER ────────────────────────────────────────────────────────
        print(f"[Heightmap Baker] Rendering → {output_file}")
        bpy.ops.render.render(write_still=True)

        # ── CLEANUP ───────────────────────────────────────────────────────
        if original_material:
            obj.data.materials[0] = original_material
        else:
            obj.data.materials.pop(index=0)

        bpy.data.materials.remove(temp_mat)
        bpy.data.worlds.remove(temp_world)

        scene.world                              = original_world
        scene.camera                             = original_camera
        scene.render.engine                      = original_engine
        scene.render.resolution_x                = original_res_x
        scene.render.resolution_y                = original_res_y
        scene.render.filepath                    = original_filepath
        scene.render.image_settings.file_format  = original_format
        scene.render.image_settings.color_depth  = original_depth
        scene.render.film_transparent            = original_film_transparent

        bpy.data.objects.remove(temp_cam, do_unlink=True)

        self.report({'INFO'}, f"Heightmap saved → {output_file}")
        print(f"[Heightmap Baker] Done. Cleanup complete.")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Panel — lives in the Tool tab of the N-panel
# ---------------------------------------------------------------------------

class VIEW3D_PT_heightmap_baker(bpy.types.Panel):
    bl_label       = "Heightmap Baker"
    bl_idname      = "VIEW3D_PT_heightmap_baker"
    bl_space_type  = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category    = 'Tool'          # ← "Tool" tab in the N-panel / sidebar

    def draw(self, context):
        layout = self.layout
        cfg    = context.scene.heightmap_baker

        # ── Resolution ───────────────────────────────────────────────────
        box = layout.box()
        box.label(text="Resolution", icon='IMAGE_DATA')
        row = box.row(align=True)
        row.prop(cfg, "resolution_x", text="W")
        row.prop(cfg, "resolution_y", text="H")

        # ── Options ───────────────────────────────────────────────────────
        box = layout.box()
        box.label(text="Options", icon='PREFERENCES')
        box.prop(cfg, "padding")
        box.prop(cfg, "file_format", expand=True)
        box.prop(cfg, "bit_depth", expand=True)

        # ── Output ────────────────────────────────────────────────────────
        box = layout.box()
        box.label(text="Output", icon='FOLDER_REDIRECT')
        box.prop(cfg, "output_dir", text="")

        # ── Action ────────────────────────────────────────────────────────
        layout.separator()
        obj = context.active_object
        if obj and obj.type == 'MESH':
            layout.operator("object.bake_heightmap", icon='RENDER_STILL')
        else:
            col = layout.column()
            col.enabled = False
            col.label(text="Select a Mesh object", icon='INFO')


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    HeightmapBakerSettings,
    OBJECT_OT_bake_heightmap,
    VIEW3D_PT_heightmap_baker,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.heightmap_baker = bpy.props.PointerProperty(
        type=HeightmapBakerSettings
    )

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.heightmap_baker

if __name__ == "__main__":
    register()